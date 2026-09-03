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
3. **Suche:** Titel + Text aller gesicherten Seiten per Volltextsuche (FTS5)
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

## Bedienung

1. Job anlegen (oder per „Bearbeiten“ ändern): Name + Start-URL (z. B. `https://example.com`), max. Seiten (Default 100), Tiefe (Default 3).
2. **Schritt 1 „Seiten entdecken“:** listet alle Unterseiten indexiert mit Seitennamen (Quelle: Sitemap/Link), mit Filter-Suche.
3. **Schritt 2 „Backup starten“:** sichert jede Seite einzeln als HTML (Fortschrittsbalken + `[i/n]`-Log, alle 3 s aktualisiert). Jedes Backup startet mit frischer Discovery und räumt alte Dateien weg.
4. Laden: **komplett als ZIP** (inkl. Index) oder **jede Unterseite einzeln** als HTML; Zeitplan für Automatik wählen (z. B. wöchentlich Montag 03:00 oder Cron `0 3 * * 1`).

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
| `POST` | `/api/jobs/{id}/run` | Phase 2: Sofort-Backup Seite für Seite |
| `GET` | `/api/runs?job_id=` | Läufe + Status |
| `GET` | `/api/runs/{id}` | Run inkl. Live-Log (`[i/n]`-Fortschritt) |
| `GET` | `/api/jobs/{id}/pages` | Index: Nr, Seitenname, URL, HTML-Datei |
| `GET` | `/api/jobs/{id}/index` | Inhaltsverzeichnis als HTML |
| `GET` | `/api/jobs/{id}/page?file=` | Einzelne HTML-Datei (Offline-Kopie) |
| `GET` | `/api/jobs/{id}/download` | Alles als ZIP (`<job>_job-<id>_<datum>.zip`) |
| `GET` | `/api/search?q=&job_id=` | Volltextsuche (Titel + Text, BM25, mit Snippet) |

Limits (per Env anpassbar): `SITEBACKUP_MAX_HTML_MB` (8), `SITEBACKUP_MAX_ASSET_MB` (10),
`SITEBACKUP_MAX_ASSETS_MB` (500 gesamt/Job), `SITEBACKUP_MAX_ASSETS` (2000).

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
python -m uvicorn app:app --host 0.0.0.0 --port 8090 --app-dir sitebackup
# UI: http://localhost:8090
```

## Stack

Python 3.11+ · FastAPI · SQLite (SQLAlchemy) · httpx · BeautifulSoup4 ·
APScheduler (Cron/Wochen-/Monatspläne, reboot-sicher) · Web UI als statisches
HTML (kein Node-Build nötig, bewusst schlank für LXC).
