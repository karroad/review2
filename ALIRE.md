# ReviewV2 — Documentation complète

## Vue d'ensemble

Application Flask de gestion des photos d'œuvres d'art.
- **Gauche** : photos trouvées sur le disque serveur
- **Droite** : champs photos actuels dans Odoo (operacrm.com)
- **Action** : cliquer une photo disque + un slot Odoo = mise à jour directe dans Odoo

URL cible : `https://review.operagallery.com`

---

## Structure des fichiers

```
/home/projet/reviewv2/
├── ALIRE.md                  ← ce fichier
├── Dockerfile                ← image Docker (BUG — voir section Docker)
├── docker-compose.yml        ← déploiement Traefik
├── .env                      ← credentials Odoo + auth utilisateurs
├── vendor/                   ← packages Python sans internet
│   ├── pyotp/
│   ├── pyotp-2.9.0.dist-info/
│   ├── qrcode/
│   └── qrcode-8.2.dist-info/
└── app/
    ├── app.py                ← backend Flask principal
    ├── requirements.txt      ← dépendances Python
    ├── templates/
    │   ├── login.html        ← MANQUANT — À CRÉER
    │   ├── index.html        ← page liste des œuvres
    │   └── artwork.html      ← page gestion photos par œuvre
    └── data/                 ← volume Docker persistant
        ├── reviewv2.db       ← SQLite (créé au démarrage)
        └── thumbs/           ← cache miniatures
            ├── FM/
            ├── A5/
            ├── NP/
            ├── 300/
            └── TIFF/
```

---

## Docker

### Dockerfile — PROBLÈME CRITIQUE

```dockerfile
FROM photo-review-photo-review   # ← dépend de l'image de l'autre app
WORKDIR /app
COPY app/ .
COPY vendor/ /usr/local/lib/python3.12/site-packages/
RUN mkdir -p /app/data
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
```

**BUG :** `FROM photo-review-photo-review` — cette image doit exister localement.
Pour construire indépendamment, remplacer par :

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .
RUN mkdir -p /app/data
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
```

> ⚠️ Le serveur n'a pas accès à internet depuis Docker. Soit utiliser le dossier `vendor/` pour les packages pyotp/qrcode, soit builder depuis l'image photo-review qui a déjà Flask/Pillow/requests installés.

### docker-compose.yml — PROBLÈMES

```yaml
services:
  web:
    build: .
    # MANQUANT: container_name: reviewv2
    # MANQUANT: restart: unless-stopped
    volumes:
      - /home/projet/pictures/img/FM:/photos/FM
      - /home/projet/pictures/img/A5:/photos/A5
      - /home/projet/pictures/img/np:/photos/np
      - /home/projet/pictures/img/300:/photos/300
      - /home/projet/pictures/img/tiff:/photos/tiff
      - ./data:/app/data
    env_file: .env
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.reviewv2.rule=Host(`review.operagallery.com`)"
      - "traefik.http.routers.reviewv2.entrypoints=websecure"
      - "traefik.http.routers.reviewv2.tls.certresolver=letsencrypt"
      - "traefik.http.services.reviewv2.loadbalancer.server.port=5000"
    networks:
      - traefik-network

networks:
  traefik-network:
    external: true
```

**Corrections à apporter :**
- Ajouter `container_name: reviewv2`
- Ajouter `restart: unless-stopped`
- Vérifier que `review.operagallery.com` pointe bien vers le serveur en DNS

### Commandes de démarrage

```bash
cd /home/projet/reviewv2
docker-compose build
docker-compose up -d
docker-compose logs -f
```

---

## Fichier .env

```
OPERACRM_URL=https://operacrm.com
OPERACRM_DB=odoo_15
OPERACRM_LOGIN=surafelwubshet7@gmail.com
OPERACRM_PASSWORD=Surafell
SECRET_KEY=reviewv2_secret_change_me_in_prod   ← CHANGER EN PROD
AUTH_USER1=admin:a5d90b21c4037474ef2116df601757c3785891dfa074a99a8a2ff6e3e63564d2:DDNBSKICYV6OPSYXY7RN25A6VANFUWDQ
```

### Format des utilisateurs

```
AUTH_USER1=login:sha256_du_mot_de_passe:secret_totp_base32
AUTH_USER2=...
```

- **login** : identifiant texte libre
- **sha256** : `echo -n "monmotdepasse" | sha256sum`
- **secret TOTP** : chaîne base32, 32 caractères (ex: générer avec `python3 -c "import pyotp; print(pyotp.random_base32())"`)

Le hash `a5d90b21...` correspond au mot de passe `admin` → **À CHANGER**.

### Configurer Google Authenticator

Accéder à `/setup_otp/admin` (sans être connecté) → affiche un QR code PNG à scanner.

---

## Authentification

Toutes les pages sont protégées par `@login_required`.
Le formulaire `/login` attend 3 champs : `login`, `password`, `otp`.

**⚠️ MANQUANT : `app/templates/login.html`** — à créer avec ce formulaire :

```html
<form method="POST">
  <input type="text" name="login" placeholder="Login">
  <input type="password" name="password" placeholder="Mot de passe">
  <input type="text" name="otp" placeholder="Code OTP (6 chiffres)" autocomplete="off">
  <button type="submit">Connexion</button>
