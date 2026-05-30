"""
Image quality scorer for review2.
Pure helper module — no Flask, no DB. Easy to unit-test and revert.

Returns a dict with: width, height, megapixels, dpi, filesize,
aspect, is_likely_crop, is_thumbnail, is_compressed, quality_score,
why (human-readable explanation).
"""
import os
import math
from PIL import Image

THUMBNAIL_MAX_DIM       = 500       # below this on either side → thumbnail
THUMBNAIL_MAX_BYTES     = 50 * 1024 # below 50KB → also thumbnail
COMPRESSED_BYTES_PER_MP = 200_000   # below 200KB/MP → over-compressed
CROP_ASPECT_TOLERANCE   = 0.20      # >20% deviation from main aspect → likely crop
DUPLICATE_DIM_TOLERANCE = 0.05      # ±5% size match → potential duplicate


def _safe_dpi(img):
    """Pillow stores dpi in info dict; fallback 72 if missing or weird."""
    try:
        dpi = img.info.get('dpi')
        if isinstance(dpi, tuple) and dpi:
            return int(dpi[0])
        if isinstance(dpi, (int, float)):
            return int(dpi)
    except Exception:
        pass
    return 72


def measure(filepath):
    """Cheap measurement — opens the image only enough to read dims + dpi."""
    try:
        with Image.open(filepath) as img:
            w, h = img.size
            dpi = _safe_dpi(img)
        size = os.path.getsize(filepath)
        return {
            'width':    w,
            'height':   h,
            'dpi':      dpi,
            'filesize': size,
            'aspect':   (w / h) if h else 0,
            'megapixels': (w * h) / 1_000_000 if w and h else 0,
            'ok': True,
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def is_likely_crop(aspect, baseline_aspect):
    """Compare against the artwork's MAIN photo aspect ratio."""
    if not baseline_aspect or not aspect:
        return False
    deviation = abs(aspect - baseline_aspect) / baseline_aspect
    return deviation > CROP_ASPECT_TOLERANCE


def is_thumbnail(width, height, filesize):
    if width and height and (width < THUMBNAIL_MAX_DIM or height < THUMBNAIL_MAX_DIM):
        return True
    if filesize and filesize < THUMBNAIL_MAX_BYTES:
        return True
    return False


def is_compressed(filesize, megapixels):
    if not megapixels or megapixels < 0.1:
        return False
    return (filesize / megapixels) < COMPRESSED_BYTES_PER_MP


def score_image(filepath, baseline_aspect=None):
    """
    Composite quality score 0–100.

    Score formula:
      +40 × log10(megapixels + 1)        # resolution
      +20 × min(dpi/300, 1)              # DPI (capped at 300)
      +15 × min(filesize_per_MP / target, 1)  # quality density
      -30 × is_likely_crop
      -50 × is_thumbnail
      -10 × is_compressed
    """
    m = measure(filepath)
    if not m.get('ok'):
        return {'ok': False, 'error': m.get('error', 'unknown'), 'quality_score': 0}

    w, h = m['width'], m['height']
    mp   = m['megapixels']
    dpi  = m['dpi']
    size = m['filesize']

    crop  = is_likely_crop(m['aspect'], baseline_aspect)
    thumb = is_thumbnail(w, h, size)
    comp  = is_compressed(size, mp)

    score = 0.0
    parts = []

    res_pts = 40 * math.log10(mp + 1) if mp > 0 else 0
    score += res_pts
    parts.append(f"{mp:.1f}MP")

    dpi_pts = 20 * min(dpi / 300, 1.0)
    score += dpi_pts
    parts.append(f"{dpi}dpi")

    if mp > 0:
        density = (size / mp) / (COMPRESSED_BYTES_PER_MP * 2)
        dens_pts = 15 * min(density, 1.0)
        score += dens_pts

    if crop:
        score -= 30
        parts.append("⚠ likely crop")
    if thumb:
        score -= 50
        parts.append("⚠ thumbnail")
    if comp:
        score -= 10
        parts.append("⚠ compressed")

    score = max(0, min(100, score))

    return {
        'ok':              True,
        'width':           w,
        'height':          h,
        'megapixels':      round(mp, 2),
        'dpi':             dpi,
        'filesize':        size,
        'aspect':          round(m['aspect'], 3),
        'is_likely_crop':  crop,
        'is_thumbnail':    thumb,
        'is_compressed':   comp,
        'quality_score':   round(score, 1),
        'why':             ' · '.join(parts),
    }


def find_baseline_aspect(scored_files):
    """
    Determine the artwork's true aspect ratio from already-scored files.
    Picks the highest-scoring non-thumbnail file in MAIN/RECTO role
    (or just highest scoring overall as fallback) and uses its aspect.
    """
    main_candidates = [
        f for f in scored_files
        if f.get('role') in ('MAIN', 'RECTO', 'MAIN A5')
        and not f.get('quality', {}).get('is_thumbnail', False)
        and f.get('quality', {}).get('aspect')
    ]
    pool = main_candidates or [
        f for f in scored_files
        if f.get('quality', {}).get('aspect') and not f.get('quality', {}).get('is_thumbnail')
    ]
    if not pool:
        return None
    best = max(pool, key=lambda f: f['quality'].get('quality_score', 0))
    return best['quality'].get('aspect')


def detect_duplicates(scored_files):
    """
    Mark probable duplicates: same role + dimensions within 5%.
    Keeps the highest-scoring copy unflagged.
    """
    by_role = {}
    for f in scored_files:
        role = f.get('role', 'OTHER')
        by_role.setdefault(role, []).append(f)

    for role, items in by_role.items():
        for i, a in enumerate(items):
            qa = a.get('quality', {})
            if not qa.get('ok'):
                continue
            for b in items[i + 1:]:
                qb = b.get('quality', {})
                if not qb.get('ok'):
                    continue
                if qa['width'] == 0 or qb['width'] == 0:
                    continue
                w_ratio = abs(qa['width']  - qb['width'])  / max(qa['width'],  qb['width'])
                h_ratio = abs(qa['height'] - qb['height']) / max(qa['height'], qb['height'])
                if w_ratio < DUPLICATE_DIM_TOLERANCE and h_ratio < DUPLICATE_DIM_TOLERANCE:
                    # same dimensions — flag the lower-scoring one
                    if qa['quality_score'] >= qb['quality_score']:
                        qb['is_duplicate'] = True
                    else:
                        qa['is_duplicate'] = True
    return scored_files


def pick_best_per_role(scored_files):
    """
    Returns dict: role → highest-scoring file (or None).
    Filters out thumbnails and known duplicates.
    """
    out = {}
    by_role = {}
    for f in scored_files:
        role = f.get('role', 'OTHER')
        by_role.setdefault(role, []).append(f)

    for role, items in by_role.items():
        eligible = [
            f for f in items
            if f.get('quality', {}).get('ok')
            and not f['quality'].get('is_thumbnail')
            and not f['quality'].get('is_duplicate')
        ]
        if not eligible:
            eligible = [f for f in items if f.get('quality', {}).get('ok')]
        if eligible:
            best = max(eligible, key=lambda f: f['quality'].get('quality_score', 0))
            out[role] = best
        else:
            out[role] = None

    return out
