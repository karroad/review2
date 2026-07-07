import os, re, json, sqlite3, requests, pyotp, qrcode, hashlib, secrets
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, Response, session, redirect, url_for, abort
from functools import wraps
from PIL import Image
import io
import quality as qmod

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# ── Auth config ───────────────────────────────────────────────────────────────
AUTH_USERS = {}  # loaded from env: USER1=login:sha256:totp_secret

def _load_users():
    """Load users from env vars USER_xxx=login:password_hash:totp_secret"""
    i = 1
    while True:
        u = os.environ.get(f'AUTH_USER{i}')
        if not u:
            break
        parts = u.split(':', 2)
        if len(parts) == 3:
            login, pw_hash, totp_secret = parts
            AUTH_USERS[login] = {'pw_hash': pw_hash, 'totp': totp_secret}
        i += 1

_load_users()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

# Shared-token auth for sister apps (e.g. details2 fetching best_picks).
SHARED_API_TOKEN = os.environ.get('REVIEW2_SHARED_TOKEN', '')

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-API-Token', '')
        if not SHARED_API_TOKEN or token != SHARED_API_TOKEN:
            return jsonify({'error': 'unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def check_password(plain, stored_hash):
    return hashlib.sha256(plain.encode()).hexdigest() == stored_hash

# ── Directories ──────────────────────────────────────────────────────────────
DIRS = {
    'FM':          '/photos/FM',
    'A5':          '/photos/A5',
    'NP':          '/photos/np',
    '300':         '/photos/300',
    'TIFF':        '/photos/tiff',
    'ARTIST_FULL': '/photos/artist_full',
    'MEDIA':       '/photos/media',
    'PERS':        '/photos/perspective',
}
# Files from these dirs get copied to ARTIST_FULL root (= public /np/) before being assigned
STAGING_DIRS = {'ARTIST_FULL', 'MEDIA'}
# Public base URL for images served at /np/
IMAGES_NP_URL = os.environ.get('IMAGES_NP_URL', 'https://images.operagallery.com/np')

DB_PATH = '/app/data/reviewv2.db'

# ── Odoo config ───────────────────────────────────────────────────────────────
ODOO_URL      = os.environ.get('OPERACRM_URL', 'https://operacrm.com')
ODOO_DB       = os.environ.get('OPERACRM_DB', 'odoo_15')
ODOO_LOGIN    = os.environ.get('OPERACRM_LOGIN', '')
ODOO_PASSWORD = os.environ.get('OPERACRM_PASSWORD', '')

# ── Odoo image fields ─────────────────────────────────────────────────────────
ODOO_FIELDS = [
    ('main_super_picture_hd', 'Main'),
    ('frame_picture',         'Frame'),
    ('back_pictures',         'Back'),
    ('in_situ_url',           'In Situ'),
    ('perspective_url',       'Perspective'),
    ('right_picture',         'Right'),
    ('left_picture',          'Left'),
    ('signatures_pictures',   'Sign'),
    ('edition_number_url',    'Ed No'),
    ('detail_1_url',          'Det 1'),
    ('detail_2_url',          'Det 2'),
    ('other_url',             'Other'),
]

FM_ONLY_FIELDS = set()

# ── Main-picture review (écran /mainpics) ──────────────────────────────────────
# On rationalise les 5 anciens champs "main" en 2 canoniques :
#   MAIN HD       → main_super_picture_hd
#   Main Low def  → main_lowdef
# Les autres champs main deviennent "legacy" : on les affiche pour repérer les
# œuvres où une photo dort encore dans un ancien champ, et proposer migration/vidage.
MAINPICS_HD_FIELD     = 'main_super_picture_hd'
MAINPICS_LOWDEF_FIELD = 'main_lowdef'
MAINPICS_FIELDS = [
    (MAINPICS_HD_FIELD,     'MAIN HD'),
    (MAINPICS_LOWDEF_FIELD, 'Main Low def'),
]
MAINPICS_LEGACY_FIELDS = [
    ('main_picture_hd',     'Main HD (legacy)'),
    ('main_web_picture',    'Web (legacy)'),
    ('main_a6_picture',     'A6 (legacy)'),
    ('main_hd_web_picture', 'HD Web (legacy)'),
]
# Tous les champs main que l'écran peut lire/écrire (canoniques + legacy).
MAINPICS_ALL_FIELDS = [f for f, _ in MAINPICS_FIELDS] + [f for f, _ in MAINPICS_LEGACY_FIELDS]
# Rôles disque considérés comme candidats "photo principale".
MAINPICS_ROLES = {'MAIN', 'MAIN A5', 'RECTO'}

# ── Role inference ────────────────────────────────────────────────────────────
ROLE_PATTERNS = [
    (r'_MAIN_A5',    'MAIN A5'),
    (r'_MAIN',       'MAIN'),
    (r'_RECTO|recto','RECTO'),
    (r'_VERSO|verso','VERSO'),
    (r'_BACK|back',  'BACK'),
    (r'IN[_\s\-]?SITU|_PERS|PERSPECTIVE', 'IN SITU'),
    (r'DETAIL|_DET', 'DETAIL'),
    (r'SIGNATURE|signature', 'SIGNATURE'),
    (r'FRAME',       'FRAME'),
    (r'SCALE',       'SCALE'),
    (r'pyramid',     'PYRAMID'),
]

def infer_role(filename):
    for pattern, label in ROLE_PATTERNS:
        if re.search(pattern, filename, re.IGNORECASE):
            return label
    return 'OTHER'

def infer_ext(filename):
    ext = os.path.splitext(filename)[1].lower()
    return ext.lstrip('.')

# ── DB ────────────────────────────────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    # Migrations first: add columns that may not exist in older DBs
    for sql in [
        "ALTER TABLE artworks ADD COLUMN gallery TEXT",
        "ALTER TABLE artworks ADD COLUMN location TEXT",
        "ALTER TABLE artworks ADD COLUMN type TEXT",
        "ALTER TABLE artworks ADD COLUMN review_status TEXT DEFAULT 'pending'",
        "ALTER TABLE artworks ADD COLUMN review_at TEXT",
        "ALTER TABLE artworks ADD COLUMN review_user TEXT",
    ]:
        try:
            db.execute(sql)
            db.commit()
        except sqlite3.OperationalError:
            pass  # column already exists or table doesn't exist yet
    db.executescript('''
        CREATE TABLE IF NOT EXISTS artworks (
            odoo_id     INTEGER PRIMARY KEY,
            id_name     TEXT,
            title       TEXT,
            artist      TEXT,
            status      TEXT,
            category    TEXT,
            gallery     TEXT,
            location    TEXT,
            synced_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_artworks_gallery ON artworks(gallery);
        CREATE TABLE IF NOT EXISTS disk_index (
            id_name     TEXT PRIMARY KEY,
            dirs        TEXT,
            file_count  INTEGER,
            scanned_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS activity_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    DEFAULT (datetime('now')),
            user        TEXT,
            action      TEXT,
            id_name     TEXT,
            odoo_id     INTEGER,
            field       TEXT,
            filename    TEXT,
            detail      TEXT,
            synced_fm   INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_log_ts      ON activity_log(ts);
        CREATE INDEX IF NOT EXISTS idx_log_synced  ON activity_log(synced_fm);
        CREATE TABLE IF NOT EXISTS fm_cache (
            id_name       TEXT PRIMARY KEY,
            record_id     TEXT,
            MAINFM        TEXT, MAIN_HD TEXT, MAINTIF TEXT, UrlMainA5 TEXT,
            BACK300       TEXT, FRONT300 TEXT, FRONTRIGHT300 TEXT, LEFT300 TEXT,
            FRAME300      TEXT, PERS300 TEXT, DET300 TEXT, INSITU300 TEXT,
            OTHER300      TEXT, Signature TEXT,
            last_sync     TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_fm_cache_id ON fm_cache(id_name);
        CREATE TABLE IF NOT EXISTS file_meta (
            path        TEXT PRIMARY KEY,
            dir         TEXT,
            filename    TEXT,
            id_name     TEXT,
            width       INTEGER,
            height      INTEGER,
            filesize    INTEGER,
            scanned_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_file_meta_id ON file_meta(id_name);
    ''')
    db.commit()
    db.close()

def log_action(action, id_name=None, odoo_id=None, field=None, filename=None, detail=None):
    """Insert a row in activity_log."""
    try:
        user = session.get('user', 'unknown')
        db = get_db()
        db.execute(
            'INSERT INTO activity_log (user,action,id_name,odoo_id,field,filename,detail) '
            'VALUES (?,?,?,?,?,?,?)',
            (user, action, id_name, odoo_id, field, filename, detail)
        )
        db.commit()
        db.close()
    except Exception:
        pass  # never break the main flow

ID_PATTERN = re.compile(r'^([A-Z]+-\d+)[_\.]', re.IGNORECASE)

def scan_disk_index():
    """Scan all dirs and build unique id_name → dirs map. Returns count."""
    id_map = {}  # id_name → set of dirs
    for dir_key, dir_path in DIRS.items():
        if not os.path.isdir(dir_path):
            continue
        for fname in os.listdir(dir_path):
            m = ID_PATTERN.match(fname)
            if not m:
                continue
            ext = infer_ext(fname)
            if ext not in ('jpg','jpeg','png','tif','tiff'):
                continue
            id_name = m.group(1).upper()
            if id_name not in id_map:
                id_map[id_name] = {'dirs': set(), 'count': 0}
            id_map[id_name]['dirs'].add(dir_key)
            id_map[id_name]['count'] += 1

    db = get_db()
    db.execute("DELETE FROM disk_index")
    for id_name, info in id_map.items():
        db.execute(
            "INSERT INTO disk_index (id_name, dirs, file_count, scanned_at) VALUES (?,?,?,datetime('now'))",
            (id_name, ','.join(sorted(info['dirs'])), info['count'])
        )
    db.commit()
    db.close()
    return len(id_map)

# ── Odoo session (cached) ─────────────────────────────────────────────────────
_odoo_session = None

def _crm_session():
    """Retourne une session requests authentifiée sur Odoo (mise en cache)."""
    global _odoo_session
    if _odoo_session is not None:
        return _odoo_session
    sess = requests.Session()
    auth = sess.post(f'{ODOO_URL}/web/session/authenticate', json={
        'jsonrpc': '2.0', 'method': 'call', 'id': 1,
        'params': {'db': ODOO_DB, 'login': ODOO_LOGIN, 'password': ODOO_PASSWORD}
    }, timeout=15)
    auth.raise_for_status()
    if not auth.json().get('result', {}).get('uid'):
        raise Exception('Authentification Odoo échouée')
    _odoo_session = sess
    return sess

def odoo_call(model, method, args, kwargs=None):
    global _odoo_session
    sess = _crm_session()
    r = sess.post(f'{ODOO_URL}/web/dataset/call_kw', json={
        'jsonrpc': '2.0', 'method': 'call', 'id': 2,
        'params': {
            'model': model, 'method': method,
            'args': args, 'kwargs': kwargs or {}
        }
    }, timeout=30)
    result = r.json()
    # Session expirée → on réauthentifie une fois
    if result.get('error', {}).get('code') == 100:
        _odoo_session = None
        sess = _crm_session()
        r = sess.post(f'{ODOO_URL}/web/dataset/call_kw', json={
            'jsonrpc': '2.0', 'method': 'call', 'id': 2,
            'params': {'model': model, 'method': method,
                       'args': args, 'kwargs': kwargs or {}}
        }, timeout=30)
        result = r.json()
    return result.get('result')

# ── Disk scan ─────────────────────────────────────────────────────────────────
def scan_artwork_files(id_name):
    """Return list of files on disk matching ARTIST-ID pattern."""
    results = []
    m = re.search(r'-(\d+)$', id_name)
    if not m:
        return results
    pattern     = re.compile(rf'^{re.escape(id_name)}[_\.]', re.IGNORECASE)
    nas_pattern = re.compile(rf'^\[{re.escape(id_name)}\][_\s]', re.IGNORECASE)

    for dir_key, dir_path in DIRS.items():
        if not os.path.isdir(dir_path):
            continue
        # Walk subdirectories for staging dirs, flat scan otherwise
        if dir_key in STAGING_DIRS:
            file_iter = []
            for root, _, files in os.walk(dir_path):
                for f in files:
                    full = os.path.join(root, f)
                    rel  = os.path.relpath(full, dir_path)
                    file_iter.append((f, rel))
        else:
            file_iter = [(f, f) for f in os.listdir(dir_path)]

        for fname, rel_path in file_iter:
            is_nas_fmt = nas_pattern.match(fname)
            if not pattern.match(fname) and not is_nas_fmt:
                continue
            ext = infer_ext(fname)
            if ext not in ('jpg', 'jpeg', 'png', 'tif', 'tiff'):
                continue
            is_nas = bool(dir_key == 'MEDIA' and (is_nas_fmt or rel_path.lower().startswith('nas/')))
            # Cache-bust query param: forces Varnish/Cloudflare/browser to re-fetch
            # when source file is updated (mtime changes → URL changes).
            try:
                v = int(os.path.getmtime(os.path.join(dir_path, rel_path)))
            except OSError:
                v = 0
            results.append({
                'dir':      dir_key,
                'filename': fname,
                'role':     infer_role(fname),
                'ext':      ext,
                'url':      f'/photo/{dir_key}/{rel_path}?v={v}',
                'thumb':    f'/thumb/{dir_key}/{rel_path}?v={v}',
                'width':    0,
                'height':   0,
                'filesize': 0,
                'best':     False,
                'nas':      is_nas,
                'nas_path': rel_path if is_nas else None,
            })

    # Enrich with dimensions from file_meta
    if results:
        try:
            db = get_db()
            meta_rows = db.execute(
                'SELECT filename, dir, width, height, filesize FROM file_meta WHERE id_name=?',
                (id_name,)
            ).fetchall()
            db.close()
            meta_map = {(r['dir'], r['filename']): r for r in meta_rows}
            for f in results:
                m = meta_map.get((f['dir'], f['filename']))
                if m:
                    f['width']    = m['width']    or 0
                    f['height']   = m['height']   or 0
                    f['filesize'] = m['filesize']  or 0
        except Exception:
            pass

    # IN SITU = the curated /photos/perspective folder is the source of truth.
    # If a PERS file exists for this id_name, demote _PERS-named files coming
    # from other dirs (FM/A5/300/etc.) to OTHER so they don't pose as perspective.
    has_pers = any(f['dir'] == 'PERS' and f['role'] == 'IN SITU' for f in results)
    if has_pers:
        for f in results:
            if f['role'] == 'IN SITU' and f['dir'] != 'PERS':
                f['role'] = 'OTHER'

    # Mark best (most pixels) per role group
    from collections import defaultdict
    by_role = defaultdict(list)
    for f in results:
        by_role[f['role']].append(f)
    for group in by_role.values():
        if len(group) > 1:
            best = max(group, key=lambda f: f['width'] * f['height'])
            if best['width'] > 0:
                best['best'] = True

    # Sort: FM first, then A5, NP, 300, TIFF; within dir by role priority
    role_order = ['MAIN', 'MAIN A5', 'RECTO', 'BACK', 'DETAIL', 'IN SITU',
                  'FRAME', 'SIGNATURE', 'SCALE', 'VERSO', 'PYRAMID', 'OTHER']
    dir_order  = ['PERS', 'FM', 'A5', 'NP', '300', 'TIFF', 'ARTIST_FULL', 'MEDIA']
    results.sort(key=lambda f: (
        dir_order.index(f['dir']) if f['dir'] in dir_order else 99,
        role_order.index(f['role']) if f['role'] in role_order else 99,
    ))
    return results

# ── Auth routes ───────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        login_val = request.form.get('login', '').strip()
        password  = request.form.get('password', '')
        otp_code  = request.form.get('otp', '').strip()
        user = AUTH_USERS.get(login_val)
        if not user:
            error = 'Identifiants incorrects'
        elif not check_password(password, user['pw_hash']):
            error = 'Identifiants incorrects'
        else:
            totp = pyotp.TOTP(user['totp'])
            if not totp.verify(otp_code, valid_window=1):
                error = 'Code OTP invalide'
            else:
                session['logged_in'] = True
                session['login'] = login_val
                next_url = request.args.get('next', '/')
                return redirect(next_url)
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/setup_otp/<login>')
def setup_otp(login):
    """Show QR code for OTP setup (only accessible if not yet logged in and user exists)."""
    user = AUTH_USERS.get(login)
    if not user:
        abort(404)
    totp = pyotp.TOTP(user['totp'])
    uri = totp.provisioning_uri(name=login, issuer_name='ReviewV2')
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
@login_required
def index():
    q = request.args.get('q', '').strip()
    page = int(request.args.get('page', 1))
    per_page = 100

    db = get_db()
    if q:
        rows = db.execute(
            "SELECT a.*, d.dirs, d.file_count FROM artworks a "
            "LEFT JOIN disk_index d ON d.id_name=a.id_name "
            "WHERE a.id_name LIKE ? OR a.title LIKE ? OR a.artist LIKE ? "
            "ORDER BY a.id_name LIMIT ? OFFSET ?",
            (f'%{q}%', f'%{q}%', f'%{q}%', per_page, (page-1)*per_page)
        ).fetchall()
        total = db.execute(
            "SELECT COUNT(*) FROM artworks WHERE id_name LIKE ? OR title LIKE ? OR artist LIKE ?",
            (f'%{q}%', f'%{q}%', f'%{q}%')
        ).fetchone()[0]
    else:
        rows = db.execute(
            "SELECT a.*, d.dirs, d.file_count FROM artworks a "
            "LEFT JOIN disk_index d ON d.id_name=a.id_name "
            "ORDER BY a.id_name LIMIT ? OFFSET ?",
            (per_page, (page-1)*per_page)
        ).fetchall()
        total = db.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]
    db.close()

    artworks = [dict(r) for r in rows]
    pages = (total + per_page - 1) // per_page
    return render_template('index.html', artworks=artworks, q=q,
                           page=page, pages=pages, total=total)

@app.route('/artwork/<id_name>')
@login_required
def artwork(id_name):
    db = get_db()
    row = db.execute("SELECT * FROM artworks WHERE id_name=?", (id_name,)).fetchone()
    db.close()
    aw = dict(row) if row else {'id_name': id_name, 'title': '', 'artist': '', 'odoo_id': None}

    # Fetch current Odoo photo URLs
    odoo_photos = {}
    if aw.get('odoo_id'):
        try:
            fields = [f for f, _ in ODOO_FIELDS]
            res = odoo_call('product.template', 'read',
                            [[aw['odoo_id']], fields])
            if res:
                odoo_photos = {f: (res[0].get(f) or '') for f in fields}
        except Exception as e:
            odoo_photos = {f: '' for f, _ in ODOO_FIELDS}

    disk_files = scan_artwork_files(id_name)

    return render_template('artwork.html',
                           aw=aw,
                           odoo_fields=ODOO_FIELDS,
                           odoo_photos=odoo_photos,
                           disk_files=disk_files)

def _send_with_revalidation(path, source_path, mimetype=None):
    """Send a file with ETag/Last-Modified based on source mtime so the
    browser revalidates instead of serving a stale copy from its own cache."""
    try:
        src_mtime = int(os.path.getmtime(source_path))
    except OSError:
        src_mtime = 0
    resp = send_file(path, mimetype=mimetype)
    resp.headers['Cache-Control'] = 'no-cache, must-revalidate'
    resp.headers['ETag'] = f'"{src_mtime}-{os.path.basename(source_path)}"'
    return resp

def _cache_is_fresh(cache_path, source_path):
    """Cache is fresh only if it exists AND is newer than source file."""
    if not os.path.exists(cache_path):
        return False
    try:
        return os.path.getmtime(cache_path) >= os.path.getmtime(source_path)
    except OSError:
        return False

@app.route('/photo/<dir_key>/<path:filename>')
def serve_photo(dir_key, filename):
    dir_path = DIRS.get(dir_key)
    if not dir_path:
        return 'Not found', 404
    full = os.path.join(dir_path, filename)
    if not os.path.exists(full):
        return 'Not found', 404
    # Browsers cannot render TIFF — serve a JPEG copy (cached) so cross-app
    # consumers (details2, lightbox previews) can display perspective/in-situ
    # files without a separate conversion step.
    ext = os.path.splitext(filename)[1].lower()
    if ext in ('.tif', '.tiff'):
        cache_root = f'/app/data/jpeg_cache/{dir_key}'
        cache_path = os.path.join(cache_root, filename + '.jpg')
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        if not _cache_is_fresh(cache_path, full):
            try:
                img = Image.open(full)
                if img.mode in ('RGBA', 'P', 'LA', 'CMYK'):
                    img = img.convert('RGB')
                if max(img.size) > 4000:
                    img.thumbnail((4000, 4000))
                img.save(cache_path, 'JPEG', quality=88)
            except Exception as e:
                print(f'[tiff_convert] {filename}: {e}', flush=True)
                return _send_with_revalidation(full, full)
        return _send_with_revalidation(cache_path, full, mimetype='image/jpeg')
    return _send_with_revalidation(full, full)

@app.route('/thumb/<dir_key>/<path:filename>')
def serve_thumb(dir_key, filename):
    dir_path = DIRS.get(dir_key)
    if not dir_path:
        return 'Not found', 404
    full = os.path.join(dir_path, filename)
    if not os.path.exists(full):
        return 'Not found', 404
    # Generate thumbnail (regenerate if source is newer than cached thumb)
    cache_dir = f'/app/data/thumbs/{dir_key}'
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, filename + '.jpg')
    if not _cache_is_fresh(cache_path, full):
        try:
            img = Image.open(full)
            img.thumbnail((400, 400))
            if img.mode not in ('RGB',):
                img = img.convert('RGB')
            img.save(cache_path, 'JPEG', quality=80)
        except Exception:
            return _send_with_revalidation(full, full)
    return _send_with_revalidation(cache_path, full, mimetype='image/jpeg')

