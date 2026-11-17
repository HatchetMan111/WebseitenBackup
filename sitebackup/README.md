# SiteBackup – komplette Webseite als HTML-Dateien sichern

Lokale Backup-App für Proxmox LXC: Ein Job = eine Start-URL. Ablauf in
2 Phasen (wie web-check erst analysieren, dann sichern):

1. **Discovery:** alle Unterseiten herausfinden – via `robots.txt`
   (Sitemap-Zeilen) + `sitemap.xml` (inkl. Sitemap-Index) + Link-Crawl.
   Ergebnis: indexierte Liste (Nr, Seitenname/Titel, URL, Quelle).
2. **Backup:** Unterseite für Unterseite als **`.html`-Datei** sichern, mit
   Titel-Index (`mapping.json`) und Inhaltsverzeichnis (`index.html`).
   Bilder/CSS/JS werden mitgeladen und Links umgeschrieben → **offline lesbar**.
   Download **komplett als ZIP** oder **jede Seite einzeln**.
3. **Einzel-HTML:** die gesamte Website als **eine einzige, selbstenthaltene
   HTML-Datei** (`/download-html`): Assets als data-URI eingebettet (inkl.
   eingebettetem CSS mit eigenen Unter-Assets), alle Seiten als Kapitel mit
   klickbarem Inhaltsverzeichnis und Sprunganker-Navigation — offline lesbar
   und für immer archivierbar, auf jeder Plattform. Optional auch **pro
   Unterseite** (`/single-page?url=…`), dort zeigen interne Links auf die
   Original-URL.
4. **Suche:** Titel + Text aller gesicherten Seiten per Volltextsuche (FTS5)
   durchsuchen – Treffer öffnen die lokale Offline-Kopie.

Zeitplan pro Job: **manuell · stündlich · täglich · wöchentlich · monatlich · Cron**
(plant.seconds automatisch Discovery + Backup ein).

## Einzeiler (Proxmox-Host)

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/WebseitenBackup/main/install/sitebackup.sh)"
```

Das Script (Community-Scripts-Stil, `set -euo pipefail`, idempotent):
- erstellt LXC-Container (Default-ID **251**, 2 vCPU, 2 GB RAM, 8 GB Disk, `onboot: 1`)
- fragt optional ein **Web-UI-Passwort** ab (leer = offen im LAN)
- installiert Python 3 + venv + alle Pakete aus `sitebackup/requirements.txt` (gepinnte Versionen)
- richtet `sitebackup.service` ein (`enable`, `Restart=always`, `After=network-online.target`, Systemd-Hardening)
- verifiziert: `systemctl is-active` + `GET /api/health` + Web-UI-/Login-Check + Passwortschutz-Check
- gibt finale URL `http://[LXC-IP]:8090` aus

Bei Fehlern: komplette Kette (Stacktrace, journalctl, systemctl status) wird
ausgegeben. Trace-Re-run: `bash -x install/sitebackup.sh`.

## Nach der Installation

- Web UI: `http://[LXC-IP]:8090`
- Health: `http://[LXC-IP]:8090/api/health`
- Service: `systemctl status sitebackup` · Logs: `journalctl -u sitebackup -f`
- Daten: `/opt/sitebackup/data/jobs/<job-id>/pages/*.html` + `pages/assets/…` + `discovery.json` + `mapping.json` + `index.html` + `<job>_job-<id>_<datum>.zip`
- Container-Shell: `pct enter <CTID>`

## Bedienung (Dashboard)

1. **Schnellweg:** URL in „🚀 Website sichern“ eingeben → **⚡ Jetzt sichern**: legt einen Job (Name = Domain) an und startet das Backup sofort — mit **max. Seiten = 0 = alle Unterseiten finden**. Fortschritt als Balken in der Job-Karte.
2. Alternativ **+ Neuer Job** für eigene Einstellungen (max. Seiten/Tiefe, Zeitplan, Cron). Default: 1000 Seiten, **0 = alle** (bis 20.000, per `SITEBACKUP_MAX_PAGES_CAP` einstellbar).
3. **Schritt 1 „Seiten entdecken“:** listet alle Unterseiten indexiert mit Seitennamen (Quelle: Sitemap/Link), mit Filter-Suche. **Seiten-Auswahl:** alles ist vorausgewählt — einzelne Seiten abhaken, die nicht ins Archiv sollen, und dann „N ausgewählte Seiten sichern“.
4. **Schritt 2 „Backup starten“:** sichert jede Seite einzeln als HTML (Fortschrittsbalken + `[i/n]`-Log, alle 4 s aktualisiert). Jedes Backup startet mit frischer Discovery und räumt alte Dateien weg. `/` und `/index.html` sind dieselbe Seite (kein Duplikat).
5. Downloads pro Job: **🌐 1 HTML-Datei** (ganze Website, offline), **🗜 ZIP** (HTML + Assets + Index), **📄 Seiten** (Index mit 👁 Vorschau, 🧾 Code und ⬇ Einzelseite-als-HTML).
6. Volltextsuche über alle Archive (FTS5).

