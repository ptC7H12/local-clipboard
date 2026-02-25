# LAN Clipboard — Implementierungskonzept (v2)

> **Zieldokument für KI-gestützte Implementierung**
> Dieses Dokument beschreibt vollständig die Architektur, Datenmodell, API, UI und Deployment einer selbst gehosteten LAN-Clipboard-Applikation. Abweichungen vom Konzept nur bei technischer Notwendigkeit — dann mit Kommentar im Code.

-----

## 1. Überblick

Eine minimalistische Webanwendung zum Teilen von **Text und Bildern (Screenshots)** im lokalen Netzwerk. Kein Account-System, keine externe Abhängigkeit. Mehrere unabhängige "Boards" werden über den URL-Pfad adressiert.

**Primärer Use-Case:**
PC1 öffnet `http://server:8000/b/work`, fügt einen Screenshot per Paste ein → PC2 öffnet dieselbe URL, sieht den Eintrag sofort (inkl. Thumbnail-Vorschau), klickt auf Download.

-----

## 2. Tech-Stack

| Schicht   | Technologie                 | Begründung                                               |
|-----------|-----------------------------|----------------------------------------------------------|
| Backend   | **FastAPI** (Python 3.11+)  | Async-native, SSE-Support, minimaler Boilerplate         |
| Storage   | **Redis 7**                 | Sorted Sets für History, Pub/Sub für Realtime, appendonly |
| Realtime  | **SSE** via `sse-starlette` | Unidirektionaler Push, kein WebSocket-Overhead            |
| Templates | **Jinja2**                  | Server-side Rendering, kein Build-Step                    |
| Frontend  | **HTMX + Alpine.js**        | DOM-Updates ohne SPA-Komplexität                          |
| Styling   | **Tailwind CSS**            | Dark Mode via `class`                                     |
| Container | **Docker Compose**          | App-Service + Redis-Service                               |

**Wichtig:** Kein Node.js, kein Build-Prozess. Alle JS/CSS-Assets werden **lokal gebundelt** im `app/static/`-Verzeichnis ausgeliefert (kein CDN — die App muss im LAN ohne Internetzugang funktionieren).

### Lokale Assets

Folgende Bibliotheken werden als Dateien in `app/static/vendor/` abgelegt:

