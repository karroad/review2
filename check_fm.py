#!/usr/bin/env python3
"""Check FileMaker fields for given numeric IDs."""
import requests, base64, json, sys
requests.packages.urllib3.disable_warnings()

FM_URL      = 'https://178.248.210.53/fmi/data/vLatest'
FM_DB       = 'OperaGallery'
FM_LOGIN    = 'DataApiAccess'
FM_PASSWORD = 'JPMEJU1JPMEJU1'
FM_LAYOUT   = 'Artworks'

IDS = [45409, 45408, 45603, 45661, 46465, 46466, 46469, 46472, 46473,
       46478, 46480, 45532, 45533, 45534, 45535, 45536, 45547, 45493]

FM_FIELDS = ['MAINFM','MAIN_HD','MAINTIF','UrlMainA5','BACK300','FRAME300',
             'PERS300','Signature','OTHER300','DET300','INSITU300','LEFT300','FRONT300']

def get_token():
    r = requests.post(
        f'{FM_URL}/databases/{FM_DB}/sessions',
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Basic ' + base64.b64encode(f'{FM_LOGIN}:{FM_PASSWORD}'.encode()).decode()},
        json={}, timeout=10, verify=False
    )
    return r.json().get('response', {}).get('token', '')

def find_record(token, numeric_id):
    # Try to find by numeric part of IdName
    r = requests.post(
        f'{FM_URL}/databases/{FM_DB}/layouts/{FM_LAYOUT}/_find',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json={'query': [{'IdName': f'*-{numeric_id}'}]},
        timeout=30, verify=False
    )
    data = r.json().get('response', {}).get('data', [])
    return data[0] if data else None

token = get_token()
if not token:
    print("❌ Impossible de se connecter à FileMaker")
    sys.exit(1)

print(f"{'ID':<8} {'IdName':<20} {'MAINFM':<6} {'MAIN_HD':<8} {'MAINTIF':<8} {'A5':<5} {'BACK':<5} {'FRAME':<6} {'PERS':<5} {'SIG':<5} {'OTHER':<6}")
print("-"*100)

for nid in IDS:
    rec = find_record(token, nid)
    if not rec:
        print(f"{nid:<8} {'NOT FOUND':<20}")
        continue
    fd = rec.get('fieldData', {})
    id_name = fd.get('IdName', '?')

    def short(url):
        if not url: return '—'
        fn = url.split('/')[-1].split('?')[0]
        return fn[:30] if fn else '✓'

    row = f"{nid:<8} {id_name:<20}"
    for fld in ['MAINFM','MAIN_HD','MAINTIF','UrlMainA5','BACK300','FRAME300','PERS300','Signature','OTHER300']:
        val = fd.get(fld, '') or ''
        sym = short(val)[:12].ljust(13)
        row += sym
    print(row)

# Release token
try:
    requests.delete(f'{FM_URL}/databases/{FM_DB}/sessions/{token}', timeout=5, verify=False)
except: pass