## 📖 Anleitung: Seiten ansehen, Code lesen, lokale Kopie nutzen

**Im Dashboard** (über „📄 Seiten" in der Job-Karte, jede gesicherte Zeile hat drei Aktionen):

| Was willst du? | Wie? |
|---|---|
| Die Seite **ansehen**, wie sie offline aussieht | **👁 Vorschau** — öffnet die gesicherte Seite in einem Fenster, inkl. Bilder/CSS; die Links zwischen den gesicherten Seiten funktionieren darin |
| Den **HTML-Quellcode ansehen** | **🧾 Code** — zeigt den exakten Inhalt der gespeicherten Datei als Klartext |
| Quellcode **kopieren** | 🧾 Code → **📋 Kopieren** |
| Die gespeicherte Datei **herunterladen** | 🧾 Code → **⬇ Datei herunterladen** (die .html-Datei, exakt wie im Archiv) |
| Eine Seite als **eine Datei mit allem** | **⬇ HTML** — selbstenthaltende Kopie dieser Seite (Bilder/CSS/Fonts eingebettet), läuft auf jedem Gerät |
| Die **ganze Website** sichern | **🌐 1 HTML-Datei** (alles in einer Datei, mit Inhaltsverzeichnis + Klick-Navigation) oder **🗜 ZIP** (alle Seiten + Assets + Index) |
| Im Archiv **suchen** | Such-Karte im Dashboard (Titel + Text aller gesicherten Seiten) |

**Als lokale Kopie verwenden — für immer, auf jeder Plattform:**

1. **Einfachster Weg (eine Datei):** „🌐 1 HTML-Datei" herunterladen. Diese eine `.html`-Datei enthält die komplette Website — Text, Bilder, CSS, Navigation. Einfach per Doppelklick im Browser öffnen: offline, auf Windows/Mac/Linux/Handy/Tablet, auch in vielen Jahren noch. Datei auf USB-Stick, Cloud oder Handy kopieren — fertig abgesichert, egal wo die Original-Website heute oder morgen gehostet ist.
2. **Mehrdatei-Archiv:** „🗜 ZIP" herunterladen und entpacken → die enthaltene `index.html` im Browser öffnen → das Inhaltsverzeichnis verlinkt alle gesicherten Seiten, innerhalb der Seiten funktionieren Bilder/CSS/Links über die relativen Pfade (`pages/…`, `pages/assets/…`). Wichtig ist nur, dass die Ordnerstruktur zusammen bleibt.
3. **Direkt auf dem Server:** Die Dateien liegen unter `data/jobs/<job-id>/pages/` (HTML + `assets/`), dazu `index.html` (Inhaltsverzeichnis), `single.html` (die Eine-Datei-Kopie), `mapping.json` (Titel-Index) und `discovery.json` (gefundene Seiten).

Was du unter 🧾 Code siehst, ist exakt der Inhalt deiner lokalen Kopie — kein umgeleitetes Online-Laden.

## Sicherheit

- **SSRF-Schutz:** es werden nur URLs mit öffentlich auflösbarem Host geholt
  (keine LAN-/Cloud-Metadaten-IPs, keine Zugangsdaten in URLs, Redirects werden
  einzeln validiert). Für Tests: `SITEBACKUP_ALLOW_PRIVATE=1`.
- **Optionales Passwort:** per Installer oder Datei `data/app.password` bzw.
  Env `SITEBACKUP_PASSWORD` (Session-Cookie, 30 Tage). Ohne Passwort ist die
  UI offen im LAN – bewusst so, bitte entsprechend absichern.
- **DoS-Schutz:** Downloads werden gestreamt (8 MB Cap pro Seite, 5 MB pro
  Sitemap, max. 25 Sitemap-Dateien), XML-Entities werden abgelehnt,
  Pfad-Traversal beim Datei-Download ist blockiert.
- Jobs, die es nicht gibt, liefern 404 (keine Verzeichnisse für fremde IDs).

## API (Auszug)

| Methode | Pfad | Beschreibung |
|---|---|---|
| `GET` | `/api/health` | Healthcheck |
| `GET/POST` | `/api/jobs` | Jobs listen / anlegen |
| `PUT/DELETE` | `/api/jobs/{id}` | Job ändern / löschen |
| `POST` | `/api/jobs/{id}/discover` | Phase 1: Unterseiten entdecken (indexierte Liste) |
| `GET` | `/api/jobs/{id}/discover` | Gespeicherte Discovery-Liste |
| `POST` | `/api/jobs/{id}/run` | Phase 2: Sofort-Backup aller Seiten — oder nur ausgewählter, mit Body `{"urls": ["https://…", …]}` |
| `GET` | `/api/runs?job_id=` | Läufe + Status |
| `GET` | `/api/runs/{id}` | Run inkl. Live-Log (`[i/n]`-Fortschritt) |
| `GET` | `/api/jobs/{id}/pages` | Index: Nr, Seitenname, URL, HTML-Datei |
| `GET` | `/api/jobs/{id}/index` | Inhaltsverzeichnis als HTML |
| `GET` | `/api/jobs/{id}/page?file=` | Einzelne HTML-Datei (Offline-Kopie) |
| `GET` | `/api/jobs/{id}/preview/{pfad}` | Ganze Offline-Kopie (HTML **und** Assets) für die In-App-Vorschau |
| `GET` | `/api/jobs/{id}/source?file=` | HTML-Quelltext einer gesicherten Seite als Klartext (Code-Ansicht) |
| `GET` | `/api/jobs/{id}/download` | Alles als ZIP (`<job>_job-<id>_<datum>.zip`) |
| `GET` | `/api/jobs/{id}/download-html` | **Gesamte Website als 1 selbstenthaltene HTML-Datei** (`<job>_job-<id>_<datum>.html`) |
| `GET` | `/api/jobs/{id}/single-page?url=` | Eine Unterseite als 1 selbstenthaltene HTML-Datei |
| `GET` | `/api/search?q=&job_id=` | Volltextsuche (Titel + Text, BM25, mit Snippet) |

Limits (per Env anpassbar): `SITEBACKUP_MAX_HTML_MB` (8), `SITEBACKUP_MAX_ASSET_MB` (10),
`SITEBACKUP_MAX_ASSETS_MB` (500 gesamt/Job), `SITEBACKUP_MAX_ASSETS` (2000),
`SITEBACKUP_MAX_PAGES_CAP` (20000, gilt wenn max_pages = 0), `SITEBACKUP_DELAY` (0.2 s
Höflichkeits-Pause zwischen Anfragen).

## Update

```bash
pct enter <CTID>
cd /opt/sitebackup && git pull 2>/dev/null || echo "per Re-Run des Installers aktualisieren"
systemctl restart sitebackup && systemctl status sitebackup --no-pager
```

Falls das Repo als Snapshot ohne `.git` installiert wurde: Installer erneut
laufen lassen (gleiche CT-ID → Abfrage „löschen und neu erstellen“).

## Deinstallation

```bash
# Nur App im Container stoppen/entfernen:
pct exec <CTID> -- bash -c 'systemctl disable --now sitebackup; rm -rf /opt/sitebackup'
# Ganzen Container löschen:
pct stop <CTID>; pct destroy <CTID> --purge
```

## Lokal entwickeln/testen

```bash
pip install -r sitebackup/requirements.txt
python -m pytest tests/ -q          # 59 Tests (ohne echtes Netzwerk)
python -m uvicorn app:app --host 0.0.0.0 --port 8090 --app-dir sitebackup
# UI: http://localhost:8090

# Dashboard-UI bauen (Build landet in sitebackup/static/):
cd web && npm install && npm run build
```

## Stack

Python 3.11+ · FastAPI · SQLite (SQLAlchemy) · httpx · BeautifulSoup4 ·
APScheduler (Cron/Wochen-/Monatspläne, reboot-sicher) · Web UI: React + Vite
(Fertig-Build committed in `static/`, kein Node im Container).
