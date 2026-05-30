#!/usr/bin/env python3
import sys, requests
sys.path.insert(0, '/app')

ODOO_URL      = 'https://operacrm.com'
ODOO_DB       = 'odoo_15'
ODOO_LOGIN    = 'sync-script@operacrm.com'
ODOO_PASSWORD = 'siZNpH3n_oCaUH59jcYOUG_ROLET03uH'

sess = requests.Session()
r = sess.post(f'{ODOO_URL}/web/session/authenticate', json={
    'jsonrpc': '2.0', 'method': 'call', 'id': 1,
    'params': {'db': ODOO_DB, 'login': ODOO_LOGIN, 'password': ODOO_PASSWORD}
}, timeout=15)
uid = r.json().get('result', {}).get('uid')
if not uid:
    print("Auth failed"); sys.exit(1)

def search(field):
    r = sess.post(f'{ODOO_URL}/web/dataset/call_kw', json={
        'jsonrpc':'2.0','method':'call','id':2,
        'params': {
            'model': 'product.template', 'method': 'search_read',
            'args': [[[field, 'like', '.tif']], ['id','name', field]],
            'kwargs': {'limit': 500}
        }
    }, timeout=30)
    return r.json().get('result', [])

MAIN_FIELDS = [
    'main_web_picture',
    'main_hd_web_picture',
    'main_picture_hd',
    'main_super_picture_hd',
    'main_a6_picture',
]

for f in MAIN_FIELDS:
    res = search(f)
    print(f"\n{f}: {len(res)} TIFFs")
    for rec in res:
        url = (rec.get(f) or '')
        fn = url.split('/')[-1].split('?')[0][:50]
        print(f"  odoo#{rec['id']} → {fn}")
