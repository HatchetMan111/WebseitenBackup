# SiteBackup – komplette Webseite als HTML-Dateien sichern

Lokale Backup-App für Proxmox LXC: Ein Job = eine Start-URL. Ablauf in
2 Phasen (wie web-check erst analysieren, dann sichern):

1. **Discovery:** alle Unterseiten herausfinden – via `robots.txt`
   (Sitemap-Zeilen) + `sitemap.xml` (inkl. Sitemap-Index) + Link-Crawl.
   Ergebnis: indexierte Liste (Nr, Seitenname/Titel, URL, Quelle).
2. **Backup:** Unterseite für Unterseite als **`.html`-Datei** sichern, mit
   Titel-Index (`mapping.json`) und Inhaltsverzeichnis (`index.html`).
   Download **komplett als ZIP** oder **jede Seite einzeln**.

Zeitplan pro Job: **manuell · stündlich · täglich · wöchentlich · monatlich · Cron**
(plant.seconds automatisch Discovery + Backup ein).

## Einzeiler (Proxmox-Host)

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/WebseitenBackup/main/install/sitebackup.sh)"
```

Das Script (Community-Scripts-Stil, `set -euo pipefail`, idempotent):
- erstellt LXC-Container (Default-ID **251**, 2 vCPU, 2 GB RAM, 8 GB Disk, `onboot: 1`)
- installiert Python 3 + venv + alle Pakete aus `sitebackup/requirements.txt`
- richtet `sitebackup.service` ein (`enable`, `Restart=always`, `After=network-online.target`)
- verifiziert: `systemctl is-active` + `GET /api/health` + Web-UI-Check (HTTP 200)
- gibt finale URL `http://[LXC-IP]:8090` aus

Bei Fehlern: komplette Kette (Stacktrace, journalctl, systemctl status) wird
ausgegeben. Trace-Re-run: `bash -x install/sitebackup.sh`.

## Nach der Installation

- Web UI: `http://[LXC-IP]:8090`
- Health: `http://[LXC-IP]:8090/api/health`
- Service: `systemctl status sitebackup` · Logs: `journalctl -u sitebackup -f`
- Daten: `/opt/sitebackup/data/jobs/<job-id>/pages/*.html` + `discovery.json` + `mapping.json` + `index.html` + `job-<id>-backup.zip`
- Container-Shell: `pct enter <CTID>`

## Bedienung

1. Job anlegen: Name + Start-URL (z. B. `https://example.com`), max. Seiten (Default 100), Tiefe (Default 3).
2. **Schritt 1 „Seiten entdecken“:** listet alle Unterseiten indexiert mit Seitennamen (Quelle: Sitemap/Link).
3. **Schritt 2 „Backup starten“:** sichert jede Seite einzeln als HTML (Live-Fortschritt `[i/n]` im Log).
4. Laden: **komplett als ZIP** (inkl. Index) oder **jede Unterseite einzeln** als HTML; Zeitplan für Automatik wählen (z. B. wöchentlich Montag 03:00 oder Cron `0 3 * * 1`).

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
| `GET` | `/api/jobs/{id}/page?file=` | Einzelne HTML-Datei |
| `GET` | `/api/jobs/{id}/download` | Alles als ZIP (HTML + Index + Mapping) |

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