- `htmx.min.js` (https://unpkg.com/htmx.org)
- `alpine.min.js` (https://unpkg.com/alpinejs)
- `tailwind.js` (https://cdn.tailwindcss.com) — Tailwind Play CDN als JS-Datei für Build-freies Setup

Die Schriftart **JetBrains Mono** wird ebenfalls lokal gehostet (`app/static/fonts/`).

-----

## 3. Projektstruktur

```
lan-clipboard/
├── app/
│   ├── main.py            # FastAPI App, Routen, Startup/Shutdown
│   ├── redis_client.py    # Redis-Logik (CRUD, Pub/Sub, TTL)
│   ├── models.py          # Pydantic-Modelle
│   ├── auth.py            # Key-Generierung & Validierung
│   ├── static/
│   │   ├── vendor/        # HTMX, Alpine.js, Tailwind (lokale Kopien)
│   │   ├── fonts/         # JetBrains Mono (WOFF2)
│   │   └── app.css        # Eigene Styles (minimal, ergänzend zu Tailwind)
│   └── templates/
│       ├── base.html      # Dark-Mode Layout, lokale Asset-Imports
│       ├── board.html     # Haupt-UI
│       └── partials/
│           └── entry.html # Einzelner Entry (für HTMX-Fragmente + SSR)
├── data/
│   └── images/            # Persistierter Bild-Storage (Docker-Volume)
├── docker-compose.yml
├── docker-compose.override.yml  # Dev-Overrides (--reload, Volume-Mounts)
├── Dockerfile
├── .gitignore
└── requirements.txt
```

-----

## 4. Datenmodell

### 4.1 Redis-Strukturen

```
board:{slug}:entries    → Redis Sorted Set  (Score = Unix-Timestamp, max. 20 Einträge)
board:{slug}:authkey    → Redis String (optional, nur gesetzt wenn Auth aktiv)
board:{slug}:channel    → Redis Pub/Sub Channel (für SSE-Broadcasting)
```

**TTL:** Jeder Board-Key (`board:{slug}:entries`, `board:{slug}:authkey`) erhält eine TTL von **48 Stunden**. Bei jeder Schreib-Operation (neuer Eintrag, Key-Änderung) wird die TTL zurückgesetzt. Boards ohne Aktivität verfallen automatisch.

### 4.2 Entry-Format (JSON, als Member im Sorted Set)

```json
{
  "id": "uuid4-string",
  "type": "text | image",
  "content": "Klartext (bei Text) ODER null (bei Bild)",
  "image_path": null | "images/{uuid}.png",
  "thumbnail": null | "base64-kodierter JPEG-Thumbnail (max. ~15 KB)",
  "mime": null | "image/png" | "image/jpeg",
  "created_at": "ISO8601-Timestamp"
}
```

### 4.3 Bild-Storage — Hybrid-Ansatz

Bilder werden **nicht** als Base64 in Redis gespeichert. Stattdessen:

1. **Originalbild** → Dateisystem unter `data/images/{uuid}.{ext}` (Docker-Volume)
2. **Thumbnail** → In Redis als kleiner Base64-JPEG (max. ~15 KB, 200px breit), inline im Entry-JSON
3. **Referenz** → `image_path` im Entry zeigt auf die Datei

**Begründung:**
- RAM-Verbrauch bleibt gering (nur Thumbnails im RAM, ~15 KB statt ~2,7 MB pro Bild)
- Thumbnails ermöglichen sofortige Vorschau in der Historie ohne zusätzliche Requests
- Originalbilder werden über einen dedizierten Endpoint ausgeliefert

**Thumbnail-Generierung (serverseitig):**
- Beim Upload wird das Bild serverseitig auf max. 200px Breite skaliert
- Format: JPEG mit Qualität 60 (guter Kompromiss zwischen Größe und Qualität)
- Bibliothek: `Pillow` (bereits im Python-Ökosystem Standard)

**Max. Upload-Größe:** 5 MB pro Bild (Validierung im Backend vor Speicherung).

### 4.4 Aufräum-Logik

Wenn durch `ZADD` + Trimming (nur die neuesten 20 behalten) Einträge aus dem Sorted Set entfernt werden, müssen zugehörige Bilddateien ebenfalls gelöscht werden. Dies geschieht synchron beim Trimming:

```python
async def trim_entries(redis, slug: str, max_entries: int = 20):
    # Alle Einträge außerhalb der Top-20 ermitteln
    removed = await redis.zrange(f"board:{slug}:entries", 0, -(max_entries + 1))
    if removed:
        for entry_json in removed:
            entry = json.loads(entry_json)
            if entry.get("image_path"):
                Path(f"data/{entry['image_path']}").unlink(missing_ok=True)
        await redis.zremrangebyrank(f"board:{slug}:entries", 0, -(max_entries + 1))
```

Zusätzlich: Ein Startup-Task prüft verwaiste Bilddateien (Dateien ohne Redis-Referenz) und räumt diese auf.

-----

## 5. API-Endpunkte

Alle Board-Routen laufen unter dem Prefix `/b/` um Kollisionen mit Framework-Routen (`/docs`, `/health` etc.) zu vermeiden.

### `GET /health`

- Prüft Redis-Verbindung (`PING`)
- Response: `{ "status": "ok" }` oder HTTP 503
- Wird für Docker-Healthcheck der App verwendet

### `GET /b/{slug}`

- Rendert `board.html` mit den letzten 20 Einträgen (Thumbnails inline, keine vollen Bilder)
- Query-Parameter: `?key=<string>` (optional, falls Board gesichert)
- Wenn Board einen Key hat und keiner übergeben → HTTP 401 mit Hinweisseite
- Slug-Validierung: nur `[a-z0-9][a-z0-9_-]*`, 2–50 Zeichen, keine reservierten Namen → sonst HTTP 400

### `POST /b/{slug}/entries`

- Body (JSON):
  - Text: `{ "type": "text", "content": "..." }`
  - Bild: `{ "type": "image", "content": "<base64-data>", "mime": "image/png" }`
- Validierung: Base64-dekodiert max. 5 MB, Mime-Type muss `image/png` oder `image/jpeg` sein
- Verarbeitung bei Bildern:
  1. Base64 dekodieren → Datei speichern unter `data/images/{uuid}.{ext}`
  2. Thumbnail generieren (200px breit, JPEG Q60) → Base64 für Redis-Entry
  3. Entry mit `image_path` und `thumbnail` in Redis speichern
- Redis: `ZADD` + Trim auf 20 (atomar via Pipeline)
- TTL auf `board:{slug}:entries` zurücksetzen (48h)
- Pub/Sub: Event `new_entry` an `board:{slug}:channel` publishen (nur Metadaten + Thumbnail, kein Vollbild)
- Response:
  - Bei `HX-Request`-Header → HTML-Fragment (gerendert aus `partials/entry.html`)
  - Sonst → JSON `{ "id": "...", "type": "...", "created_at": "..." }`

### `DELETE /b/{slug}/entries/{id}`

- Entfernt Eintrag aus dem Sorted Set (atomar via `ZREM` — Member wird über ID gesucht)
- Löscht zugehörige Bilddatei falls vorhanden
- Pub/Sub: Event `delete_entry` mit der ID
- Auth-Check: Falls Board einen Key hat, muss `?key=` stimmen

### `GET /b/{slug}/entries/{id}/image`

- Liefert das Originalbild vom Dateisystem
- `Content-Type` gemäß gespeichertem Mime-Type
- `Cache-Control: private, max-age=3600`
- Auth-Check: Falls Board einen Key hat, muss `?key=` stimmen

### `GET /b/{slug}/entries/{id}/download`

- Wie `/image`, aber mit `Content-Disposition: attachment; filename="clipboard_{timestamp}.{ext}"`
- Für den Download-Button in der UI

### `GET /b/{slug}/stream`

- SSE-Endpoint (`Content-Type: text/event-stream`)
- Nutzt Redis Pub/Sub (`SUBSCRIBE board:{slug}:channel`)
- Events:
  - `new_entry` → Daten: gerendertes HTML-Fragment (mit Thumbnail, ohne Vollbild)
  - `delete_entry` → Daten: `{ "id": "..." }`
- Cleanup: Bei Client-Disconnect wird der Pub/Sub-Subscriber sauber beendet (`try/except asyncio.CancelledError`)

### `POST /b/{slug}/auth/generate`

- **Nur erlaubt wenn:** Board noch keinen Key hat ODER bestehender Key als `?key=` übergeben wird
- Generiert einen zufälligen Key (16 Zeichen, URL-sicher, `secrets.token_urlsafe`)
- Speichert in Redis mit 48h TTL
- Response: `{ "key": "..." }`

### `DELETE /b/{slug}/auth`

- **Erfordert** den bestehenden Key als `?key=`
- Entfernt den Key → Board wieder öffentlich
- Response: HTTP 204

### Slug-Validierung

```python
import re

RESERVED_SLUGS = {"health", "docs", "redoc", "openapi.json", "static", "favicon.ico", "api"}
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,49}$")

def validate_slug(slug: str) -> bool:
    return slug not in RESERVED_SLUGS and SLUG_PATTERN.match(slug) is not None
```

-----

## 6. Auth-Konzept

- Auth ist **pro Board, optional**
- Ein Board ohne Key ist für alle im LAN zugänglich
- Wenn ein Key gesetzt ist: jede Anfrage (GET, POST, DELETE, SSE) muss `?key=<value>` tragen
- Der Key wird **nicht** in einem Cookie gespeichert — er ist Teil der URL
- Key-Generierung und -Löschung sind geschützt (siehe API-Endpunkte oben)
- UI zeigt die vollständige URL mit Key an und bietet einen **"URL kopieren"-Button**

```
http://192.168.1.10:8000/b/work?key=xK9mP2qR4nL7vB3j
                           [URL kopieren]
```

**Sicherheitshinweis (im Code als Kommentar):** Das ist kein kryptografisches Auth-System. Es schützt gegen versehentlichen Zugriff, nicht gegen gezielten Angriff im LAN (Key ist in URL sichtbar, in Logs, Browser-History).

-----

## 7. Frontend — UI-Spezifikation

### Design-Vorgaben

- **Nur Dark Mode** — kein Toggle, kein `prefers-color-scheme`
- **Farbpalette:** Hintergrund `#0f0f0f`, Cards `#1a1a1a`, Akzent `#e2e2e2`, CTA/Highlight `#6ee7b7` (mint-grün)
- **Typografie:**
  - `JetBrains Mono` für Code-Inhalte und Clipboard-Content
  - System-Font-Stack (`system-ui, -apple-system, sans-serif`) für UI-Elemente (Buttons, Labels, Header)
- **Keine Icons-Library** — Unicode-Zeichen reichen
- **Animationen:** nur `transition` für Hover-States, keine Page-Transitions

### Layout

```
┌──────────────────────────────────────────────────────────┐
│  /b/work                                 [Key]  [URL]    │  ← Header, sticky top
├──────────────────────────────────────────────────────────┤
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Text hier einfügen oder Screenshot pasten       │   │  ← Textarea, autofocus
│  │                                                  │   │     paste-Event für Bilder
│  └──────────────────────────────────────────────────┘   │
│                                           [Senden]       │
│                                                          │
├──────────────────────────────────────────────────────────┤
│  Letzte Einträge                              ← sticky  │
│ ┌──────────────────────────────────────────────────────┐ │
│ │  (scrollbarer Bereich)                               │ │
│ │                                                      │ │
│ │  ┌──────────────────────────────────────────────┐   │ │
│ │  │ [T]  SELECT * FROM users WHERE...        [C] │   │ │  ← Text: Copy-Button
│ │  └──────────────────────────────────────────────┘   │ │
│ │  ┌──────────────────────────────────────────────┐   │ │
│ │  │ [IMG] ┌────────┐  screenshot_2024...  [DL]  │   │ │  ← Bild: Thumbnail +
│ │  │       │  thumb  │  12:34 · 1.2 MB     [X]   │   │ │     Download-Button
│ │  │       │  200px  │                            │   │ │
│ │  │       └────────┘                             │   │ │
│ │  └──────────────────────────────────────────────┘   │ │
│ │  ...                                                │ │
│ └──────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

**Layout-Details:**
- Header und Eingabebereich sind sticky oben
- Die History-Liste scrollt unabhängig (`overflow-y: auto`, `flex-grow: 1`)
- Auf großen Bildschirmen bleibt das Layout zentriert (`max-width: 800px`, `margin: auto`)

### Interaktionen im Detail

**Text einfügen:**

1. User öffnet Board → Textarea hat `autofocus`
2. Strg+V → Text landet in Textarea
3. Klick auf "Senden" oder Strg+Enter → `POST /b/{slug}/entries`
4. HTMX prepended das Response-HTML-Fragment an `#history-list`
5. Textarea wird geleert

**Bild einfügen (Screenshot-Workflow):**

1. User drückt Druck-Taste → Bild im System-Clipboard
2. User fokussiert die Textarea, drückt Strg+V
3. JavaScript `paste`-Event fängt `items` ab, erkennt `image/*`
4. `FileReader.readAsDataURL()` → Base64-String
5. Client-seitige Größenprüfung (max. 5 MB)
6. Direkt POST ohne Klick auf "Senden" → Upload läuft automatisch
7. Feedback: "Uploading…"-Overlay auf dem Eingabebereich
8. Response-Fragment wird in History eingefügt (mit Thumbnail)

**Bild in der Historie:**

- **Thumbnail:** `<img>` mit `max-height: 120px`, lädt aus dem inline Base64-Thumbnail im Entry-JSON — sofort sichtbar, kein extra Request
- **Klick auf Thumbnail:** Modal-Overlay (Alpine.js `x-show`) lädt das Vollbild vom Server (`/b/{slug}/entries/{id}/image`) — erst bei Bedarf
- **Download-Button:** `<a href="/b/{slug}/entries/{id}/download">` — sauberer Download über den Server-Endpoint, kein data:-URI
- **Metadaten:** Dateigröße und Zeitstempel werden unter dem Thumbnail angezeigt

**Copy-Button (Text):**

```javascript
navigator.clipboard.writeText(content)
// Visuelles Feedback: Button-Text wechselt kurz zu "Kopiert"
```

**Realtime (SSE):**

```javascript
const source = new EventSource(`/b/${slug}/stream${key ? '?key='+key : ''}`)

source.addEventListener('new_entry', (e) => {
  // HTML-Fragment direkt ins DOM prependen
  const list = document.getElementById('history-list')
  list.insertAdjacentHTML('afterbegin', e.data)
  htmx.process(list.firstElementChild)  // HTMX-Attribute aktivieren
})

source.addEventListener('delete_entry', (e) => {
  const { id } = JSON.parse(e.data)
  document.getElementById(`entry-${id}`)?.remove()
})

// Reconnect bei Verbindungsabbruch (EventSource macht das automatisch)
```

-----

## 8. Paste-Event Logik (JavaScript)

```javascript
textarea.addEventListener('paste', async (e) => {
  const items = e.clipboardData?.items
  if (!items) return

  for (const item of items) {
    if (item.type.startsWith('image/')) {
      e.preventDefault()
      const blob = item.getAsFile()
      if (blob.size > 5 * 1024 * 1024) {
        showError('Bild zu groß (max. 5 MB)')
        return
      }
      showUploadOverlay()
      try {
        const base64 = await toBase64(blob)
        await postEntry({ type: 'image', content: base64, mime: item.type })
      } catch (err) {
        showError('Upload fehlgeschlagen')
      } finally {
        hideUploadOverlay()
      }
      return
    }
  }
  // Falls kein Bild → normales Text-Paste in Textarea, nichts tun
})

function toBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result.split(',')[1])
    reader.onerror = () => reject(new Error('FileReader failed'))
    reader.readAsDataURL(blob)
  })
}
```

-----

## 9. SSE-Broadcasting via Redis Pub/Sub

Um auch bei mehreren Uvicorn-Workern korrekt zu funktionieren, wird Redis Pub/Sub als zentraler Event-Bus verwendet.

### Publish (bei neuem/gelöschtem Eintrag)

```python
# In redis_client.py
async def publish_event(slug: str, event_type: str, data: str):
    """Published ein Event an alle SSE-Subscriber eines Boards."""
    channel = f"board:{slug}:channel"
    message = json.dumps({"event": event_type, "data": data})
    await redis.publish(channel, message)
```

### Subscribe (SSE-Endpoint)

```python
# In main.py — SSE-Generator
async def board_event_generator(slug: str, key: str | None):
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"board:{slug}:channel")
    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                payload = json.loads(message["data"])
                yield ServerSentEvent(
                    data=payload["data"],
                    event=payload["event"]
                )
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe(f"board:{slug}:channel")
        await pubsub.close()
```

**Hinweis:** Bei Single-Worker-Betrieb funktioniert das identisch — Redis Pub/Sub ist auch lokal effizient. Kein Nachteil gegenüber In-Process-Lösung.

-----

## 10. Dockerfile & Docker Compose

### Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app

# System-Dependencies für Pillow (Thumbnail-Generierung)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo-dev libwebp-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Bild-Verzeichnis anlegen
RUN mkdir -p /app/data/images

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### docker-compose.yml

```yaml
services:
  app:
    build: .
    ports:
      - "8000:8000"
    depends_on:
      redis:
        condition: service_healthy
    environment:
      - REDIS_URL=redis://redis:6379
      - ENTRY_TTL_HOURS=48
      - MAX_ENTRIES_PER_BOARD=20
      - MAX_UPLOAD_SIZE_MB=5
    volumes:
      - image_data:/app/data/images
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 3
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped

volumes:
  redis_data:
  image_data:
```

### docker-compose.override.yml (Dev)

```yaml
services:
  app:
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
    volumes:
      - ./app:/app/app          # Live-Reload bei Code-Änderungen
      - image_data:/app/data/images
```

### requirements.txt

```
fastapi>=0.111
uvicorn[standard]>=0.29
redis>=5.0
sse-starlette>=1.8
jinja2>=3.1
Pillow>=10.0
```

-----

## 11. Randbedingungen & Hinweise für die Implementierung

1. **Redis-Verbindung** — bei Start einmal `await redis.ping()` prüfen, bei Fehler mit lesbarer Fehlermeldung crashen (nicht silent)
2. **Slug-Validierung** — nur `[a-z0-9][a-z0-9_-]*`, 2–50 Zeichen, keine reservierten Slugs, sonst HTTP 400
3. **Board-Prefix** — alle Board-Routen unter `/b/{slug}` um Kollisionen mit `/docs`, `/health` etc. zu vermeiden
4. **Content-Size** — im Request-Handler prüfen bevor Redis-Write oder Datei-Schreib
5. **Atomare Redis-Operationen** — ZADD + ZREMRANGEBYRANK + EXPIRE immer in einer Pipeline/Transaction:
   ```python
   async with redis.pipeline(transaction=True) as pipe:
       await pipe.zadd(key, {entry_json: timestamp})
       await pipe.zremrangebyrank(key, 0, -(max_entries + 1))
       await pipe.expire(key, ttl_seconds)
       await pipe.execute()
   ```
6. **HTMX-Fragments** — `partials/entry.html` wird sowohl beim SSR (board.html include) als auch standalone (POST-Response, SSE-Event) gerendert
7. **SSE-Cleanup** — bei Client-Disconnect Pub/Sub-Subscriber sauber beenden (`try/except asyncio.CancelledError`)
8. **Bild-Cleanup** — beim Trimmen der History und beim TTL-Ablauf verwaiste Bilddateien aufräumen (Startup-Task)
9. **CORS** — nicht nötig, alles same-origin
10. **HTTPS** — bewusst nicht in Scope, LAN-only
11. **Keine Datenbank-Migrations** — Redis-Schema ist implizit
12. **Logging** — `uvicorn` Access-Log reicht, kein extra Logger
13. **Board "erstellen"** — kein expliziter Create-Endpoint; ein Board existiert sobald der erste Eintrag gepostet wird
14. **Root-Route** — `GET /` zeigt eine einfache Willkommensseite mit Erklärung und Beispiel-Link
15. **Favicon** — Inline-SVG im `<head>` von `base.html`, kein separates File
16. **HX-Request-Header** — POST-Endpoints prüfen den `HX-Request`-Header: wenn vorhanden → HTML-Fragment, sonst → JSON

-----

## 12. Nicht in Scope

- User-Accounts oder Sessions
- HTTPS / TLS-Terminierung (soll vorgelagert per Reverse-Proxy erfolgen falls gewünscht)
- Mobile-optimiertes Layout (Desktop-first, muss aber auf Tablet funktionieren)
- Export / Backup der Einträge
- Rate-Limiting
- Mehrsprachigkeit

-----

## 13. Änderungen gegenüber v1

| # | Thema | v1 | v2 | Begründung |
|---|-------|----|----|------------|
| 1 | Assets | CDN | Lokale Dateien in `static/vendor/` | LAN-only muss ohne Internet funktionieren |
| 2 | Bild-Storage | Base64 in Redis | Dateisystem + Thumbnail in Redis | RAM-Verbrauch (2,7 MB/Bild → ~15 KB/Thumbnail) |
| 3 | Bild-Vorschau | Kein Thumbnail | Serverseitiger JPEG-Thumbnail (200px) | Bilder in Historie sofort erkennbar |
| 4 | SSE-Daten | Volle Einträge (inkl. Bilder) | Nur HTML-Fragmente mit Thumbnail | 13 MB Traffic pro Paste → wenige KB |
| 5 | Redis-Datenstruktur | List (LPUSH/LTRIM) | Sorted Set (ZADD/ZREM) | Atomares Delete, kein Race-Condition |
| 6 | Redis-Transaktionen | Keine | Pipeline mit `transaction=True` | ZADD+TRIM+EXPIRE atomar |
| 7 | SSE-Broadcasting | In-Process (implizit) | Redis Pub/Sub | Multi-Worker-fähig |
| 8 | TTL | Keine | 48h pro Board | Automatische Bereinigung inaktiver Boards |
| 9 | Board-Routen | `/{slug}` | `/b/{slug}` | Keine Slug-Kollision mit Framework-Routen |
| 10 | Auth-Schutz | Jeder kann Key setzen/löschen | Key-Generierung/Löschung erfordert bestehenden Key | Verhindert Board-Hijacking |
| 11 | Bild-Download | `data:`-URI im `href` | Dedizierter Download-Endpoint | Browser-kompatibel, speicherschonend |
| 12 | Typografie | JetBrains Mono für alles | Mono nur für Content, System-Font für UI | Bessere Lesbarkeit der UI-Elemente |
| 13 | Upload-Limit | 2 MB | 5 MB | Praxistauglicher für hochauflösende Screenshots |
| 14 | Health-Endpoint | Keiner | `GET /health` | Docker-Healthcheck für App-Container |
| 15 | Pillow | Nicht vorhanden | Neue Dependency | Thumbnail-Generierung serverseitig |
| 16 | Dev-Setup | Keines | `docker-compose.override.yml` | Live-Reload bei Entwicklung |
| 17 | Layout | "Kein Scroll" | Scrollbare History-Liste | Realistisch für 20 Einträge mit Thumbnails |
| 18 | python-multipart | In requirements | Entfernt | Wird nicht benötigt (kein File-Upload) |
