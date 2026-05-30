#!/usr/bin/env python3
"""
Find records with empty MAINFM in local cache that have a main photo in Odoo.
"""
import requests, sqlite3, sys

ODOO_URL='https://operacrm.com'; ODOO_DB='odoo_15'
ODOO_LOGIN='sync-script@operacrm.com'; ODOO_PASSWORD='siZNpH3n_oCaUH59jcYOUG_ROLET03uH'
DB_PATH='/app/data/reviewv2.db'

# Step 1: get empty MAINFM from cache + their odoo_ids
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
rows = db.execute("""
    SELECT f.id_name, a.odoo_id
    FROM fm_cache f
    LEFT JOIN artworks a ON a.id_name = f.id_name
    WHERE (f.MAINFM IS NULL OR f.MAINFM='')
    AND a.odoo_id IS NOT NULL AND a.odoo_id != ''
""").fetchall()
db.close()
print(f"MAINFM vide dans cache avec odoo_id: {len(rows)}")

# Step 2: batch-check Odoo for main photo
sess = requests.Session()
sess.post(f'{ODOO_URL}/web/session/authenticate', json={
    'jsonrpc':'2.0','method':'call','id':1,
    'params':{'db':ODOO_DB,'login':ODOO_LOGIN,'password':ODOO_PASSWORD}
}, timeout=15)

BATCH = 200
candidates = []

odoo_ids = [(r['id_name'], int(r['odoo_id'])) for r in rows]
print(f"Vérification Odoo par lots de {BATCH}...")

for i in range(0, len(odoo_ids), BATCH):
    batch = odoo_ids[i:i+BATCH]
    ids   = [oid for _, oid in batch]
    r = sess.post(f'{ODOO_URL}/web/dataset/call_kw', json={
        'jsonrpc':'2.0','method':'call','id':2,
        'params':{'model':'product.template','method':'read',
                  'args':[ids, ['id','main_web_picture','main_hd_web_picture',
                                'main_picture_hd','main_super_picture_hd']],
                  'kwargs':{}}
    }, timeout=60)
    results = r.json().get('result') or []
    odoo_map = {rec['id']: rec for rec in results}

    for id_name, oid in batch:
        rec = odoo_map.get(oid)
        if not rec:
            continue
        for f in ['main_web_picture','main_hd_web_picture','main_picture_hd','main_super_picture_hd']:
            url = (rec.get(f) or '').strip()
            if len(url) > 4:
                candidates.append({'id_name': id_name, 'odoo_id': oid, 'field': f, 'url': url})
                break

    if (i // BATCH) % 5 == 0:
        print(f"  {i+len(batch)}/{len(odoo_ids)} traités, {len(candidates)} candidats trouvés")

print(f"\n{'='*60}")
print(f"Records MAINFM vide dans FM mais photo dispo dans Odoo: {len(candidates)}")
for c in candidates:
    fn = c['url'].split('/')[-1].split('?')[0][:50]
    print(f"  {c['id_name']:<25} odoo#{c['odoo_id']:<8} {c['field']}: {fn}")
