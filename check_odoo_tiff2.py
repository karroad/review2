#!/usr/bin/env python3
"""Check all Odoo main fields for .tif and .tiff URLs, paginating through all records."""
import requests, sys

ODOO_URL      = 'https://operacrm.com'
ODOO_DB       = 'odoo_15'
ODOO_LOGIN    = 'sync-script@operacrm.com'
ODOO_PASSWORD = 'siZNpH3n_oCaUH59jcYOUG_ROLET03uH'

FIELDS = [
    'main_web_picture',
    'main_hd_web_picture',
    'main_picture_hd',
    'main_super_picture_hd',
    'main_a6_picture',
]

sess = requests.Session()
r = sess.post(f'{ODOO_URL}/web/session/authenticate', json={
    'jsonrpc':'2.0','method':'call','id':1,
    'params':{'db':ODOO_DB,'login':ODOO_LOGIN,'password':ODOO_PASSWORD}
}, timeout=15)
if not r.json().get('result',{}).get('uid'):
    print("Auth failed"); sys.exit(1)

def search_read(domain, fields, offset=0, limit=1000):
    r = sess.post(f'{ODOO_URL}/web/dataset/call_kw', json={
        'jsonrpc':'2.0','method':'call','id':2,
        'params': {
            'model':'product.template','method':'search_read',
            'args':[domain, ['id','name']+fields],
            'kwargs':{'limit':limit,'offset':offset,'order':'id asc'}
        }
    }, timeout=60)
    return r.json().get('result',[])

# Paginate through all records
print("Chargement de tous les enregistrements...", flush=True)
tiff_by_field = {f: [] for f in FIELDS}
offset = 0
batch  = 1000
total  = 0

while True:
    recs = search_read([[('id','>',0)]], FIELDS, offset=offset, limit=batch)
    if not recs:
        break
    total += len(recs)
    for rec in recs:
        for f in FIELDS:
            url = (rec.get(f) or '').lower()
            if url.endswith('.tif') or url.endswith('.tiff') or '.tif?' in url or '.tiff?' in url:
                tiff_by_field[f].append(rec)
    offset += batch
    print(f"  ... {total} traités", flush=True)
    if len(recs) < batch:
        break

print(f"\nTotal interrogés : {total}\n")
print("="*60)
for f in FIELDS:
    hits = tiff_by_field[f]
    print(f"\n{f}: {len(hits)} TIFFs")
    for rec in hits:
        url = rec.get(f) or ''
        fn  = url.split('/')[-1].split('?')[0][:55]
        print(f"  odoo#{rec['id']:>7}  {fn}")
