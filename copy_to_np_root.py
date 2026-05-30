#!/usr/bin/env python3
"""
Copy all files from ARTIST-FULL subdirectories to the root in 2 versions:
1. Original filename (with spaces/special chars)
2. Normalized filename (spaces→underscores, commas/accents cleaned up)
"""
import os, shutil, re, unicodedata

SRC_ROOT = '/home/pictures/ARTIST-FULL'
DST_ROOT = '/home/pictures/ARTIST-FULL'  # same root = flat copy

def normalize(name):
    """spaces→underscore, remove commas/parens, collapse multiple underscores."""
    # decompose accents but keep the base char (é→e, °→°, etc.)
    n = name
    # replace common separators with underscore
    n = re.sub(r'[\s,;]+', '_', n)
    # remove remaining unwanted chars (parens, quotes, etc.)
    n = re.sub(r"[()'\"]", '', n)
    # collapse multiple underscores
    n = re.sub(r'_+', '_', n)
    # strip leading/trailing underscores
    n = n.strip('_')
    return n

copied  = 0
skipped = 0
errors  = 0

for dirpath, dirnames, filenames in os.walk(SRC_ROOT):
    # only process files in subdirectories, not root itself
    if dirpath == SRC_ROOT:
        continue
    for fname in filenames:
        if fname.startswith('.'):
            continue
        src = os.path.join(dirpath, fname)

        # Version 1 : original name at root
        dst1 = os.path.join(DST_ROOT, fname)
        if not os.path.exists(dst1):
            try:
                shutil.copy2(src, dst1)
                copied += 1
            except Exception as e:
                print(f"ERR v1 {fname}: {e}")
                errors += 1
        else:
            skipped += 1

        # Version 2 : normalized name at root
        norm_fname = normalize(fname)
        if norm_fname != fname:
            dst2 = os.path.join(DST_ROOT, norm_fname)
            if not os.path.exists(dst2):
                try:
                    shutil.copy2(src, dst2)
                    copied += 1
                except Exception as e:
                    print(f"ERR v2 {norm_fname}: {e}")
                    errors += 1
            else:
                skipped += 1

print(f"\nDone — copié: {copied}, déjà existant: {skipped}, erreurs: {errors}")
