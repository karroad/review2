#!/usr/bin/env python3
"""
Audit /media/nas files >500MB vs artworks in stock/in transit.
Output CSV: filename, size_mb, id_name, status, odoo_main, fm_main, safe_to_delete
"""
import os, re, csv, sqlite3, requests, base64
requests.packages.urllib3.disable_warnings()

NAS_DIR  = '/photos/media/nas'
MIN_SIZE = 500 * 1024 * 1024  # 500 MB
DB_PATH  = '/app/data/reviewv2.db'
OUT_CSV  = '/app/data/nas_audit.csv'

# ── Step 1: large files in /media/nas ────────────────────────────────────────
print("Scan /media/nas pour fichiers >500MB...")
large_files = []
for fname in os.listdir(NAS_DIR):
    fpath = os.path.join(NAS_DIR, fname)
    if not os.path.isfile(fpath):
        continue
    size = os.path.getsize(fpath)
    if size < MIN_SIZE:
        continue
    # Extract id_name from filename: [ARTIST-12345] or ARTIST-12345_
    m = re.search(r'\[?([A-Z]+-\d+)\]?', fname)
    id_name = m.group(1) if m else ''
    large_files.append({'fname': fname, 'fpath': fpath, 'size': size, 'id_name': id_name})

print(f"  {len(large_files)} fichiers >500MB trouvés")

# ── Step 2: get artwork statuses from local DB ────────────────────────────────
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
id_names = list({f['id_name'] for f in large_files if f['id_name']})
if id_names:
    ph = ','.join('?' * len(id_names))
    rows = db.execute(f"SELECT id_name, odoo_id, status FROM artworks WHERE id_name IN ({ph})", id_names).fetchall()
    artwork_map = {r['id_name']: dict(r) for r in rows}
else:
    artwork_map = {}
db.close()

# ── Step 3: get Odoo main fields ──────────────────────────────────────────────
odoo_ids = [int(a['odoo_id']) for a in artwork_map.values() if a.get('odoo_id')]
print(f"Vérification Odoo pour {len(odoo_ids)} oeuvres...")
odoo_main = {}
if odoo_ids:
    sess = requests.Session()
    sess.post('https://operacrm.com/web/session/authenticate', json={
        'jsonrpc':'2.0','method':'call','id':1,
        'params':{'db':'odoo_15','login':'sync-script@operacrm.com','password':'siZNpH3n_oCaUH59jcYOUG_ROLET03uH'}
    }, timeout=15)
    for i in range(0, len(odoo_ids), 200):
        batch = odoo_ids[i:i+200]
        r = sess.post('https://operacrm.com/web/dataset/call_kw', json={
            'jsonrpc':'2.0','method':'call','id':2,
            'params':{'model':'product.template','method':'read',
                      'args':[batch, ['id','main_picture_hd','main_web_picture']],
                      'kwargs':{}}
        }, timeout=30)
        for rec in r.json().get('result') or []:
            v = rec.get('main_picture_hd') or rec.get('main_web_picture') or ''
            odoo_main[rec['id']] = v.split('/')[-1].split('?')[0] if v else ''

# ── Step 4: get FM MAINFM ─────────────────────────────────────────────────────
print("Vérification FM cache...")
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
fm_rows = db.execute(f"SELECT id_name, MAINFM FROM fm_cache WHERE id_name IN ({','.join('?'*len(id_names))})", id_names).fetchall() if id_names else []
fm_map = {r['id_name']: (r['MAINFM'] or '').split('/')[-1] for r in fm_rows}
db.close()

# ── Step 5: build CSV ─────────────────────────────────────────────────────────
KEEP_STATUSES = {'in stock', 'in transit'}

rows = []
for f in large_files:
    id_name  = f['id_name']
    art      = artwork_map.get(id_name, {})
    status   = (art.get('status') or '').lower()
    odoo_id  = art.get('odoo_id')
    main_odoo = odoo_main.get(int(odoo_id), '') if odoo_id else ''
    main_fm  = fm_map.get(id_name, '')
    size_mb  = round(f['size'] / 1024 / 1024, 1)

    # Safe to delete if: artwork in stock/transit AND has main in Odoo AND has main in FM
    relevant  = status in KEEP_STATUSES
    has_odoo  = bool(main_odoo)
    has_fm    = bool(main_fm)
    safe      = relevant and has_odoo and has_fm

    rows.append({
        'filename':       f['fname'],
        'size_mb':        size_mb,
        'id_name':        id_name,
        'status':         status or '?',
        'odoo_main':      main_odoo or '—',
        'fm_main':        main_fm or '—',
        'safe_to_delete': 'OUI' if safe else 'NON',
    })

rows.sort(key=lambda r: (r['safe_to_delete'] != 'OUI', r['id_name']))

with open(OUT_CSV, 'w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(rows)

safe_count = sum(1 for r in rows if r['safe_to_delete'] == 'OUI')
total_safe_mb = sum(r['size_mb'] for r in rows if r['safe_to_delete'] == 'OUI')
print(f"\nCSV: {OUT_CSV}")
print(f"Total fichiers >500MB : {len(rows)}")
print(f"Supprimables (in stock/transit + main Odoo + main FM) : {safe_count} ({total_safe_mb:.0f} MB)")
print(f"\n{'Supprimable':<12} {'Taille':>8} {'id_name':<25} {'Status':<12} {'Odoo main':<40} {'FM main'}")
print('-'*130)
for r in rows:
    print(f"{r['safe_to_delete']:<12} {r['size_mb']:>7}MB {r['id_name']:<25} {r['status']:<12} {r['odoo_main']:<40} {r['fm_main']}")