def _maybe_convert_tiff(url, field):
    """If url points to a TIFF and field doesn't contain 'tif', convert to JPG.
    Returns the (possibly updated) url and filename."""
    if not url:
        return url
    filename = url.split('/')[-1].split('?')[0]
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ('.tif', '.tiff'):
        return url
    if 'tif' in field.lower():
        return url  # TIF field → keep as-is
    # Find local file — track which dir_key it came from
    local_path = None
    found_dir_key = None
    for dir_key, d in DIRS.items():
        candidate = os.path.join(d, filename)
        if os.path.exists(candidate):
            local_path = candidate
            found_dir_key = dir_key
            break
    if not local_path:
        return url  # can't find file locally, send as-is
    jpg_filename = os.path.splitext(filename)[0] + '.jpg'
    jpg_path = os.path.join(os.path.dirname(local_path), jpg_filename)
    if not _cache_is_fresh(jpg_path, local_path):
        try:
            from PIL import Image as _Img
            _Img.MAX_IMAGE_PIXELS = None  # allow large TIFFs
            img = _Img.open(local_path)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(jpg_path, 'JPEG', quality=92)
        except Exception:
            return url  # conversion failed, send original
    # Rebuild URL using the correct dir_key (where the file was actually found)
    base = url[:url.find('/photo/') + len('/photo/')]
    return f'{base}{found_dir_key}/{jpg_filename}'


def _normalize_filename(fname):
    """Normalize filename for public URL: spaces→_, remove special chars."""
    name, ext = os.path.splitext(fname)
    name = re.sub(r'[\s,;]+', '_', name)
    name = re.sub(r'[°()\'"&%#@!+]', '', name)
    name = re.sub(r'_+', '_', name).strip('_')
    return name + ext