</form>
```

**⚠️ BUG :** `/api/sync`, `/api/scan_disk`, `/api/disk_files/<id>` n'ont pas `@login_required` — n'importe qui peut déclencher une sync Odoo.

---

## Base de données SQLite

Fichier : `/app/data/reviewv2.db` (créé automatiquement au démarrage)

### Table `artworks`

| Colonne | Type | Description |
|---------|------|-------------|
| odoo_id | INTEGER PK | ID interne Odoo (product.template) |
| id_name | TEXT | Ex: `CABELI-47703` |
| title | TEXT | Titre de l'œuvre |
| artist | TEXT | Nom de l'artiste |
| status | TEXT | Ex: `In stock`, `On order`, `Sold` |
| category | TEXT | Catégorie Odoo |
| synced_at | TEXT | Date de dernière sync |

### Table `disk_index`

| Colonne | Type | Description |
|---------|------|-------------|
| id_name | TEXT PK | Ex: `CABELI-47703` |
| dirs | TEXT | CSV des répertoires : `A5,FM,NP` |
| file_count | INTEGER | Nombre total de fichiers trouvés |
| scanned_at | TEXT | Date du dernier scan |

---

## Répertoires photos (montés en volumes)

| Clé | Chemin disque | Chemin container | Description |
|-----|--------------|-----------------|-------------|
| FM | `/home/projet/pictures/img/FM` | `/photos/FM` | Haute résolution ~3307px, MAIN |
| A5 | `/home/projet/pictures/img/A5` | `/photos/A5` | Résolution A5 ~1980px |
| NP | `/home/projet/pictures/img/np` | `/photos/np` | Originaux avec noms descriptifs |
| 300 | `/home/projet/pictures/img/300` | `/photos/300` | 300 dpi |
| TIFF | `/home/projet/pictures/img/tiff` | `/photos/tiff` | Pyramid TIFFs |

---

## Inférence de rôle (depuis le nom de fichier)

L'app lit le nom du fichier et lui attribue automatiquement un rôle :

| Pattern (regex, insensible casse) | Rôle affiché |
|-----------------------------------|-------------|
| `_MAIN_A5` | MAIN A5 |
| `_MAIN` | MAIN |
| `_RECTO` ou `recto` | RECTO |
| `_VERSO` ou `verso` | VERSO |
| `_BACK` ou `back` | BACK |
| `IN_SITU` | IN SITU |
| `DETAIL` ou `_DET` | DETAIL |
| `SIGNATURE` ou `signature` | SIGNATURE |
| `FRAME` | FRAME |
| `SCALE` | SCALE |
| `pyramid` | PYRAMID |
| (aucun match) | OTHER |

---

## Champs Odoo gérés

Ces champs sont lus et écrits sur le modèle `product.template` d'Odoo 15 :

| Champ Odoo | Label affiché |
|------------|--------------|
| `main_super_picture_hd` | Super HD |
| `main_picture_hd` | Main HD |
| `main_web_picture` | Web |
| `main_a6_picture` | A6 |
| `main_hd_web_picture` | HD Web |
| `other_url` | Other |
| `detail_1_url` | Detail 1 |
| `detail_2_url` | Detail 2 |
| `back_pictures` | Back |
| `frame_picture` | Frame |
| `perspective_url` | Perspective |
| `signatures_pictures` | Signature |

Ces champs contiennent des **URLs https** (ex: `https://operacrm.com/web/image/...`), pas du base64.

---

## Routes API

