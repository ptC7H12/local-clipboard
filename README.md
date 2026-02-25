# LAN Clipboard

Eine minimalistische Webanwendung zum Teilen von **Text und Screenshots** im lokalen Netzwerk.
Kein Account, keine Cloud, keine externe Abhängigkeit zur Laufzeit.

```
PC1 öffnet http://server:8000/b/work  →  Screenshot pasten
PC2 öffnet http://server:8000/b/work  →  Eintrag erscheint sofort
```

---

## Inhaltsverzeichnis

1. [Voraussetzungen](#voraussetzungen)
2. [Schnellstart](#schnellstart)
3. [Schritt-für-Schritt: Build & Deploy](#schritt-für-schritt-build--deploy)
4. [Entwicklungsmodus](#entwicklungsmodus)
5. [Konfiguration](#konfiguration)
6. [Nutzung](#nutzung)
7. [Technische Übersicht](#technische-übersicht)
8. [Dateistruktur](#dateistruktur)

---

## Voraussetzungen

| Werkzeug | Mindestversion |
|----------|---------------|
| Docker | 24.x |
| Docker Compose (Plugin) | 2.x (`docker compose`) |

Internetzugang wird **nur beim ersten `docker compose build`** benötigt, um die Vendor-Assets
(HTMX, Alpine.js, Tailwind, JetBrains Mono) in das Image zu laden. Danach läuft alles offline.

---

## Schnellstart

```bash
git clone <repo-url>
cd local-clipboard
docker compose up -d
# App erreichbar unter http://localhost:8000
```

---

## Schritt-für-Schritt: Build & Deploy

### 1. Repository klonen

```bash
git clone <repo-url>
cd local-clipboard
```

### 2. Image bauen

```bash
docker compose build
```

Was dabei passiert:
- Python 3.11-Slim-Basis-Image wird gezogen
- System-Abhängigkeiten für Pillow werden installiert (`libjpeg`, `libwebp`)
- Python-Pakete aus `requirements.txt` werden installiert
- App-Code wird ins Image kopiert
- Vendor-Assets werden heruntergeladen und ins Image eingebettet:
  - `htmx.min.js` (unpkg.com)
  - `alpine.min.js` (unpkg.com)
  - `tailwind.js` (cdn.tailwindcss.com)
  - `JetBrainsMono-Regular.woff2` + `JetBrainsMono-Bold.woff2` (github.com)

Nach dem Build ist das Image vollständig offline-fähig.

### 3. Container starten

```bash
docker compose up -d
```

Docker Compose startet zwei Services:
- **app** — FastAPI-Anwendung auf Port `8000`
- **redis** — Redis 7 mit aktiviertem `appendonly` für Persistenz

Der `app`-Container wartet, bis Redis den Healthcheck besteht (`redis-cli ping`).

### 4. Erreichbarkeit prüfen

```bash
curl http://localhost:8000/health
# → {"status":"ok"}
```

Auf anderen Geräten im LAN die IP-Adresse des Hosts verwenden:

```
http://192.168.1.10:8000/b/mein-board
```

### 5. Logs ansehen

```bash
docker compose logs -f app     # App-Logs
docker compose logs -f redis   # Redis-Logs
```

### 6. Stoppen

```bash
docker compose down            # Container stoppen, Volumes bleiben erhalten
docker compose down -v         # + Volumes löschen (alle Daten weg)
```

### 7. Update (neues Image)

```bash
git pull
docker compose build --no-cache
docker compose up -d
```

---

## Entwicklungsmodus

Im Entwicklungsmodus wird `docker-compose.override.yml` automatisch eingelesen.
Der App-Code wird als Volume eingebunden — Änderungen am Code sind sofort wirksam,
ohne das Image neu zu bauen.

```bash
docker compose up
```

Uvicorn läuft mit `--reload`. Änderungen an `app/` werden live übernommen.
Redis-Daten und Bilder bleiben über Neustarts hinweg erhalten.

---

## Konfiguration

Umgebungsvariablen werden in `docker-compose.yml` gesetzt:

| Variable | Standard | Beschreibung |
|----------|----------|-------------|
| `REDIS_URL` | `redis://redis:6379` | Redis-Verbindungs-URL |
| `ENTRY_TTL_HOURS` | `48` | Lebensdauer eines Boards ohne Aktivität (Stunden) |
| `MAX_ENTRIES_PER_BOARD` | `20` | Max. Einträge pro Board (älteste werden automatisch entfernt) |
| `MAX_UPLOAD_SIZE_MB` | `5` | Max. Bildgröße pro Upload (MB) |

Anpassung direkt in `docker-compose.yml` oder via `.env`-Datei im Projektverzeichnis.

---

## Nutzung

### Board öffnen

Einfach eine URL mit beliebigem Board-Namen aufrufen:

```
http://<server>:8000/b/<board-name>
```

Board-Namen bestehen aus Kleinbuchstaben, Zahlen, `-` und `_` (2–50 Zeichen).
Ein Board wird automatisch angelegt, sobald der erste Eintrag erstellt wird.

### Text einfügen

1. Textarea anklicken (hat `autofocus`)
2. Text eingeben oder aus Zwischenablage einfügen (Strg+V)
3. **Strg+Enter** oder Klick auf **Senden**

### Screenshot pasten

1. Screenshot aufnehmen (z.B. mit Druck-Taste / Snipping Tool)
2. Textarea fokussieren
3. **Strg+V** — Bild wird automatisch hochgeladen (kein Klick auf Senden nötig)

### Board absichern (optionaler Key)

Über den **🔓 Key**-Button in der Header-Leiste:

1. Key generieren → Board ist ab sofort gesichert
2. Vollständige URL mit Key wird angezeigt und kann kopiert werden
3. Nur Personen mit der URL+Key sehen das Board

```
http://192.168.1.10:8000/b/work?key=xK9mP2qR4nL7vB3j
```

> **Hinweis:** Der Key ist in der URL sichtbar und erscheint in Browser-History und Server-Logs.
> Er schützt gegen versehentlichen Zugriff, nicht gegen gezielten Angriff im LAN.

---

## Technische Übersicht

### Stack

| Schicht | Technologie | Begründung |
|---------|-------------|------------|
| Backend | **FastAPI** (Python 3.11) | Async-native, SSE-Support, minimaler Boilerplate |
| Datenbank | **Redis 7** | Sorted Sets für History, Pub/Sub für Realtime |
| Realtime | **SSE** via `sse-starlette` | Unidirektionaler Push ohne WebSocket-Overhead |
| Templates | **Jinja2** | Server-side Rendering, kein Build-Step |
| Frontend | **HTMX + Alpine.js** | DOM-Updates ohne SPA-Komplexität |
| Styling | **Tailwind CSS** (Play CDN) | Dark Mode, kein Build-Prozess |
| Container | **Docker Compose** | App-Service + Redis-Service |

### Architektur

```
Browser ──HTTP──► FastAPI (Uvicorn)
                      │
                      ├── GET /b/{slug}      → HTML (Jinja2 SSR)
                      ├── POST /b/{slug}/entries → Entry speichern + SSE publish
                      ├── GET /b/{slug}/stream   → SSE (EventSource)
                      └── GET /b/{slug}/entries/{id}/image|download
                                │
                       Redis (aioredis)
                          │         │
                    Sorted Set    Pub/Sub
                  (Einträge +   (SSE Events
                    TTL 48h)    an Clients)
```

### Datenmodell (Redis)

```
board:{slug}:entries  →  Sorted Set   Score = Unix-Timestamp
                                       Member = Entry-JSON
                                       Max. 20 Einträge, TTL 48h

board:{slug}:authkey  →  String       Optionaler Key, TTL 48h

board:{slug}:channel  →  Pub/Sub      SSE-Broadcasting
```

### Entry-Format (JSON im Sorted Set)

```json
{
  "id": "uuid4-string",
  "type": "text | image",
  "content": "Klartext oder null",
  "image_path": "images/{uuid}.{ext}",
  "thumbnail": "base64-JPEG (~15 KB, 200px breit)",
  "mime": "image/png | image/jpeg",
  "file_size": 1234567,
  "created_at": "2024-01-15T14:30:00+00:00"
}
```

### Bild-Ablage (Hybrid)

```
Upload (Base64 via JSON)
       │
       ├── Original  →  data/images/{uuid}.{ext}   (Docker Volume, Disk)
       └── Thumbnail →  Redis Entry JSON            (200px, JPEG Q60, ~15 KB)
                         ↑
                    Sofort sichtbar in History, kein extra Request
```

Thumbnails bleiben im Redis-Entry, Originalbilder werden nur auf explizite Anfrage
(`/image` oder `/download`) vom Dateisystem geliefert.

### Realtime-Flow (SSE + Redis Pub/Sub)

```
Client A postet Entry
       │
       ▼
FastAPI: ZADD + Trim + EXPIRE (atomare Pipeline)
       │
       ▼
Redis PUBLISH board:{slug}:channel
       │
       ├──► Client A (EventSource)  → new_entry → DOM update
       └──► Client B (EventSource)  → new_entry → DOM update
```

Funktioniert auch bei mehreren Uvicorn-Workern, da Redis als zentraler Event-Bus dient.

### Atomare Redis-Operationen

Jedes neue Entry wird in einer einzigen atomaren Pipeline geschrieben:

```python
async with redis.pipeline(transaction=True) as pipe:
    await pipe.zadd(key, {entry_json: timestamp})      # Entry hinzufügen
    await pipe.zremrangebyrank(key, 0, -(MAX+1))       # Auf MAX trimmen
    await pipe.expire(key, TTL_SECONDS)                # TTL zurücksetzen
    await pipe.execute()
```

### API-Endpunkte

| Methode | Pfad | Beschreibung |
|---------|------|-------------|
| `GET` | `/` | Willkommensseite mit Board-Öffner |
| `GET` | `/health` | Redis-Check, für Docker-Healthcheck |
| `GET` | `/b/{slug}` | Board-Seite (HTML) |
| `POST` | `/b/{slug}/entries` | Neuen Eintrag erstellen |
| `DELETE` | `/b/{slug}/entries/{id}` | Eintrag löschen |
| `GET` | `/b/{slug}/entries/{id}/image` | Originalbild abrufen |
| `GET` | `/b/{slug}/entries/{id}/download` | Originalbild herunterladen |
| `GET` | `/b/{slug}/stream` | SSE-Stream (Realtime-Updates) |
| `POST` | `/b/{slug}/auth/generate` | Board-Key generieren |
| `DELETE` | `/b/{slug}/auth` | Board-Key entfernen |

---

## Dateistruktur

```
local-clipboard/
├── app/
│   ├── main.py              # FastAPI App, alle Routen
│   ├── redis_client.py      # Redis CRUD, Pub/Sub, Cleanup
│   ├── models.py            # Pydantic-Modelle
│   ├── auth.py              # Slug-Validierung, Key-Generierung
│   ├── static/
│   │   ├── vendor/          # HTMX, Alpine.js, Tailwind (im Build heruntergeladen)
│   │   ├── fonts/           # JetBrains Mono WOFF2 (im Build heruntergeladen)
│   │   └── app.css          # Eigene Styles
│   └── templates/
│       ├── base.html        # Layout, Asset-Imports, Favicon (inline SVG)
│       ├── board.html       # Haupt-UI
│       ├── index.html       # Willkommensseite
│       ├── auth_required.html
│       └── partials/
│           └── entry.html   # Entry-Fragment (SSR + HTMX + SSE)
├── data/
│   └── images/              # Bild-Storage (Docker Volume)
├── Dockerfile
├── docker-compose.yml
├── docker-compose.override.yml  # Dev: --reload + Volume-Mount
├── requirements.txt
├── CONCEPT.md
└── .gitignore
```