@app.route('/api/assign', methods=['POST'])
@login_required
def assign_photo():
    """Assign a disk photo URL to an Odoo field."""
    data = request.json
    odoo_id  = data.get('odoo_id')
    field    = data.get('field')
    url      = data.get('url')   # full public URL
    id_name  = data.get('id_name', '')
    if not odoo_id or not field:
        return jsonify({'error': 'missing params'}), 400
    # Validate field name (ODOO_FIELDS + champs de l'écran /mainpics)
    valid_fields = [f for f, _ in ODOO_FIELDS] + MAINPICS_ALL_FIELDS
    if field not in valid_fields:
        return jsonify({'error': 'invalid field'}), 400
    try:
        if url:
            for dir_key in STAGING_DIRS:
                dir_path = DIRS[dir_key]
                marker = f'/photo/{dir_key}/'
                if marker.lower() in url.lower():
                    idx      = url.lower().find(marker.lower())
                    rel_path = url[idx + len(marker):].split('?')[0]
                    fname    = rel_path.split('/')[-1]
                    src      = os.path.join(dir_path, rel_path)

                    if dir_key == 'MEDIA' and os.path.exists(src):
                        # NAS/MEDIA: convert to JPEG and copy into FM dir
                        suffix    = FIELD_SUFFIX.get(field, '_MEDIA')
                        base_name = f'{id_name.upper()}{suffix}.jpg'
                        counter   = 2
                        fm_dir    = DIRS.get('FM', '')
                        dest_name = base_name
                        while fm_dir and os.path.exists(os.path.join(fm_dir, dest_name)):
                            dest_name = f'{id_name.upper()}{suffix}_{counter}.jpg'
                            counter  += 1
                        if fm_dir:
                            img = Image.open(src)
                            if img.mode in ('RGBA', 'P', 'LA', 'CMYK'):
                                img = img.convert('RGB')
                            img.save(os.path.join(fm_dir, dest_name), 'JPEG', quality=92)
                            url = request.host_url.rstrip('/') + f'/photo/FM/{dest_name}'
                    else:
                        import shutil
                        norm = _normalize_filename(fname)
                        dst  = os.path.join(DIRS['ARTIST_FULL'], norm)
                        if os.path.exists(src) and not os.path.exists(dst):
                            shutil.copy2(src, dst)
                        url = f'{IMAGES_NP_URL}/{norm}'
                    break
            url = _maybe_convert_tiff(url, field)

        action = 'clear' if not url else 'assign'
        fname  = url.split('/')[-1].split('?')[0] if url else None

        if field not in FM_ONLY_FIELDS:
            # Normal flow: write to Odoo
            odoo_call('product.template', 'write',
                      [[int(odoo_id)], {field: url or False}])
        elif action == 'clear' and field in FM_PHOTOS_TABLE_FIELDS:
            # FM-only clear: delete the record from CRMPhotos immediately
            prev_url = data.get('prev_url', '')  # caller passes old URL
            if prev_url:
                try:
                    token = _fm_get_token()
                    if token:
                        _fm_delete_photo_record(token, id_name, prev_url)
                        requests.delete(f'{FM_URL}/databases/{FM_DB}/sessions/{token}',
                                        timeout=5, verify=False)
                except Exception:
                    pass

        log_action(action, id_name=id_name, odoo_id=odoo_id, field=field, filename=fname,
                   detail=url or 'cleared')
        return jsonify({'ok': True, 'url': url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/sync', methods=['POST'])
@login_required
def sync_crm():
    """Sync artworks from Odoo into local DB. Preserves review_status across syncs."""
    try:
        fields = ['IdName', 'name', 'ArtistName', 'Status', 'Category', 'CRMTypeLayout', 'id']
        offset, batch = 0, 500
        all_records = []
        while True:
            res = odoo_call('product.template', 'search_read',
                            [[['IdName', '!=', False]]],
                            {'fields': fields, 'limit': batch, 'offset': offset})
            if not res:
                break
            all_records.extend(res)
            if len(res) < batch:
                break
            offset += batch

        db = get_db()
        for r in all_records:
            # UPSERT keeps review_status / review_at / review_user across syncs
            db.execute(
                """INSERT INTO artworks (odoo_id, id_name, title, artist, status, category, type, synced_at)
                   VALUES (?,?,?,?,?,?,?, datetime('now'))
                   ON CONFLICT(odoo_id) DO UPDATE SET
                     id_name=excluded.id_name, title=excluded.title, artist=excluded.artist,
                     status=excluded.status, category=excluded.category, type=excluded.type,
                     synced_at=excluded.synced_at""",
                (r['id'], r.get('IdName',''), r.get('name',''),
                 r.get('ArtistName',''), r.get('Status',''), r.get('Category',''),
                 r.get('CRMTypeLayout') or '')
            )
        db.commit()
        db.close()
        return jsonify({'ok': True, 'count': len(all_records)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scan_disk', methods=['POST'])
@login_required
def api_scan_disk():
    """Scan all photo dirs and rebuild disk_index."""
    try:
        count = scan_disk_index()
        return jsonify({'ok': True, 'count': count})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/disk_files/<id_name>')
@login_required
def api_disk_files(id_name):
    return jsonify(scan_artwork_files(id_name))

@app.route('/api/artwork_detail/<id_name>')
@login_required
def api_artwork_detail(id_name):
    """Return disk files + current Odoo field assignments for one artwork."""
    db  = get_db()
    row = db.execute("SELECT * FROM artworks WHERE id_name=?", (id_name,)).fetchone()
    db.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404

    disk_files  = scan_artwork_files(id_name)
    odoo_photos = {f: '' for f, _ in ODOO_FIELDS}
    if row['odoo_id']:
        fields = [f for f, _ in ODOO_FIELDS]
        try:
            result = odoo_call('product.template', 'read',
                               [[int(row['odoo_id'])], fields])
            if result:
                for f in fields:
                    odoo_photos[f] = result[0].get(f) or ''
        except Exception as e:
            # Fallback: read fields one-by-one so one invalid field doesn't
            # wipe out the whole response.
            print(f'[artwork_detail] bulk read failed ({e}) — falling back to per-field', flush=True)
            for f in fields:
                try:
                    res = odoo_call('product.template', 'read',
                                    [[int(row['odoo_id'])], [f]])
                    if res:
                        odoo_photos[f] = res[0].get(f) or ''
                except Exception as fe:
                    print(f'[artwork_detail] field {f}: {fe}', flush=True)

    return jsonify({
        'id_name':       id_name,
        'title':         row['title'],
        'artist':        row['artist'],
        'odoo_id':       row['odoo_id'],
        'type':          row['type'] if 'type' in row.keys() else '',
        'review_status': row['review_status'] if 'review_status' in row.keys() else 'pending',
        'disk_files':    disk_files,
        'odoo_photos':   odoo_photos,
        'odoo_fields':   [[f, label] for f, label in ODOO_FIELDS],
    })

@app.route('/api/artwork_list')
@login_required
def api_artwork_list():
    """Return ordered list of id_names for prev/next navigation."""
    db = get_db()
    rows = db.execute("SELECT id_name FROM artworks ORDER BY id_name").fetchall()
    db.close()
    return jsonify([r['id_name'] for r in rows])


# ── Écran /mainpics : revue des 2 champs main (HD + Low def) ───────────────────
@app.route('/mainpics')
@login_required
def mainpics_page():
    return render_template('mainpics.html')

@app.route('/api/mainpics/<id_name>')
@login_required
def api_mainpics(id_name):
    """Détail focalisé main-picture : les 2 champs canoniques, les champs legacy
    encore remplis, et les candidats photo principale sur disque."""
    id_name = id_name.upper()
    db  = get_db()
    row = db.execute("SELECT * FROM artworks WHERE id_name=?", (id_name,)).fetchone()
    db.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404

    # Lecture Odoo des champs main (bulk, puis fallback par champ si un champ
    # n'existe pas — ex : main_lowdef pas encore créé côté Odoo).
    photos      = {f: '' for f in MAINPICS_ALL_FIELDS}
    field_error = {}   # field → message si lecture impossible
    if row['odoo_id']:
        oid = int(row['odoo_id'])
        try:
            res = odoo_call('product.template', 'read', [[oid], MAINPICS_ALL_FIELDS])
            if res:
                for f in MAINPICS_ALL_FIELDS:
                    photos[f] = res[0].get(f) or ''
        except Exception as e:
            print(f'[mainpics] bulk read {id_name} failed ({e}) — per-field', flush=True)
            for f in MAINPICS_ALL_FIELDS:
                try:
                    r = odoo_call('product.template', 'read', [[oid], [f]])
                    if r:
                        photos[f] = r[0].get(f) or ''
                except Exception as fe:
                    field_error[f] = str(fe)

    # Candidats disque : on garde tout mais on met les rôles MAIN en tête.
    disk = scan_artwork_files(id_name)
    for f in disk:
        f['is_main'] = f['role'] in MAINPICS_ROLES
    disk.sort(key=lambda f: (0 if f['is_main'] else 1))

    # Legacy encore rempli = signal "à migrer/vider"
    legacy = [
        {'field': f, 'label': lbl, 'url': photos.get(f, '')}
        for f, lbl in MAINPICS_LEGACY_FIELDS
        if (photos.get(f, '') or '').strip()
    ]

    return jsonify({
        'id_name':       id_name,
        'title':         row['title'] or '',
        'artist':        row['artist'] or '',
        'status':        row['status'] or '',
        'odoo_id':       row['odoo_id'],
        'review_status': row['review_status'] if 'review_status' in row.keys() else 'pending',
        'fields':        [[f, lbl] for f, lbl in MAINPICS_FIELDS],
        'photos':        photos,
        'legacy':        legacy,
        'field_error':   field_error,
        'disk':          disk,
    })


# Role → preferred Odoo field (used by /api/best_picks for v2 dashboard)
ROLE_TO_ODOO_FIELD = {
    'MAIN':       'main_super_picture_hd',
    'MAIN A5':    'main_web_picture',
    'RECTO':      'main_super_picture_hd',
    'BACK':       'back_pictures',
    'VERSO':      'back_pictures',
    'FRAME':      'frame_picture',
    'IN SITU':    'perspective_url',
    'SIGNATURE':  'signatures_pictures',
    'DETAIL':     'detail_1_url',
}


def _compute_best_picks(id_name, absolute_url_base=None):
    """
    Pure helper: scan + score + pick best.
    If absolute_url_base is given, /photo/ and /thumb/ URLs are prefixed.
    Returns the dict body used by /api/best_picks routes.
    """
    files = scan_artwork_files(id_name)

    # Score each file using the path on disk
    for f in files:
        dir_path = DIRS.get(f['dir'])
        if not dir_path:
            f['quality'] = {'ok': False, 'error': 'unknown dir', 'quality_score': 0}
            continue
        # rel path inside dir (works for both flat and walked dirs)
        rel = f['url'].split(f'/photo/{f["dir"]}/', 1)[-1]
        full = os.path.join(dir_path, rel)
        f['quality'] = qmod.score_image(full)

    # Establish baseline aspect from MAIN family
    baseline = qmod.find_baseline_aspect(files)

    # Recompute is_likely_crop now that we know baseline
    if baseline:
        for f in files:
            q = f.get('quality', {})
            if not q.get('ok'):
                continue
            crop = qmod.is_likely_crop(q.get('aspect'), baseline)
            if crop and not q.get('is_likely_crop'):
                q['is_likely_crop'] = True
                q['quality_score'] = max(0, q['quality_score'] - 30)
                q['why'] = (q.get('why') or '') + ' · ⚠ likely crop'

    # Detect duplicates (mark lower-scoring of same-size pairs)
    files = qmod.detect_duplicates(files)

    # Pick best per role
    best_per_role_full = qmod.pick_best_per_role(files)
    best_per_role = {}
    best_per_field = {}
    for role, fobj in best_per_role_full.items():
        if not fobj:
            continue
        url = fobj['url']
        best_per_role[role] = url
        field = ROLE_TO_ODOO_FIELD.get(role)
        if field and field not in best_per_field:
            q = fobj.get('quality', {})
            best_per_field[field] = {
                'url':      url,
                'score':    q.get('quality_score', 0),
                'why':      q.get('why', ''),
                'role':     role,
                'dir':      fobj['dir'],
                'filename': fobj['filename'],
            }

    # Prefix URLs with absolute base if requested (cross-domain consumers).
    # URL-encode the filename portion so spaces/commas/etc. don't break <img src>.
    if absolute_url_base:
        from urllib.parse import quote
        def _enc_rel(rel_path):
            """Encode a /photo/<dir>/<rest> or /thumb/<dir>/<rest> path once."""
            for marker in ('/photo/', '/thumb/'):
                if rel_path.startswith(marker):
                    parts = rel_path[len(marker):].split('/', 1)
                    if len(parts) == 2:
                        d, rest = parts
                        return marker + d + '/' + quote(rest, safe='/')
            return rel_path

        # Build a lookup so best_per_field can reference the same encoded URL.
        url_by_key = {}
        for f in files:
            rel = f.get('url', '')
            thumb_rel = f.get('thumb', '')
            if rel.startswith('/'):
                f['url']   = absolute_url_base + _enc_rel(rel)
            if thumb_rel.startswith('/'):
                f['thumb'] = absolute_url_base + _enc_rel(thumb_rel)
            url_by_key[(f['dir'], f['filename'])] = (f['url'], f['thumb'])

        # best_per_role values are simple URL strings (already-relative — encode now)
        for role, rel in list(best_per_role.items()):
            if isinstance(rel, str) and rel.startswith('/'):
                best_per_role[role] = absolute_url_base + _enc_rel(rel)

        # best_per_field values are dicts — reuse the encoded URLs from url_by_key.
        for field, v in list(best_per_field.items()):
            if isinstance(v, dict):
                key = (v.get('dir'), v.get('filename'))
                if key in url_by_key:
                    v['url'], v['thumb'] = url_by_key[key]
                else:
                    v['url']   = absolute_url_base + '/photo/' + v['dir'] + '/' + quote(v['filename'], safe='')
                    v['thumb'] = absolute_url_base + '/thumb/' + v['dir'] + '/' + quote(v['filename'], safe='')

    return {
        'files':           files,
        'baseline_aspect': baseline,
        'best_per_role':   best_per_role,
        'best_per_field':  best_per_field,
    }


# External endpoint for sister apps (details2). Token-protected, returns
# absolute URLs so cross-domain consumers can render images directly.
EXTERNAL_BASE_URL = os.environ.get('REVIEW2_PUBLIC_URL', 'https://review.operagallery.com')

@app.route('/api/external/best_picks/<id_name>')
@token_required
def api_external_best_picks(id_name):
    return jsonify(_compute_best_picks(id_name, absolute_url_base=EXTERNAL_BASE_URL))

# ── Auto-assign rules ─────────────────────────────────────────────────────────
AUTO_ASSIGN_RULES = [
    # (dir, role, odoo_field)  — premier match gagne
    ('FM',   'MAIN',       'main_picture_hd'),
    ('FM',   'RECTO',      'main_picture_hd'),
    ('A5',   'MAIN A5',    'main_web_picture'),
    ('A5',   'MAIN',       'main_web_picture'),
    ('A5',   'RECTO',      'main_web_picture'),
    ('NP',   'RECTO',      'main_picture_hd'),
    ('NP',   'MAIN',       'main_picture_hd'),
    ('FM',   'BACK',       'back_pictures'),
    ('NP',   'BACK',       'back_pictures'),
    ('NP',   'VERSO',      'back_pictures'),
    ('FM',   'VERSO',      'back_pictures'),
    ('NP',   'DETAIL',     'detail_1_url'),
    ('FM',   'DETAIL',     'detail_1_url'),
    ('300',  'DETAIL',     'detail_1_url'),
    ('NP',   'FRAME',      'frame_picture'),
    ('FM',   'FRAME',      'frame_picture'),
    ('NP',   'SIGNATURE',  'signatures_pictures'),
    ('FM',   'SIGNATURE',  'signatures_pictures'),
    ('PERS',     'IN SITU', 'perspective_url'),
    ('NP',       'IN SITU', 'perspective_url'),
    ('300',      'IN SITU', 'perspective_url'),
]

@app.route('/api/auto_assign/<id_name>', methods=['POST'])
@login_required
def auto_assign(id_name):
    """Auto-assign disk photos to Odoo fields based on role/dir conventions."""
    db = get_db()
    row = db.execute("SELECT odoo_id FROM artworks WHERE id_name=?", (id_name,)).fetchone()
    db.close()
    if not row or not row['odoo_id']:
        return jsonify({'error': 'Œuvre non trouvée ou sans odoo_id'}), 404

    odoo_id = row['odoo_id']
    files = scan_artwork_files(id_name)
    assigned = {}  # field → url (already assigned this session)

    for dir_key, role, field in AUTO_ASSIGN_RULES:
        if field in assigned:
            continue  # field already filled
        for f in files:
            if f['dir'] == dir_key and f['role'] == role:
                url = request.host_url.rstrip('/') + f['url']
                assigned[field] = url
                break

    if not assigned:
        return jsonify({'ok': True, 'assigned': {}, 'message': 'Aucun fichier correspondant trouvé'})

    try:
        odoo_call('product.template', 'write', [[int(odoo_id)], assigned])
        return jsonify({'ok': True, 'assigned': assigned})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Review workflow (Validate / Skip) ─────────────────────────────────────────
def _set_review_status(id_name, new_status):
    """Update review status for an artwork. Returns (ok, error)."""
    db  = get_db()
    row = db.execute("SELECT odoo_id FROM artworks WHERE id_name=?", (id_name,)).fetchone()
    if not row:
        db.close()
        return False, 'Œuvre non trouvée'
    user = session.get('user', 'unknown')
    db.execute(
        "UPDATE artworks SET review_status=?, review_at=datetime('now'), review_user=? "
        "WHERE id_name=?",
        (new_status, user, id_name)
    )
    db.commit()
    db.close()
    log_action(f'review_{new_status}', id_name=id_name, odoo_id=row['odoo_id'])
    return True, None

@app.route('/api/review/validate/<id_name>', methods=['POST'])
@login_required
def api_review_validate(id_name):
    ok, err = _set_review_status(id_name.upper(), 'validated')
    if not ok:
        return jsonify({'ok': False, 'error': err}), 404
    return jsonify({'ok': True, 'status': 'validated'})

@app.route('/api/review/skip/<id_name>', methods=['POST'])
@login_required
def api_review_skip(id_name):
    ok, err = _set_review_status(id_name.upper(), 'skipped')
    if not ok:
        return jsonify({'ok': False, 'error': err}), 404
    return jsonify({'ok': True, 'status': 'skipped'})

@app.route('/api/review/reset/<id_name>', methods=['POST'])
@login_required
def api_review_reset(id_name):
    ok, err = _set_review_status(id_name.upper(), 'pending')
    if not ok:
        return jsonify({'ok': False, 'error': err}), 404
    return jsonify({'ok': True, 'status': 'pending'})

# ── Mode Express ──────────────────────────────────────────────────────────────
@app.route('/rapid')
@login_required
def rapid():
    return render_template('rapid.html', dirs=list(DIRS.keys()))

@app.route('/api/artworks_meta')
@login_required
def api_artworks_meta():
    """Return id_name → {title, artist, status, type, review_status} from local DB."""
    db   = get_db()
    rows = db.execute(
        "SELECT id_name, title, artist, status, type, review_status FROM artworks"
    ).fetchall()
    db.close()
    return jsonify({r['id_name']: {
        'title':         r['title']  or '',
        'artist':        r['artist'] or '',
        'status':        r['status'] or '',
        'type':          r['type']   or '',
        'review_status': r['review_status'] or 'pending',
    } for r in rows})

@app.route('/api/role_index')
@login_required
def api_role_index():
    """Scan all dirs ONCE and return {id_name: [roles]} — fast, no Odoo call."""
    index = {}   # id_name → set of roles
    dirs_present = {}  # id_name → set of dirs
    for dir_key, dir_path in DIRS.items():
        if not os.path.isdir(dir_path):
            continue
        for fname in os.listdir(dir_path):
            if not fname.lower().endswith(('.jpg', '.jpeg', '.png', '.tif', '.tiff')):
                continue
            m = ID_PATTERN.match(fname)
            if not m:
                continue
            id_name = m.group(1).upper()
            role    = infer_role(fname)
            index.setdefault(id_name, set()).add(role)
            dirs_present.setdefault(id_name, set()).add(dir_key)
    return jsonify({
        k: {'roles': list(v), 'dirs': list(dirs_present.get(k, []))}
        for k, v in index.items()
    })

@app.route('/api/rapid_photos')
@login_required
def api_rapid_photos():
    dir_filter = request.args.get('dir', 'ALL')
    offset     = int(request.args.get('offset', 0))
    limit      = int(request.args.get('limit', 80))
    q          = request.args.get('q', '').strip().lower()

    dirs_scan = {k: v for k, v in DIRS.items()} if dir_filter == 'ALL' else \
                {dir_filter: DIRS[dir_filter]} if dir_filter in DIRS else {}

    all_files = []
    for dir_key, dir_path in dirs_scan.items():
        if not os.path.isdir(dir_path):
            continue
        for fname in os.listdir(dir_path):
            if not fname.lower().endswith(('.jpg', '.jpeg', '.png', '.tif', '.tiff')):
                continue
            if q and q not in fname.lower():
                continue
            all_files.append((dir_key, fname))

    all_files.sort(key=lambda x: (x[1].lower(), x[0]))
    total      = len(all_files)
    page_files = all_files[offset:offset + limit]

    db = get_db()
    results = []
    for dir_key, fname in page_files:
        m       = ID_PATTERN.match(fname)
        id_name = m.group(1).upper() if m else None
        title = artist = None
        if id_name:
            row = db.execute(
                'SELECT title, artist FROM artworks WHERE id_name=?', (id_name,)
            ).fetchone()
            if row:
                title  = row['title']
                artist = row['artist']
        try:
            v = int(os.path.getmtime(os.path.join(dirs_scan.get(dir_key, ''), fname)))
        except OSError:
            v = 0
        results.append({
            'dir':      dir_key,
            'filename': fname,
            'url':      f'/photo/{dir_key}/{fname}?v={v}',
            'thumb':    f'/thumb/{dir_key}/{fname}?v={v}',
            'id_name':  id_name,
            'title':    title,
            'artist':   artist,
            'role':     infer_role(fname),
        })
    db.close()
    return jsonify({'total': total, 'offset': offset, 'items': results})

# ── Image editor ──────────────────────────────────────────────────────────────
@app.route('/edit/<dir_key>/<path:filename>')
@login_required
def edit_photo(dir_key, filename):
    dir_path = DIRS.get(dir_key)
    if not dir_path:
        abort(404)
    full = os.path.join(dir_path, filename)
    if not os.path.exists(full):
        abort(404)
    try:
        img = Image.open(full)
        w, h = img.size
        size_mb = round(os.path.getsize(full) / 1024 / 1024, 1)
    except Exception:
        w, h, size_mb = 0, 0, 0
    return render_template('editor.html',
        dir_key=dir_key, filename=filename,
        width=w, height=h, size_mb=size_mb,
        thumb_url=f'/thumb/{dir_key}/{filename}',
        photo_url=f'/photo/{dir_key}/{filename}')

@app.route('/api/process', methods=['POST'])
@login_required
def process_image():
    """Apply transformations to an image and save (optionally generate FM+A5)."""
    data = request.json
    dir_key  = data.get('dir')
    filename = data.get('filename')
    ops      = data.get('ops', {})   # rotate, brightness, contrast, crop, resize
    save_as  = data.get('save_as', 'replace')  # replace | fm | a5 | both

    dir_path = DIRS.get(dir_key)
    if not dir_path:
        return jsonify({'error': 'Invalid dir'}), 400
    full = os.path.join(dir_path, filename)
    if not os.path.exists(full):
        return jsonify({'error': 'File not found'}), 404

    try:
        from PIL import ImageEnhance
        img = Image.open(full)
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')

        # Rotation (90/180/270)
        angle = int(ops.get('rotate', 0))
        if angle:
            img = img.rotate(-angle, expand=True)

        # Straighten (fine angle, ±15°) — rotate then auto-crop black triangles
        st_angle = float(ops.get('straighten', 0))
        if st_angle:
            import math
            orig_w, orig_h = img.size
            img = img.rotate(st_angle, resample=Image.BICUBIC, expand=True, fillcolor=(255,255,255))
            # Auto-crop to remove white corners using inner rectangle formula
            rad = abs(math.radians(st_angle))
            cos_a, sin_a = math.cos(rad), math.sin(rad)
            w, h = img.size
            scale = 1.0 / (cos_a + sin_a * orig_h / orig_w)
            nw = int(orig_w * scale)
            nh = int(orig_h * scale)
            nw = min(nw, w); nh = min(nh, h)
            left = (w - nw) // 2; top = (h - nh) // 2
            img = img.crop((left, top, left + nw, top + nh))

        # Crop: {left, top, right, bottom} in % 0-100
        crop = ops.get('crop')
        if crop:
            w, h = img.size
            box = (
                int(crop['left'] / 100 * w),
                int(crop['top']  / 100 * h),
                int(crop['right']/ 100 * w),
                int(crop['bottom']/100 * h),
            )
            img = img.crop(box)

        # Brightness (1.0 = normal)
        br = float(ops.get('brightness', 1.0))
        if br != 1.0:
            img = ImageEnhance.Brightness(img).enhance(br)

        # Contrast
        ct = float(ops.get('contrast', 1.0))
        if ct != 1.0:
            img = ImageEnhance.Contrast(img).enhance(ct)

        # Saturation
        sat = float(ops.get('saturation', 1.0))
        if sat != 1.0:
            img = ImageEnhance.Color(img).enhance(sat)

        results = []

        def _save_jpg(dest_img, dest_path, max_w=None, quality=92):
            out = dest_img.copy()
            if max_w and out.size[0] > max_w:
                ratio = max_w / out.size[0]
                out = out.resize((max_w, int(out.size[1] * ratio)), Image.LANCZOS)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            out.save(dest_path, 'JPEG', quality=quality)
            return dest_path

        base = os.path.splitext(filename)[0]

        if save_as in ('replace', 'both'):
            dest = os.path.join(dir_path, filename)
            # If TIFF source, save as JPG next to it
            if filename.lower().endswith(('.tif', '.tiff')):
                dest = os.path.join(dir_path, base + '.jpg')
            _save_jpg(img, dest)
            results.append(dest.replace(dir_path, '').lstrip('/'))

        if save_as in ('fm', 'both'):
            fm_path = os.path.join(DIRS['FM'], base + '_MAIN.jpg')
            _save_jpg(img, fm_path, max_w=3307, quality=92)
            results.append('FM/' + base + '_MAIN.jpg')

        if save_as in ('a5', 'both'):
            a5_path = os.path.join(DIRS['A5'], base + '_MAIN_A5.jpg')
            _save_jpg(img, a5_path, max_w=1980, quality=88)
            results.append('A5/' + base + '_MAIN_A5.jpg')

        # Purge thumb cache for this file
        for d in DIRS:
            cache = f'/app/data/thumbs/{d}/{filename}.jpg'
            if os.path.exists(cache):
                os.remove(cache)

        return jsonify({'ok': True, 'saved': results})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Suffix to append to id_name for each Odoo field
FIELD_SUFFIX = {
    'main_super_picture_hd': '_MAIN',
    'main_lowdef':           '_MAIN_LOWDEF',
    'main_picture_hd':       '_MAIN',
    'main_web_picture':      '_MAIN',
    'main_a6_picture':       '_MAIN_A5',
    'main_hd_web_picture':   '_MAIN',
    'other_url':             '_OTHER',
    'detail_1_url':          '_DETAIL_1',
    'detail_2_url':          '_DETAIL_2',
    'back_pictures':         '_BACK',
    'frame_picture':         '_FRAME',
    'perspective_url':       '_PERS',
    'in_situ_url':           '_INSITU',
    'right_picture':         '_RIGHT',
    'left_picture':          '_LEFT',
    'signatures_pictures':   '_SIGNATURE',
    'edition_number_url':    '_EDNO',
}

@app.route('/api/upload_to_field', methods=['POST'])
@login_required
def upload_to_field():
    """Upload a photo, save to FM with correct name, assign to Odoo field."""
    id_name = request.form.get('id_name', '').upper()
    field   = request.form.get('field')
    f       = request.files.get('file')

    if not id_name or not field or not f:
        return jsonify({'ok': False, 'error': 'Paramètres manquants'}), 400

    valid_fields = [fn for fn, _ in ODOO_FIELDS] + MAINPICS_ALL_FIELDS
    if field not in valid_fields:
        return jsonify({'ok': False, 'error': 'Champ invalide'}), 400

    db  = get_db()
    row = db.execute("SELECT odoo_id FROM artworks WHERE id_name=?", (id_name,)).fetchone()
    db.close()
    if not row or not row['odoo_id']:
        return jsonify({'ok': False, 'error': 'Œuvre sans ID Odoo'}), 400

    fm_dir = DIRS.get('FM')
    if not fm_dir or not os.path.isdir(fm_dir):
        return jsonify({'ok': False, 'error': 'Répertoire FM introuvable'}), 500

    # Generate unique filename
    suffix    = FIELD_SUFFIX.get(field, '_' + field.upper())
    base      = f'{id_name}{suffix}'
    dest_name = f'{base}.jpg'
    counter   = 2
    while os.path.exists(os.path.join(fm_dir, dest_name)):
        dest_name = f'{base}_{counter}.jpg'
        counter  += 1

    # Convert & save as JPEG
    try:
        from PIL import ImageEnhance
        img = Image.open(f.stream)
        if img.mode in ('RGBA', 'P', 'LA', 'CMYK'):
            img = img.convert('RGB')
        img.save(os.path.join(fm_dir, dest_name), 'JPEG', quality=92)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Erreur image : {e}'}), 500

    # Build public URL and assign to Odoo
    photo_url = request.host_url.rstrip('/') + f'/photo/FM/{dest_name}'
    try:
        odoo_call('product.template', 'write',
                  [[int(row['odoo_id'])], {field: photo_url}])
        log_action('upload_assign', id_name=id_name, odoo_id=row['odoo_id'],
                   field=field, filename=dest_name, detail=photo_url)
    except Exception as e:
        log_action('upload_odoo_fail', id_name=id_name, field=field,
                   filename=dest_name, detail=str(e))
        return jsonify({'ok': True, 'filename': dest_name,
                        'url': f'/photo/FM/{dest_name}',
                        'warning': f'Fichier sauvé mais Odoo KO : {e}'})

    return jsonify({'ok': True, 'filename': dest_name, 'url': f'/photo/FM/{dest_name}'})


@app.route('/api/nas_to_fm', methods=['POST'])
@login_required
def nas_to_fm():
    """Copy a NAS file to FM dir, assign to Odoo main_super_picture_hd, push to FM."""
    import shutil
    data     = request.json or {}
    id_name  = (data.get('id_name') or '').upper()
    nas_path = data.get('nas_path', '')   # relative path inside MEDIA dir, e.g. nas/[ID]_Title.tif
    field    = data.get('field', 'main_super_picture_hd')

    if not id_name or not nas_path:
        return jsonify({'ok': False, 'error': 'Paramètres manquants'}), 400
    if field not in [f for f, _ in ODOO_FIELDS]:
        return jsonify({'ok': False, 'error': 'Champ invalide'}), 400

    src = os.path.join(DIRS['MEDIA'], nas_path)
    if not os.path.isfile(src):
        return jsonify({'ok': False, 'error': 'Fichier NAS introuvable'}), 404

    fm_dir = DIRS.get('FM')
    if not fm_dir or not os.path.isdir(fm_dir):
        return jsonify({'ok': False, 'error': 'Répertoire FM introuvable'}), 500

    db  = get_db()
    row = db.execute("SELECT odoo_id FROM artworks WHERE id_name=?", (id_name,)).fetchone()
    db.close()
    if not row or not row['odoo_id']:
        return jsonify({'ok': False, 'error': 'Œuvre sans ID Odoo'}), 400

    suffix    = FIELD_SUFFIX.get(field, '_MAIN')
    base      = f'{id_name}{suffix}'
    dest_name = f'{base}.jpg'
    counter   = 2
    while os.path.exists(os.path.join(fm_dir, dest_name)):
        dest_name = f'{base}_{counter}.jpg'
        counter  += 1

    try:
        img = Image.open(src)
        if img.mode in ('RGBA', 'P', 'LA', 'CMYK'):
            img = img.convert('RGB')
        img.save(os.path.join(fm_dir, dest_name), 'JPEG', quality=92)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Erreur conversion : {e}'}), 500

    photo_url = request.host_url.rstrip('/') + f'/photo/FM/{dest_name}'
    warning   = None
    try:
        odoo_call('product.template', 'write',
                  [[int(row['odoo_id'])], {field: photo_url}])
        log_action('nas_to_fm', id_name=id_name, odoo_id=row['odoo_id'],
                   field=field, filename=dest_name, detail=photo_url)
    except Exception as e:
        warning = f'Odoo KO : {e}'

    # Push to FM
    fm_pushed = False
    try:
        fm_f = ODOO_TO_FM.get(field)
        db2  = get_db()
        fm_row = db2.execute("SELECT record_id FROM fm_cache WHERE id_name=?", (id_name,)).fetchone()
        db2.close()
        if fm_f and fm_row and fm_row['record_id']:
            token = _fm_get_token()
            if token:
                fm_pushed = _fm_update_record(token, fm_row['record_id'], {fm_f: photo_url})
                try:
                    requests.delete(f'{FM_URL}/databases/{FM_DB}/sessions/{token}', timeout=5, verify=False)
                except Exception:
                    pass
                if fm_pushed:
                    db3 = get_db()
                    db3.execute(f"UPDATE fm_cache SET {fm_f}=? WHERE id_name=?", (photo_url, id_name))
                    db3.commit()
                    db3.close()
    except Exception:
        pass

    return jsonify({
        'ok':       True,
        'filename': dest_name,
        'url':      f'/photo/FM/{dest_name}',
        'photo_url': photo_url,
        'fm_pushed': fm_pushed,
        'warning':  warning,
    })


@app.route('/api/upload_replace', methods=['POST'])
@login_required
def api_upload_replace():
    """Upload a new file to replace an existing one, with automatic backup."""
    dir_key  = request.form.get('dir')
    filename = request.form.get('filename')   # original filename to replace (or new name)
    f        = request.files.get('file')

    if not f:
        return jsonify({'ok': False, 'error': 'Aucun fichier'}), 400
    if dir_key not in DIRS:
        return jsonify({'ok': False, 'error': 'Répertoire invalide'}), 400

    dir_path = DIRS[dir_key]
    if not os.path.isdir(dir_path):
        return jsonify({'ok': False, 'error': f'Répertoire introuvable'}), 400

    ALLOWED = {'.jpg', '.jpeg', '.png', '.tif', '.tiff'}
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED:
        return jsonify({'ok': False, 'error': f'Format non supporté : {ext}'}), 400

    # Use provided filename or uploaded filename
    dest_name = filename if filename else f.filename
    dest_path = os.path.join(dir_path, dest_name)

    backup_name = None
    if os.path.exists(dest_path):
        # Backup: put in _backups/ subdir with timestamp
        backup_dir = os.path.join(dir_path, '_backups')
        os.makedirs(backup_dir, exist_ok=True)
        from datetime import datetime
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        base, orig_ext = os.path.splitext(dest_name)
        backup_name = f'{base}_{ts}{orig_ext}'
        import shutil
        shutil.copy2(dest_path, os.path.join(backup_dir, backup_name))

    f.save(dest_path)
    return jsonify({'ok': True, 'saved': dest_name, 'backup': backup_name})


@app.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    """Upload one or more photo files into a directory."""
    dir_key = request.form.get('dir')
    if dir_key not in DIRS:
        return jsonify({'ok': False, 'error': 'Répertoire invalide'}), 400
    dir_path = DIRS[dir_key]
    if not os.path.isdir(dir_path):
        return jsonify({'ok': False, 'error': f'Répertoire introuvable : {dir_path}'}), 400

    files = request.files.getlist('files')
    if not files:
        return jsonify({'ok': False, 'error': 'Aucun fichier'}), 400

    ALLOWED = {'.jpg', '.jpeg', '.png', '.tif', '.tiff'}
    saved = []
    errors = []
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED:
            errors.append(f'{f.filename} : format non supporté')
            continue
        dest = os.path.join(dir_path, f.filename)
        if os.path.exists(dest):
            errors.append(f'{f.filename} : fichier existant (non écrasé)')
            continue
        f.save(dest)
        saved.append(f.filename)

    return jsonify({'ok': True, 'saved': saved, 'errors': errors})


FM_IMAGE_FIELDS = [
    'MAINFM', 'MAIN_HD', 'MAINTIF', 'UrlMainA5',
    'BACK300', 'FRONT300', 'FRONTRIGHT300', 'LEFT300',
    'FRAME300', 'PERS300', 'DET300', 'INSITU300', 'OTHER300',
    'Signature',
]

# Global sync progress state
_fm_sync_state = {'running': False, 'total': 0, 'done': 0, 'errors': 0, 'started': None, 'last_error': ''}

def _check_local(url):
    """Check if a FM URL's file exists locally. Returns (exists, local_path)."""
    if not url:
        return False, None
    filename = url.rstrip('/').split('/')[-1]
    for dir_key, dir_path in DIRS.items():
        if os.path.isfile(os.path.join(dir_path, filename)):
            return True, f'/photo/{dir_key}/{filename}'
    return False, None

@app.route('/api/fm_status/<id_name>')
@login_required
def api_fm_status(id_name):
    """Read FM cache for an artwork and check local files."""
    db  = get_db()
    row = db.execute('SELECT * FROM fm_cache WHERE id_name=?', (id_name,)).fetchone()
    db.close()
    if not row:
        return jsonify({'error': 'Non trouvé en cache — lance une synchronisation FM', 'cached': False}), 404

    result = []
    for field in FM_IMAGE_FIELDS:
        url = row[field] or ''
        if not url:
            result.append({'field': field, 'url': '', 'local': False, 'local_path': None, 'filename': ''})
            continue
        filename = url.rstrip('/').split('/')[-1]
        local, local_path = _check_local(url)
        result.append({'field': field, 'url': url, 'filename': filename, 'local': local, 'local_path': local_path})

    return jsonify({'id_name': id_name, 'last_sync': row['last_sync'], 'fields': result, 'cached': True})


def _do_fm_sync_all():
    """Background: fetch all FM Artworks records and populate fm_cache."""
    global _fm_sync_state
    _fm_sync_state.update({
        'running': True, 'done': 0, 'errors': 0,
        'started': datetime.utcnow().isoformat(),
        'last_error': '', 'total': 0,
    })

    try:
        token = _fm_get_token()
        if not token:
            _fm_sync_state['last_error'] = 'Connexion FM impossible (token=None)'
            return

        # Get total count first
        r = requests.get(
            f'{FM_URL}/databases/{FM_DB}/layouts/{FM_LAYOUT}/records?_limit=1',
            headers={'Authorization': f'Bearer {token}'},
            timeout=30, verify=False
        )
        total = r.json().get('response', {}).get('dataInfo', {}).get('foundCount', 0)
        _fm_sync_state['total'] = total
        if not total:
            _fm_sync_state['last_error'] = 'FM a renvoyé foundCount=0'

        batch  = 500
        offset = 1  # FM Data API: _offset is 1-indexed; 0 returns HTTP 400 code 960
        cols   = ','.join(FM_IMAGE_FIELDS)

        while offset <= total:
            try:
                r = requests.get(
                    f'{FM_URL}/databases/{FM_DB}/layouts/{FM_LAYOUT}/records'
                    f'?_limit={batch}&_offset={offset}',
                    headers={'Authorization': f'Bearer {token}'},
                    timeout=60, verify=False
                )
                if r.status_code != 200:
                    msgs = r.json().get('messages', [{}])
                    _fm_sync_state['last_error'] = f'HTTP {r.status_code} @offset={offset}: {msgs}'
                    print(f'[fm_sync] {_fm_sync_state["last_error"]}', flush=True)
                    break
                data = r.json().get('response', {}).get('data', [])
                if not data:
                    break

                db = get_db()
                for rec in data:
                    fd      = rec.get('fieldData', {})
                    id_name = fd.get('IdName', '')
                    if not id_name:
                        continue
                    vals = [fd.get(f, '') or '' for f in FM_IMAGE_FIELDS]
                    db.execute(
                        f'''INSERT INTO fm_cache (id_name, record_id, {cols}, last_sync)
                            VALUES (?, ?, {','.join('?'*len(FM_IMAGE_FIELDS))}, datetime('now'))
                            ON CONFLICT(id_name) DO UPDATE SET
                            record_id=excluded.record_id,
                            {', '.join(f"{f}=excluded.{f}" for f in FM_IMAGE_FIELDS)},
                            last_sync=excluded.last_sync''',
                        [id_name, rec.get('recordId', '')] + vals
                    )
                    _fm_sync_state['done'] += 1
                db.commit()
                db.close()

                offset += batch
                if len(data) < batch:
                    break

            except Exception as e:
                _fm_sync_state['errors'] += 1
                _fm_sync_state['last_error'] = f'batch @offset={offset}: {e}'
                print(f'[fm_sync] {_fm_sync_state["last_error"]}', flush=True)
                offset += batch

        # Refresh token if needed (long sync)
        try:
            requests.delete(f'{FM_URL}/databases/{FM_DB}/sessions/{token}', timeout=5, verify=False)
        except Exception:
            pass

    except Exception as e:
        _fm_sync_state['last_error'] = f'fatal: {e}'
        print(f'[fm_sync] fatal: {e}', flush=True)
    finally:
        _fm_sync_state['running'] = False


@app.route('/api/fm_sync_all', methods=['POST'])
@login_required
def api_fm_sync_all():
    if _fm_sync_state['running']:
        return jsonify({'error': 'Sync déjà en cours', 'state': _fm_sync_state})
    t = threading.Thread(target=_do_fm_sync_all, daemon=True)
    t.start()
    return jsonify({'ok': True, 'message': 'Synchronisation FM lancée en background'})


@app.route('/api/fm_sync_progress')
@login_required
def api_fm_sync_progress():
    db    = get_db()
    cached = db.execute('SELECT COUNT(*) FROM fm_cache').fetchone()[0]
    db.close()
    return jsonify({**_fm_sync_state, 'cached': cached})


@app.route('/api/activity_log')
@login_required
def api_activity_log():
    limit  = int(request.args.get('limit', 200))
    offset = int(request.args.get('offset', 0))
    db   = get_db()
    rows = db.execute(
        'SELECT * FROM activity_log ORDER BY ts DESC LIMIT ? OFFSET ?',
        (limit, offset)
    ).fetchall()
    total = db.execute('SELECT COUNT(*) FROM activity_log').fetchone()[0]
    db.close()
    return jsonify({'total': total, 'rows': [dict(r) for r in rows]})

@app.route('/log')
@login_required
def log_page():
    return render_template('log.html')

@app.route('/fm_sync')
@login_required
def fm_sync_page():
    return render_template('fm_sync.html')

# ── File metadata scan ────────────────────────────────────────────────────────
_scan_meta_state = {'running': False, 'done': 0, 'total': 0}

def _do_scan_file_meta():
    global _scan_meta_state
    _scan_meta_state.update({'running': True, 'done': 0, 'total': 0})
    try:
        # Collect all files
        all_files = []
        for dir_key, dir_path in DIRS.items():
            if not os.path.isdir(dir_path): continue
            for fn in os.listdir(dir_path):
                if fn.lower().endswith(('.jpg','.jpeg','.png','.tif','.tiff')):
                    all_files.append((dir_key, dir_path, fn))
        _scan_meta_state['total'] = len(all_files)

        db = get_db()
        for dir_key, dir_path, fn in all_files:
            full = os.path.join(dir_path, fn)
            path = f'{dir_key}/{fn}'
            m = re.match(r'^([A-Z]+-\d+)', fn)
            id_name = m.group(1) if m else ''
            try:
                fsize = os.path.getsize(full)
                with Image.open(full) as img:
                    w, h = img.size
            except Exception:
                fsize, w, h = 0, 0, 0
            db.execute('''INSERT INTO file_meta (path, dir, filename, id_name, width, height, filesize, scanned_at)
                VALUES (?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT(path) DO UPDATE SET width=excluded.width, height=excluded.height,
                filesize=excluded.filesize, scanned_at=excluded.scanned_at''',
                (path, dir_key, fn, id_name, w, h, fsize))
            _scan_meta_state['done'] += 1
            if _scan_meta_state['done'] % 500 == 0:
                db.commit()
        db.commit()
        db.close()
    except Exception:
        pass
    finally:
        _scan_meta_state['running'] = False

@app.route('/api/scan_file_meta', methods=['POST'])
@login_required
def api_scan_file_meta():
    if _scan_meta_state['running']:
        return jsonify({'error': 'Scan déjà en cours'})
    threading.Thread(target=_do_scan_file_meta, daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/scan_meta_progress')
@login_required
def api_scan_meta_progress():
    db = get_db()
    cached = db.execute('SELECT COUNT(*) FROM file_meta').fetchone()[0]
    db.close()
    return jsonify({**_scan_meta_state, 'cached': cached})

# ── FM Audit ──────────────────────────────────────────────────────────────────
AUDIT_FIELDS = ['MAINFM','BACK300','PERS300','FRAME300','DET300','INSITU300','UrlMainA5']
# FM field → Odoo field (for audit comparison)
FM_TO_ODOO_AUDIT = {
    'MAINFM':    'main_super_picture_hd',
    'MAIN_HD':   'main_picture_hd',
    'UrlMainA5': 'main_a6_picture',
    'BACK300':   'back_pictures',
    'FRAME300':  'frame_picture',
    'PERS300':   'perspective_url',
    'Signature': 'signatures_pictures',
    'OTHER300':  'other_url',
    'DET300':    'detail_1_url',
}
AUDIT_COMPARE = [
    ('MAINFM',    'main_super_picture_hd', 'Main'),
    ('MAIN_HD',   'main_picture_hd',       'HD'),
    ('UrlMainA5', 'main_a6_picture',       'A6'),
    ('BACK300',   'back_pictures',         'Back'),
    ('FRAME300',  'frame_picture',         'Cadre'),
    ('PERS300',   'perspective_url',       'Pers'),
    ('Signature', 'signatures_pictures',   'Sign'),
    ('OTHER300',  'other_url',             'Autre'),
    ('DET300',    'detail_1_url',          'Dét'),
]

@app.route('/fm_audit')
@login_required
def fm_audit_page():
    return render_template('fm_audit.html')

@app.route('/api/fm_audit')
@login_required
def api_fm_audit():
    page    = max(1, int(request.args.get('page', 1)))
    limit   = min(200, int(request.args.get('limit', 100)))
    q       = request.args.get('q', '').strip()
    missing = request.args.get('missing', '')
    pending = request.args.get('pending', '')
    gallery = request.args.get('gallery', '').strip()
    max_px  = int(request.args.get('max_px', 0))
    sort    = request.args.get('sort', '')
    statuses = [s for s in request.args.get('statuses', '').split(',') if s]
    diff_filter = request.args.get('diff', '')  # 'odoo_only' | 'fm_only' | 'any'

    db = get_db()
    has_odoo_cache = _table_exists(db, 'odoo_photos_cache')

    where, params = ['1=1'], []
    if q:
        where.append('(f.id_name LIKE ? OR a.artist LIKE ? OR a.title LIKE ? OR a.gallery LIKE ?)')
        params += [f'%{q}%']*4
    if missing:
        where.append(f"(f.{missing} IS NULL OR f.{missing}='')")
    if pending == '1':
        where.append('COALESCE(pend.pending,0) > 0')
    if gallery:
        where.append('a.gallery = ?')
        params.append(gallery)
    if statuses:
        placeholders = ','.join('?' for _ in statuses)
        where.append(f'LOWER(COALESCE(a.status,"")) IN ({placeholders})')
        params += [s.lower() for s in statuses]
    if max_px > 0:
        where.append('fm_w.width > 0 AND fm_w.width <= ?')
        params.append(max_px)

    # Diff filter: odoo rempli / FM vide
    if diff_filter == 'odoo_only' and has_odoo_cache:
        odoo_conds = ' OR '.join(
            f"(COALESCE(o.{of},'')!='' AND COALESCE(f.{ff},'')='')"
            for ff, of, _ in AUDIT_COMPARE
        )
        where.append(f'({odoo_conds})')
    elif diff_filter == 'fm_only' and has_odoo_cache:
        fm_conds = ' OR '.join(
            f"(COALESCE(f.{ff},'')!='' AND COALESCE(o.{of},'')='')"
            for ff, of, _ in AUDIT_COMPARE
        )
        where.append(f'({fm_conds})')
    elif diff_filter == 'any' and has_odoo_cache:
        any_conds = ' OR '.join(
            f"(COALESCE(f.{ff},'')!=COALESCE(o.{of},'') AND (COALESCE(f.{ff},'')!='' OR COALESCE(o.{of},'')!=''))"
            for ff, of, _ in AUDIT_COMPARE
        )
        where.append(f'({any_conds})')

    where_sql = ' AND '.join(where)

    sort_map = {
        'width_desc':    'fm_w.width DESC',
        'width_asc':     'CASE WHEN fm_w.width IS NULL OR fm_w.width=0 THEN 1 ELSE 0 END, fm_w.width ASC',
        'filesize_desc': 'fm_w.filesize DESC',
        'filesize_asc':  'CASE WHEN fm_w.filesize IS NULL OR fm_w.filesize=0 THEN 1 ELSE 0 END, fm_w.filesize ASC',
        'id_asc':        'f.id_name ASC',
        'gallery_asc':   'a.gallery ASC, f.id_name ASC',
        'pending_desc':  'COALESCE(pend.pending,0) DESC, f.id_name ASC',
    }
    order = sort_map.get(sort, 'COALESCE(pend.pending,0) DESC, f.id_name ASC')

    odoo_join = "LEFT JOIN odoo_photos_cache o ON o.id_name = f.id_name" if has_odoo_cache else ""
    odoo_select = ", o.main_super_picture_hd, o.main_picture_hd, o.main_a6_picture, o.back_pictures, o.frame_picture, o.perspective_url, o.signatures_pictures, o.other_url, o.detail_1_url" if has_odoo_cache else ""

    base = f'''
        FROM fm_cache f
        LEFT JOIN artworks a ON a.id_name = f.id_name
        {odoo_join}
        LEFT JOIN (SELECT id_name, COUNT(*) as pending FROM activity_log
                   WHERE synced_fm=0 AND id_name!='' GROUP BY id_name) pend ON pend.id_name=f.id_name
        LEFT JOIN (SELECT id_name, width, height, filesize FROM file_meta
                   WHERE dir='FM' GROUP BY id_name HAVING MAX(width*height)) fm_w ON fm_w.id_name=f.id_name
        WHERE {where_sql}'''

    total = db.execute(f'SELECT COUNT(*) {base}', params).fetchone()[0]
    rows  = db.execute(
        f'''SELECT f.id_name, a.artist, a.title, a.status, a.odoo_id,
                   COALESCE(a.gallery,'') as gallery, COALESCE(a.location,'') as location,
                   f.MAINFM, f.MAIN_HD, f.BACK300, f.PERS300, f.FRAME300, f.DET300,
                   f.INSITU300, f.UrlMainA5, f.Signature, f.OTHER300,
                   f.last_sync, COALESCE(pend.pending,0) as pending,
                   COALESCE(fm_w.width,0) as width, COALESCE(fm_w.height,0) as height,
                   COALESCE(fm_w.filesize,0) as filesize
                   {odoo_select}
            {base} ORDER BY {order} LIMIT ? OFFSET ?''',
        params + [limit, (page-1)*limit]).fetchall()

    stats = db.execute('''SELECT COUNT(*) as total,
        SUM(CASE WHEN MAINFM!='' AND MAINFM IS NOT NULL THEN 1 ELSE 0 END) as mainfm,
        SUM(CASE WHEN BACK300!='' AND BACK300 IS NOT NULL THEN 1 ELSE 0 END) as back300,
        SUM(CASE WHEN PERS300!='' AND PERS300 IS NOT NULL THEN 1 ELSE 0 END) as pers300,
        SUM(CASE WHEN FRAME300!='' AND FRAME300 IS NOT NULL THEN 1 ELSE 0 END) as frame300,
        SUM(CASE WHEN DET300!='' AND DET300 IS NOT NULL THEN 1 ELSE 0 END) as det300,
        SUM(CASE WHEN INSITU300!='' AND INSITU300 IS NOT NULL THEN 1 ELSE 0 END) as insitu300,
        SUM(CASE WHEN UrlMainA5!='' AND UrlMainA5 IS NOT NULL THEN 1 ELSE 0 END) as urla5
        FROM fm_cache''').fetchone()
    pending_count = db.execute(
        "SELECT COUNT(DISTINCT id_name) FROM activity_log WHERE synced_fm=0 AND id_name!=''").fetchone()[0]

    galleries = [r[0] for r in db.execute(
        "SELECT DISTINCT gallery FROM artworks WHERE gallery IS NOT NULL AND gallery!='' ORDER BY gallery"
    ).fetchall()]
    gallery_synced = db.execute(
        "SELECT COUNT(*) FROM artworks WHERE gallery IS NOT NULL AND gallery!=''"
    ).fetchone()[0]

    def row_compare(r):
        if not has_odoo_cache:
            return None
        rd = dict(r)
        result = {}
        for fm_f, odoo_f, label in AUDIT_COMPARE:
            fv = (rd.get(fm_f) or '').strip()
            ov = (rd.get(odoo_f) or '').strip()
            if fv and ov:      st = 'ok' if fv == ov else 'both'
            elif fv and not ov: st = 'fm_only'
            elif ov and not fv: st = 'odoo_only'
            else:               st = 'missing'
            result[fm_f] = {'st': st, 'fm': fv, 'odoo': ov, 'label': label}
        return result

    db.close()
    return jsonify({
        'total': total, 'page': page,
        'pages': max(1, (total+limit-1)//limit),
        'gallery_synced': gallery_synced,
        'galleries': galleries,
        'has_odoo_cache': has_odoo_cache,
        'stats': {
            'total': stats['total'], 'pending': pending_count,
            'fields': {f: stats[{'UrlMainA5':'urla5'}.get(f, f.lower())] or 0 for f in AUDIT_FIELDS},
        },
        'rows': [{
            'id_name':   r['id_name'],
            'artist':    r['artist'] or '',
            'title':     r['title'] or '',
            'status':    r['status'] or '',
            'gallery':   r['gallery'],
            'location':  r['location'],
            'odoo_id':   r['odoo_id'],
            'pending':   r['pending'],
            'width':     r['width'],
            'height':    r['height'],
            'filesize':  r['filesize'],
            'last_sync': r['last_sync'] or '',
            'fields':    {f: bool(r[f]) for f in AUDIT_FIELDS},
            'compare':   row_compare(r),
        } for r in rows]
    })

# ── Gallery sync from Odoo ────────────────────────────────────────────────────
_gallery_sync_state = {'running': False, 'done': 0, 'total': 0}

def _do_sync_galleries():
    global _gallery_sync_state
    _gallery_sync_state.update({'running': True, 'done': 0})
    try:
        db = get_db()
        rows = db.execute("SELECT odoo_id FROM artworks WHERE odoo_id IS NOT NULL").fetchall()
        odoo_ids = [r['odoo_id'] for r in rows]
        _gallery_sync_state['total'] = len(odoo_ids)
        batch_size = 500
        for i in range(0, len(odoo_ids), batch_size):
            batch = odoo_ids[i:i+batch_size]
            try:
                result = odoo_call('product.template', 'read',
                                   [batch, ['id','control_gallery_combined','Location']])
                for rec in result:
                    gal = rec.get('control_gallery_combined') or ''
                    if isinstance(gal, list): gal = gal[1] if len(gal)>1 else ''
                    loc = rec.get('Location') or ''
                    db.execute("UPDATE artworks SET gallery=?, location=? WHERE odoo_id=?",
                               (gal, loc, rec['id']))
                _gallery_sync_state['done'] += len(batch)
                db.commit()
            except Exception:
                _gallery_sync_state['done'] += len(batch)
        db.close()
    except Exception:
        pass
    finally:
        _gallery_sync_state['running'] = False

@app.route('/api/sync_galleries', methods=['POST'])
@login_required
def api_sync_galleries():
    if _gallery_sync_state['running']:
        return jsonify({'error': 'Sync déjà en cours'})
    threading.Thread(target=_do_sync_galleries, daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/sync_galleries_progress')
@login_required
def api_sync_galleries_progress():
    return jsonify(_gallery_sync_state)

# ── FM Audit export ───────────────────────────────────────────────────────────
@app.route('/api/fm_audit/export')
@login_required
def api_fm_audit_export():
    """Export CSV — standard ou rapport qualité groupé par galerie."""
    import csv, io as _io
    q        = request.args.get('q','').strip()
    missing  = request.args.get('missing','')
    pending  = request.args.get('pending','')
    min_px   = int(request.args.get('min_px', 0))
    max_px   = int(request.args.get('max_px', 0))
    mode     = request.args.get('mode', 'full')   # 'full' ou 'quality'
    statuses = [s for s in request.args.get('statuses', '').split(',') if s]

    db = get_db()
    where, params = ['1=1'], []
    if q:
        where.append('(f.id_name LIKE ? OR a.artist LIKE ? OR a.title LIKE ?)')
        params += [f'%{q}%']*3
    if missing:
        where.append(f"(f.{missing} IS NULL OR f.{missing}='')")
    if pending == '1':
        where.append('COALESCE(pend.pending,0)>0')
    if max_px > 0:
        where.append('(fm_w.width IS NOT NULL AND fm_w.width <= ?)')
        params.append(max_px)
    if statuses:
        placeholders = ','.join('?' for _ in statuses)
        where.append(f'LOWER(COALESCE(a.status,"")) IN ({placeholders})')
        params += [s.lower() for s in statuses]

    rows = db.execute(f'''
        SELECT f.id_name, a.artist, a.title, a.status, a.odoo_id,
               COALESCE(a.gallery,'') as gallery, COALESCE(a.location,'') as location,
               f.MAINFM, f.BACK300, f.PERS300, f.FRAME300, f.DET300, f.INSITU300, f.UrlMainA5,
               COALESCE(pend.pending,0) as pending,
               COALESCE(fm_w.width,0) as width, COALESCE(fm_w.height,0) as height,
               COALESCE(fm_w.filesize,0) as filesize
        FROM fm_cache f
        LEFT JOIN artworks a ON a.id_name=f.id_name
        LEFT JOIN (SELECT id_name,COUNT(*) as pending FROM activity_log
                   WHERE synced_fm=0 AND id_name!='' GROUP BY id_name) pend ON pend.id_name=f.id_name
        LEFT JOIN (SELECT id_name,width,height,filesize FROM file_meta WHERE dir='FM'
                   GROUP BY id_name HAVING MAX(width*height)) fm_w ON fm_w.id_name=f.id_name
        WHERE {' AND '.join(where)}
        ORDER BY a.gallery, f.id_name''', params).fetchall()
    db.close()

    buf = _io.StringIO()
    w = csv.writer(buf)

    if mode == 'quality':
        # Rapport groupé par galerie pour demande de reshot
        from collections import defaultdict
        by_gallery = defaultdict(list)
        for r in rows:
            by_gallery[r['gallery'] or '(Galerie inconnue)'].append(r)

        w.writerow(['=== RAPPORT QUALITÉ PHOTOS — ' + datetime.utcnow().strftime('%Y-%m-%d') + ' ==='])
        w.writerow([])
        for gallery, items in sorted(by_gallery.items()):
            w.writerow([f'GALERIE : {gallery}', f'{len(items)} artwork(s) concerné(s)'])
            w.writerow(['ID', 'Artiste', 'Titre', 'Localisation', 'Largeur px', 'Poids Ko', 'MAINFM URL'])
            for r in items:
                w.writerow([
                    r['id_name'], r['artist'] or '', r['title'] or '',
                    r['location'] or '', r['width'] or '—',
                    round(r['filesize']/1024,1) if r['filesize'] else '—',
                    r['MAINFM'] or ''
                ])
            w.writerow([])
        fname = 'rapport_qualite_photos.csv'
    else:
        w.writerow(['ID','Artiste','Titre','Statut','Galerie','Localisation',
                    'Pending Sync','MAINFM','BACK300','PERS300','FRAME300','DET300','INSITU300','UrlMainA5',
                    'Largeur px','Hauteur px','Poids Ko'])
        for r in rows:
            w.writerow([
                r['id_name'], r['artist'] or '', r['title'] or '', r['status'] or '',
                r['gallery'], r['location'], r['pending'],
                '✓' if r['MAINFM'] else '', '✓' if r['BACK300'] else '',
                '✓' if r['PERS300'] else '', '✓' if r['FRAME300'] else '',
                '✓' if r['DET300'] else '', '✓' if r['INSITU300'] else '',
                '✓' if r['UrlMainA5'] else '',
                r['width'] or '', r['height'] or '',
                round(r['filesize']/1024,1) if r['filesize'] else '',
            ])
        fname = 'fm_audit.csv'

    buf.seek(0)
    return Response(buf.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={fname}'})

@app.route('/howto')
@login_required
def howto_page():
    return render_template('howto.html')

# ── FM sync background thread ─────────────────────────────────────────────────
import threading, time as _time, base64

FM_URL          = os.environ.get('FM_URL', '')
FM_DB           = os.environ.get('FM_DB', '')
FM_LOGIN        = os.environ.get('FM_LOGIN', '')
FM_PASSWORD     = os.environ.get('FM_PASSWORD', '')
FM_LAYOUT       = os.environ.get('FM_LAYOUT', 'Artworks')
FM_PHOTOS_LAYOUT = os.environ.get('FM_PHOTOS_LAYOUT', 'CRMPhotos')

# Fields routed to CRMPhotos table (no dedicated FM artwork field)
FM_PHOTOS_TABLE_FIELDS = {'edition_number_url'}

# Mapping: role (from filename suffix / odoo field) → FM field name
FM_FIELD_MAP = {
    # Odoo field → FM field
    'main_super_picture_hd': 'MAINFM',
    'frame_picture':         'FRAME300',
    'back_pictures':         'BACK300',
    'in_situ_url':           'INSITU300',
    'perspective_url':       'PERS300',
    'right_picture':         'RIGHT300',
    'left_picture':          'LEFT300',
    'signatures_pictures':   'Signature',
    'detail_1_url':          'DET300',
    'detail_2_url':          'DET2300',
    'other_url':             'OTHER300',
    # Filename suffix fallbacks
    'MAIN':      'MAINFM',
    'RECTO':     'MAINFM',
    'BACK':      'BACK300',
    'VERSO':     'BACK300',
    'FRAME':     'FRAME300',
    'PERS':      'PERS300',
    'SIGNATURE': 'Signature',
    'OTHER':     'OTHER300',
    'INSITU':    'INSITU300',
    'IN_SITU':   'INSITU300',
    'DET':       'DET300',
    'LEFT':      'LEFT300',
    'RIGHT':     'RIGHT300',
    'FRONT':     'FRONT300',
}

def _fm_get_token():
    """Authenticate to FM Data API, return token or empty string."""
    try:
        r = requests.post(
            f'{FM_URL}/databases/{FM_DB}/sessions',
            headers={'Content-Type': 'application/json',
                     'Authorization': 'Basic ' + base64.b64encode(
                         f'{FM_LOGIN}:{FM_PASSWORD}'.encode()).decode()},
            json={}, timeout=10, verify=False
        )
        return r.json().get('response', {}).get('token', '')
    except Exception:
        return ''

def _fm_find_record_id(token, id_name):
    """Find the FM internal recordId for a given id_name (ARTIST-12345 format)."""
    try:
        r = requests.post(
            f'{FM_URL}/databases/{FM_DB}/layouts/{FM_LAYOUT}/_find',
            headers={'Authorization': f'Bearer {token}',
                     'Content-Type': 'application/json'},
            json={'query': [{'IdName': id_name}]},
            timeout=60, verify=False
        )
        data = r.json().get('response', {}).get('data', [])
        if data:
            return data[0].get('recordId')
    except Exception:
        pass
    return None

def _fm_update_record(token, record_id, field_data):
    """PATCH a FM record with the given fieldData dict. Returns (ok, error_msg)."""
    try:
        r = requests.patch(
            f'{FM_URL}/databases/{FM_DB}/layouts/{FM_LAYOUT}/records/{record_id}',
            headers={'Authorization': f'Bearer {token}',
                     'Content-Type': 'application/json'},
            json={'fieldData': field_data},
            timeout=10, verify=False
        )
        if r.status_code == 200:
            msgs = r.json().get('messages', [{}])
            return msgs[0].get('code', '0') == '0'
        print(f'[FM] PATCH erreur {r.status_code}: {r.text[:200]}')
        return False
    except Exception as e:
        print(f'[FM] PATCH exception: {e}')
        return False

def _fm_delete_photo_record(token, id_name, url):
    """Find and delete a CRMPhotos record matching LArtwork + Url. Returns True on success."""
    try:
        # Find record by LArtwork + Url
        r = requests.post(
            f'{FM_URL}/databases/{FM_DB}/layouts/{FM_PHOTOS_LAYOUT}/_find',
            headers={'Authorization': f'Bearer {token}',
                     'Content-Type': 'application/json'},
            json={'query': [{'LArtwork': id_name, 'Url': url}]},
            timeout=10, verify=False
        )
        data = r.json().get('response', {}).get('data', [])
        if not data:
            return True  # Already gone
        record_id = data[0].get('recordId')
        if not record_id:
            return False
        d = requests.delete(
            f'{FM_URL}/databases/{FM_DB}/layouts/{FM_PHOTOS_LAYOUT}/records/{record_id}',
            headers={'Authorization': f'Bearer {token}'},
            timeout=10, verify=False
        )
        return d.status_code == 200
    except Exception as e:
        print(f'[FM] CRMPhotos DELETE exception: {e}')
        return False


def _fm_create_photo_record(token, id_name, filename, url, publish_web=True):
    """Create a record in CRMPhotos table for an artwork. Returns True on success."""
    try:
        r = requests.post(
            f'{FM_URL}/databases/{FM_DB}/layouts/{FM_PHOTOS_LAYOUT}/records',
            headers={'Authorization': f'Bearer {token}',
                     'Content-Type': 'application/json'},
            json={'fieldData': {
                'LArtwork':    id_name,
                'NomFichier':  filename,
                'Url':         url,
                'PublishWeb':  '1' if publish_web else '0',
            }},
            timeout=10, verify=False
        )
        if r.status_code in (200, 201):
            msgs = r.json().get('messages', [{}])
            return msgs[0].get('code', '0') == '0'
        print(f'[FM] CRMPhotos POST erreur {r.status_code}: {r.text[:200]}')
        return False
    except Exception as e:
        print(f'[FM] CRMPhotos POST exception: {e}')
        return False


def _fm_sync_loop():
    """Every 15 min: push unsynced assign/upload actions to FileMaker artwork records."""
    while True:
        _time.sleep(900)   # 15 minutes
        if not all([FM_URL, FM_DB, FM_LOGIN, FM_PASSWORD]):
            continue
        try:
            db   = get_db()
            rows = db.execute(
                "SELECT * FROM activity_log WHERE synced_fm=0 AND action IN ('assign','upload_to_field','nas_to_fm') ORDER BY ts"
            ).fetchall()
            if not rows:
                # Mark irrelevant rows as synced so they don't pile up
                db.execute("UPDATE activity_log SET synced_fm=1 WHERE synced_fm=0")
                db.commit()
                db.close()
                continue

            token = _fm_get_token()
            if not token:
                db.close(); continue

            synced_ids = []
            # Group by id_name to minimise FM round-trips
            from collections import defaultdict
            by_artwork = defaultdict(list)
            for row in rows:
                by_artwork[row['id_name']].append(row)

            for id_name, artwork_rows in by_artwork.items():
                record_id = _fm_find_record_id(token, id_name)
                if not record_id:
                    synced_ids.extend(r['id'] for r in artwork_rows)
                    continue

                field_data   = {}   # direct FM artwork fields
                photos_rows  = []   # rows destined for CRMPhotos table

                for row in artwork_rows:
                    odoo_field = row['field'] or ''
                    url = (row['detail'] or '').strip()
                    if not url or url == 'cleared' or not url.startswith('http'):
                        synced_ids.append(row['id'])
                        continue

                    if odoo_field in FM_PHOTOS_TABLE_FIELDS:
                        photos_rows.append(row)
                        continue

                    fm_field = FM_FIELD_MAP.get(odoo_field)
                    if not fm_field:
                        # Fallback: guess from filename suffix
                        fn = (row['filename'] or '').upper()
                        for suffix, ff in FM_FIELD_MAP.items():
                            if fn.endswith(f'_{suffix}.JPG') or fn.endswith(f'_{suffix}.JPEG'):
                                fm_field = ff
                                break
                    if fm_field:
                        field_data[fm_field] = _maybe_convert_tiff(url, fm_field)
                    else:
                        synced_ids.append(row['id'])

                # Push direct fields in one PATCH
                if field_data:
                    ok = _fm_update_record(token, record_id, field_data)
                    if ok:
                        synced_ids.extend(r['id'] for r in artwork_rows if r['id'] not in synced_ids and (r['field'] or '') not in FM_PHOTOS_TABLE_FIELDS)
                else:
                    for row in artwork_rows:
                        if row['id'] not in synced_ids and (row['field'] or '') not in FM_PHOTOS_TABLE_FIELDS:
                            synced_ids.append(row['id'])

                # Push CRMPhotos rows one by one
                for row in photos_rows:
                    url      = (row['detail'] or '').strip()
                    filename = os.path.basename(row['filename'] or url.split('/')[-1])
                    ok = _fm_create_photo_record(token, id_name, filename, url)
                    if ok:
                        synced_ids.append(row['id'])

            # Close FM session
            try:
                requests.delete(f'{FM_URL}/databases/{FM_DB}/sessions/{token}', timeout=5, verify=False)
            except Exception:
                pass

            if synced_ids:
                db.execute(
                    f'UPDATE activity_log SET synced_fm=1 WHERE id IN ({",".join("?"*len(synced_ids))})',
                    synced_ids
                )
                db.commit()

            # Also mark non-photo actions as synced
            db.execute("UPDATE activity_log SET synced_fm=1 WHERE synced_fm=0 AND action NOT IN ('assign','upload_to_field','nas_to_fm')")
            db.commit()
            db.close()
        except Exception:
            pass

# ── Odoo ↔ FM alignment ───────────────────────────────────────────────────────
ODOO_TO_FM = {
    'main_super_picture_hd': 'MAINFM',
    'frame_picture':         'FRAME300',
    'back_pictures':         'BACK300',
    'in_situ_url':           'INSITU300',
    'perspective_url':       'PERS300',
    'right_picture':         'RIGHT300',
    'left_picture':          'LEFT300',
    'signatures_pictures':   'Signature',
    'edition_number_url':    'CRMPhotos',
    'detail_1_url':          'DET300',
    'detail_2_url':          'DET2300',
    'other_url':             'OTHER300',
}
FM_TO_ODOO = {v: k for k, v in ODOO_TO_FM.items() if v != 'CRMPhotos'}

@app.route('/sync-check')
@login_required
def sync_check_page():
    return render_template('sync_check.html')

_odoo_photo_sync_state = {'running': False, 'done': 0, 'total': 0, 'last_error': ''}

def _ensure_odoo_photos_cache():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS odoo_photos_cache (
            id_name TEXT PRIMARY KEY,
            main_super_picture_hd TEXT DEFAULT '',
            main_picture_hd       TEXT DEFAULT '',
            main_a6_picture       TEXT DEFAULT '',
            back_pictures         TEXT DEFAULT '',
            frame_picture         TEXT DEFAULT '',
            perspective_url       TEXT DEFAULT '',
            signatures_pictures   TEXT DEFAULT '',
            other_url             TEXT DEFAULT '',
            detail_1_url          TEXT DEFAULT '',
            synced_at             TEXT
        );
    ''')
    db.commit()
    db.close()

def _sync_odoo_photos_worker():
    global _odoo_photo_sync_state
    _odoo_photo_sync_state.update({'running': True, 'done': 0, 'total': 0, 'last_error': ''})
    _ensure_odoo_photos_cache()
    try:
        fields = list(ODOO_TO_FM.keys()) + ['IdName']
        all_rows, offset = [], 0
        while True:
            res = odoo_call('product.template', 'search_read',
                [[['Status','in',['In stock','In transit','On Order','On order','On hold']],
                  ['IdName','!=',False]]],
                {'fields': fields, 'limit': 500, 'offset': offset})
            if not res: break
            all_rows.extend(res)
            offset += len(res)
            if len(res) < 500: break
        _odoo_photo_sync_state['total'] = len(all_rows)
        db = get_db()
        from datetime import datetime
        now = datetime.utcnow().isoformat()
        for i, r in enumerate(all_rows):
            idn = (r.get('IdName') or '').strip()
            if not idn: continue
            db.execute('''INSERT OR REPLACE INTO odoo_photos_cache
                (id_name,main_super_picture_hd,main_picture_hd,main_a6_picture,
                 back_pictures,frame_picture,perspective_url,signatures_pictures,
                 other_url,detail_1_url,synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (idn,
                 r.get('main_super_picture_hd') or '',
                 r.get('main_picture_hd') or '',
                 r.get('main_a6_picture') or '',
                 r.get('back_pictures') or '',
                 r.get('frame_picture') or '',
                 r.get('perspective_url') or '',
                 r.get('signatures_pictures') or '',
                 r.get('other_url') or '',
                 r.get('detail_1_url') or '',
                 now))
            _odoo_photo_sync_state['done'] = i + 1
            if i % 200 == 0: db.commit()
        db.commit()
        db.close()
    except Exception as e:
        _odoo_photo_sync_state['last_error'] = str(e)
        print('odoo_photos_sync error:', e, flush=True)
    finally:
        _odoo_photo_sync_state['running'] = False

@app.route('/api/sync_check/sync_odoo', methods=['POST'])
@login_required
def api_sync_check_sync_odoo():
    if _odoo_photo_sync_state['running']:
        return jsonify({'error': 'déjà en cours'})
    threading.Thread(target=_sync_odoo_photos_worker, daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/sync_check/sync_odoo_status')
@login_required
def api_sync_check_sync_odoo_status():
    db = get_db()
    if _table_exists(db, 'odoo_photos_cache'):
        n_row    = db.execute('SELECT COUNT(*) FROM odoo_photos_cache').fetchone()
        sync_row = db.execute('SELECT MAX(synced_at) FROM odoo_photos_cache').fetchone()
        n        = n_row[0]
        synced   = sync_row[0]
    else:
        n, synced = 0, None
    db.close()
    return jsonify({**_odoo_photo_sync_state, 'cached': n, 'synced_at': synced})

def _table_exists(db, name):
    return db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0] > 0

@app.route('/api/sync_check')
@login_required
def api_sync_check():
    """Compare odoo_photos_cache vs fm_cache."""
    q             = (request.args.get('q') or '').strip()
    only_diff     = request.args.get('diff','') == '1'
    status_filter = request.args.get('status','')
    page          = max(1, int(request.args.get('page', 1)))
    per_page      = 50

    db = get_db()
    if not _table_exists(db, 'odoo_photos_cache'):
        db.close()
        return jsonify({'total':0,'page':1,'per_page':per_page,'rows':[],'need_sync':True})

    where  = "a.status IN ('In stock','In transit','On Order','On order','On hold')"
    params = []
    if q:
        where += " AND (a.id_name LIKE ? OR a.artist LIKE ? OR a.title LIKE ?)"
        params += [f'%{q}%', f'%{q}%', f'%{q}%']

    rows = db.execute(f'''
        SELECT a.id_name, a.title, a.artist, a.status, a.gallery,
               f.MAINFM, f.MAIN_HD, f.UrlMainA5, f.BACK300, f.FRAME300,
               f.PERS300, f.Signature, f.OTHER300, f.DET300, f.last_sync as fm_sync,
               o.main_super_picture_hd, o.main_picture_hd, o.main_a6_picture,
               o.back_pictures, o.frame_picture, o.perspective_url,
               o.signatures_pictures, o.other_url, o.detail_1_url,
               o.synced_at as odoo_sync
        FROM artworks a
        LEFT JOIN fm_cache f           ON f.id_name = a.id_name
        LEFT JOIN odoo_photos_cache o  ON o.id_name = a.id_name
        WHERE {where}
        ORDER BY a.id_name
    ''', params).fetchall()
    db.close()

    results = []
    for r in rows:
        rd = dict(r)
        fields_status = {}
        has_diff = False
        for odoo_f, fm_f in ODOO_TO_FM.items():
            ov = (rd.get(odoo_f) or '').strip()
            fv = (rd.get(fm_f)   or '').strip()
            if ov == fv:          st = 'ok'
            elif ov and not fv:   st = 'odoo_only';  has_diff = True
            elif fv and not ov:   st = 'fm_only';    has_diff = True
            else:                 st = 'both_diff';  has_diff = True
            fields_status[odoo_f] = {'status':st,'odoo':ov,'fm':fv,'fm_field':fm_f}

        if only_diff and not has_diff: continue
        if status_filter and not any(v['status']==status_filter for v in fields_status.values()): continue

        results.append({
            'id_name':   rd['id_name'],
            'title':     rd.get('title',''),
            'artist':    rd.get('artist',''),
            'status':    rd.get('status',''),
            'gallery':   rd.get('gallery',''),
            'fm_sync':   rd.get('fm_sync',''),
            'odoo_sync': rd.get('odoo_sync',''),
            'has_diff':  has_diff,
            'fields':    fields_status,
        })

    total = len(results)
    paged = results[(page-1)*per_page : page*per_page]
    return jsonify({'total':total,'page':page,'per_page':per_page,'rows':paged})

@app.route('/api/sync_check/push', methods=['POST'])
@login_required
def api_sync_check_push():
    """Pousse les champs Odoo → FM pour une oeuvre donnée."""
    data    = request.json
    id_name = (data.get('id_name') or '').strip()
    fields  = data.get('fields', [])  # liste de odoo_field_names à pousser
    if not id_name:
        return jsonify({'error': 'missing id_name'}), 400

    db = get_db()
    fm_row = db.execute("SELECT record_id FROM fm_cache WHERE id_name=?", (id_name,)).fetchone()
    db.close()
    if not fm_row or not fm_row['record_id']:
        return jsonify({'error': 'record FM introuvable'}), 404

    token = _fm_get_token()
    if not token:
        return jsonify({'error': 'Connexion FM impossible'}), 500

    # Récupérer les valeurs Odoo pour cet artwork
    try:
        odoo_fields_to_read = fields if fields else list(ODOO_TO_FM.keys())
        res = odoo_call('product.template', 'search_read',
                        [[['IdName','=',id_name]]],
                        {'fields': odoo_fields_to_read, 'limit': 1})
        if not res:
            return jsonify({'error': 'Artwork Odoo introuvable'}), 404
        odoo_data = res[0]
    except Exception as e:
        return jsonify({'error': f'Odoo: {e}'}), 500

    field_data   = {}   # direct FM artwork fields
    photos_added = 0    # records created in CRMPhotos

    for odoo_f in odoo_fields_to_read:
        val = odoo_data.get(odoo_f) or ''
        if not val:
            continue
        if odoo_f in FM_PHOTOS_TABLE_FIELDS:
            filename = val.split('/')[-1].split('?')[0]
            if _fm_create_photo_record(token, id_name, filename, val):
                photos_added += 1
        else:
            fm_f = ODOO_TO_FM.get(odoo_f)
            if fm_f:
                field_data[fm_f] = val

    pushed = 0
    ok = True
    if field_data:
        ok = _fm_update_record(token, fm_row['record_id'], field_data)
        if ok:
            pushed = len(field_data)

    try:
        requests.delete(f'{FM_URL}/databases/{FM_DB}/sessions/{token}', timeout=5, verify=False)
    except Exception:
        pass

    if not ok:
        return jsonify({'ok': False, 'error': 'FM a refusé la mise à jour (voir logs)', 'pushed': 0})

    # Mettre à jour le cache local pour les champs directs
    if field_data:
        db = get_db()
        for fm_f, val in field_data.items():
            db.execute(f"UPDATE fm_cache SET {fm_f}=? WHERE id_name=?", (val, id_name))
        db.commit()
        db.close()

    return jsonify({'ok': True, 'pushed': pushed + photos_added, 'fields': field_data, 'photos_table': photos_added})

_sync_thread = threading.Thread(target=_fm_sync_loop, daemon=True)
_sync_thread.start()

# ── Refresh all caches ────────────────────────────────────────────────────────
_refresh_state = {
    'running': False, 'step': '', 'started': None,
    'purge_files': 0, 'scan_count': 0, 'stale_fm': 0, 'stale_odoo': 0,
    'errors': [],
}

def _purge_image_caches():
    """Wipe regenerated JPEG/thumbnail caches so /photo/ and /thumb/ rebuild
    them from the current source files on disk. Source files in /photos/* are
    NEVER touched."""
    import shutil
    removed = 0
    for root in ('/app/data/thumbs', '/app/data/jpeg_cache'):
        if not os.path.isdir(root):
            continue
        for sub in os.listdir(root):
            sub_path = os.path.join(root, sub)
            if not os.path.isdir(sub_path):
                continue
            for dirpath, _, files in os.walk(sub_path):
                for f in files:
                    try:
                        os.remove(os.path.join(dirpath, f))
                        removed += 1
                    except OSError:
                        pass
    return removed

def _flag_stale_records(sync_started_iso):
    """Mark cache records older than the current sync as stale (no delete).
    Returns (stale_fm, stale_odoo) counts."""
    db = get_db()
    stale_fm = stale_odoo = 0
    try:
        stale_fm_rows = db.execute(
            "SELECT id_name FROM fm_cache WHERE last_sync < ?", (sync_started_iso,)
        ).fetchall()
        stale_fm = len(stale_fm_rows)
        if _table_exists(db, 'odoo_photos_cache'):
            stale_odoo_rows = db.execute(
                "SELECT id_name FROM odoo_photos_cache WHERE synced_at < ?", (sync_started_iso,)
            ).fetchall()
            stale_odoo = len(stale_odoo_rows)
        if stale_fm or stale_odoo:
            db.execute(
                'INSERT INTO activity_log (user,action,detail) VALUES (?,?,?)',
                ('system', 'refresh_stale_warning',
                 f'FM stale: {stale_fm} · Odoo stale: {stale_odoo} (records not returned by latest refresh — review manually)')
            )
            db.commit()
    finally:
        db.close()
    return stale_fm, stale_odoo

def _do_refresh_all():
    """Full pipeline: purge image caches → rescan disk → FM sync → Odoo sync.
    Each step updates _refresh_state['step'] so the UI can show progress."""
    global _refresh_state
    if _refresh_state['running']:
        return
    started = datetime.utcnow().isoformat()
    _refresh_state.update({
        'running': True, 'step': 'purge', 'started': started,
        'purge_files': 0, 'scan_count': 0, 'stale_fm': 0, 'stale_odoo': 0,
        'errors': [],
    })
    try:
        # 1) Image cache purge — fixes stale thumbnails/JPEGs
        try:
            _refresh_state['purge_files'] = _purge_image_caches()
        except Exception as e:
            _refresh_state['errors'].append(f'purge: {e}')

        # 2) Disk scan — picks up new files / removes deleted ones
        _refresh_state['step'] = 'scan'
        try:
            _refresh_state['scan_count'] = scan_disk_index()
        except Exception as e:
            _refresh_state['errors'].append(f'scan: {e}')

        # 3) FM cache
        _refresh_state['step'] = 'fm'
        _do_fm_sync_all()
        if _fm_sync_state.get('last_error'):
            _refresh_state['errors'].append(f"fm: {_fm_sync_state['last_error']}")

        # 4) Odoo cache
        _refresh_state['step'] = 'odoo'
        _sync_odoo_photos_worker()
        if _odoo_photo_sync_state.get('last_error'):
            _refresh_state['errors'].append(f"odoo: {_odoo_photo_sync_state['last_error']}")

        # 5) Flag stale (records not refreshed this run) — no delete
        try:
            _refresh_state['stale_fm'], _refresh_state['stale_odoo'] = _flag_stale_records(started)
        except Exception as e:
            _refresh_state['errors'].append(f'stale_check: {e}')
    finally:
        _refresh_state.update({'running': False, 'step': 'done'})

def _nightly_refresh_loop():
    """Chaque nuit à 2h00 UTC : rafraîchit les deux caches automatiquement."""
    import datetime as dt
    while True:
        now = dt.datetime.utcnow()
        # Prochain 2h00 UTC
        next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += dt.timedelta(days=1)
        wait = (next_run - dt.datetime.utcnow()).total_seconds()
        _time.sleep(max(wait, 60))
        print(f'[nightly] Démarrage refresh caches automatique — {dt.datetime.utcnow().isoformat()}')
        try:
            _do_refresh_all()
        except Exception as e:
            print(f'[nightly] Erreur: {e}')

@app.route('/api/refresh_all', methods=['POST'])
@login_required
def api_refresh_all():
    if _refresh_state['running']:
        return jsonify({'ok': False, 'error': 'Refresh déjà en cours'}), 409
    if _fm_sync_state.get('running') or _odoo_photo_sync_state.get('running'):
        return jsonify({'ok': False, 'error': 'Une synchro est déjà en cours'}), 409
    threading.Thread(target=_do_refresh_all, daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/refresh_all/status')
@login_required
def api_refresh_all_status():
    fm   = _fm_sync_state
    odoo = _odoo_photo_sync_state
    return jsonify({
        'running':     _refresh_state['running'],
        'step':        _refresh_state['step'],
        'started':     _refresh_state['started'],
        'purge_files': _refresh_state.get('purge_files', 0),
        'scan_count':  _refresh_state.get('scan_count', 0),
        'stale_fm':    _refresh_state.get('stale_fm', 0),
        'stale_odoo':  _refresh_state.get('stale_odoo', 0),
        'errors':      _refresh_state.get('errors', []),
        'fm':   {
            'running': fm.get('running', False), 'done': fm.get('done', 0),
            'total':   fm.get('total', 0),       'errors': fm.get('errors', 0),
            'last_error': fm.get('last_error', ''),
        },
        'odoo': {
            'running': odoo.get('running', False), 'done': odoo.get('done', 0),
            'total':   odoo.get('total', 0),
            'last_error': odoo.get('last_error', ''),
        },
    })

threading.Thread(target=_nightly_refresh_loop, daemon=True).start()

init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
