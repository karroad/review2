#!/usr/bin/env python3
"""Check FileMaker fields for given numeric IDs — resolve id_names from local DB first."""
import requests, base64, sqlite3, sys
requests.packages.urllib3.disable_warnings()

FM_URL      = 'https://178.248.210.53/fmi/data/vLatest'
FM_DB       = 'OperaGallery'
FM_LOGIN    = 'DataApiAccess'
FM_PASSWORD = 'JPMEJU1JPMEJU1'
FM_LAYOUT   = 'Artworks'
DB_PATH     = '/app/data/reviewv2.db'

NUMERIC_IDS = [45409, 45408, 45603, 45661, 46465, 46466, 46469, 46472, 46473,
               46478, 46480, 45532, 45533, 45534, 45535, 45536, 45547, 45493]

FM_FIELDS = ['MAINFM','MAIN_HD','MAINTIF','UrlMainA5','BACK300','FRAME300',
             'PERS300','Signature','OTHER300']

# ── Step 1: resolve id_names from local DB ──────────────────────────────────
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
placeholders = ' OR '.join(["id_name LIKE ?"]*len(NUMERIC_IDS))
params = [f'%-{n}' for n in NUMERIC_IDS]
rows = db.execute(f"SELECT id_name, odoo_id FROM artworks WHERE {placeholders}", params).fetchall()
db.close()

id_map = {int(r['id_name'].split('-')[-1]): r['id_name'] for r in rows}

print("\n=== ID_NAME résolution ===")
for n in NUMERIC_IDS:
    found = id_map.get(n, 'NON TROUVÉ dans DB locale')
    print(f"  {n} → {found}")

# ── Step 2: FM token ─────────────────────────────────────────────────────────
def get_token():
    r = requests.post(
        f'{FM_URL}/databases/{FM_DB}/sessions',
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Basic ' + base64.b64encode(f'{FM_LOGIN}:{FM_PASSWORD}'.encode()).decode()},
        json={}, timeout=10, verify=False
    )
    return r.json().get('response', {}).get('token', '')

def find_record(token, id_name):
    r = requests.post(
        f'{FM_URL}/databases/{FM_DB}/layouts/{FM_LAYOUT}/_find',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json={'query': [{'IdName': id_name}]},
        timeout=30, verify=False
    )
    data = r.json().get('response', {}).get('data', [])
    return data[0] if data else None

token = get_token()
if not token:
    print("❌ Impossible de se connecter à FileMaker")
    sys.exit(1)

# ── Step 3: check FM fields ──────────────────────────────────────────────────
def sym(val):
    if not val or not val.strip(): return '—'
    fn = val.split('/')[-1].split('?')[0]
    return (fn[:18] if fn else '✓')

header = f"\n{'NUM':<7} {'IdName':<22}"
for f in FM_FIELDS:
    header += f" {f[:12]:<13}"
print(header)
print("-"*120)

for nid in NUMERIC_IDS:
    id_name = id_map.get(nid)
    if not id_name:
        print(f"{nid:<7} {'—DB locale—':<22}")
        continue
    rec = find_record(token, id_name)
    if not rec:
        print(f"{nid:<7} {id_name:<22} FM: NOT FOUND")
        continue
    fd = rec.get('fieldData', {})
    row = f"{nid:<7} {id_name:<22}"
    for fld in FM_FIELDS:
        row += f" {sym(fd.get(fld,'')):<13}"
    print(row)

try:
    requests.delete(f'{FM_URL}/databases/{FM_DB}/sessions/{token}', timeout=5, verify=False)
except: pass