| Route | Méthode | Auth | Description |
|-------|---------|------|-------------|
| `/` | GET | ✅ | Liste œuvres, pagination 100/page, recherche |
| `/artwork/<id_name>` | GET | ✅ | Page photo management pour une œuvre |
| `/photo/<dir>/<filename>` | GET | ✅ | Sert le fichier original depuis disque |
| `/thumb/<dir>/<filename>` | GET | ✅ | Miniature 400px (cache dans `/app/data/thumbs/`) |
| `/api/assign` | POST | ✅ | Assigne URL vers champ Odoo |
| `/api/sync` | POST | ❌ BUG | Sync Odoo → SQLite |
| `/api/scan_disk` | POST | ❌ BUG | Scan disque → disk_index |
| `/api/disk_files/<id>` | GET | ❌ BUG | Liste fichiers disque pour un ID |
| `/login` | GET/POST | — | Formulaire connexion |
| `/logout` | GET | — | Déconnexion |
| `/setup_otp/<login>` | GET | — | QR code PNG pour configurer OTP |

### POST /api/assign

```json
{
  "odoo_id": 12345,
  "field": "main_picture_hd",
  "url": "https://review.operagallery.com/photo/FM/CABELI-47703_MAIN.jpg"
}
```

Réponse : `{"ok": true}` ou `{"error": "message"}`

---

## Connexion Odoo — BUG CRITIQUE

La méthode `_crm_session()` utilise :
```python
requests.post(f'{ODOO_URL}/web/dataset/call_kw', json={
    'params': {
        'model': 'res.users', 'method': 'authenticate', ...
    }
})
```

**Ce n'est pas le bon endpoint pour l'authentification Odoo.**
Le bon endpoint est `/web/dataset/call_kw` avec le modèle `common` ou bien `/web/session/authenticate`.

**Référence qui fonctionne :** voir `/home/projet/photo-review/app/app.py` — la fonction `_crm_session()` de cette app fonctionne correctement en production.

---

## BUG MAJEUR — URLs photos vers Odoo

Quand l'utilisateur assigne une photo, l'app envoie à Odoo :
```
https://review.operagallery.com/photo/FM/CABELI-47703_MAIN.jpg
```

**Problème :** Odoo (operacrm.com) doit pouvoir accéder à cette URL pour afficher la photo dans le CRM. Or :
- `/photo/<dir>/<filename>` requiert une session authentifiée
- Odoo n'a pas de session ReviewV2

**Solutions possibles :**
1. Retirer `@login_required` sur la route `/photo/` uniquement (accès public aux photos)
2. Créer une route `/pub/photo/<token>/<dir>/<filename>` avec token signé
3. Utiliser une URL publique différente (nginx qui sert directement `/home/projet/pictures/img/`)

---

## Flux utilisateur prévu

1. `https://review.operagallery.com` → redirige vers `/login`
2. Saisir login + mot de passe + code OTP (Google Authenticator)
3. Page index vide au premier lancement → cliquer **↻ Sync Odoo** (importe toutes les œuvres depuis Odoo)
4. Cliquer **🔍 Scan Disque** (indexe tous les fichiers photo des 5 répertoires)
5. La grille affiche les œuvres avec badges FM/A5/NP et nombre de fichiers
6. Cliquer une œuvre → page artwork
7. **Gauche** : grille des photos sur disque, filtrables par répertoire, avec badge de rôle (MAIN, RECTO, IN SITU…)
8. **Droite** : liste des 12 champs Odoo avec URL actuelle et miniature
9. Cliquer photo gauche → bordure jaune = sélectionné
10. Cliquer slot Odoo droit → confirmation → écriture dans Odoo via JSON-RPC
11. Bouton ✕ sur un slot → vide le champ dans Odoo

---

## Ce qui reste à faire

| Priorité | Tâche |
|----------|-------|
| 🔴 BLOQUANT | Créer `app/templates/login.html` |
| 🔴 BLOQUANT | Corriger le Dockerfile (FROM invalide) |
| 🔴 BLOQUANT | Corriger `_crm_session()` (voir photo-review) |
| 🔴 BLOQUANT | Corriger l'URL des photos envoyées à Odoo |
| 🟠 IMPORTANT | Ajouter `@login_required` sur les routes API |
| 🟠 IMPORTANT | Ajouter `container_name` et `restart` dans docker-compose |
| 🟡 NICE TO HAVE | Filtre par statut sur la page index |
| 🟡 NICE TO HAVE | Indicateur visuel quand tous les champs Odoo sont remplis |
| 🟡 NICE TO HAVE | Bouton "ouvrir dans Odoo" depuis la page artwork |
