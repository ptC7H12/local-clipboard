# Vendor Assets

Diese Dateien müssen **manuell heruntergeladen** werden, da die App im LAN ohne Internetzugang
funktionieren muss. Lege die Dateien direkt in diesem Verzeichnis ab:

| Datei | Quelle |
|-------|--------|
| `htmx.min.js` | https://unpkg.com/htmx.org/dist/htmx.min.js |
| `alpine.min.js` | https://unpkg.com/alpinejs/dist/cdn.min.js |
| `tailwind.js` | https://cdn.tailwindcss.com |

## Automatischer Download (Einmalig beim Build)

Der Dockerfile kann um einen Download-Schritt erweitert werden, falls beim Build
Internetzugang vorhanden ist. Für den Produktionsbetrieb im LAN sind die Dateien
lokal einzuchecken.

## Versionen (empfohlen)

- HTMX: 1.9.x oder 2.x
- Alpine.js: 3.x
- Tailwind Play CDN: aktuell (tailwindcss.com)
