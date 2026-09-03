# SiteBackup – komplette Webseite als HTML-Dateien sichern

Lokale Backup-App für Proxmox LXC: Ein Job = eine Start-URL. Der Crawler folgt
allen Unterseiten (gleiche Domain), speichert **jede Seite als `.html`-Datei**
und bietet alles als **ZIP-Download**. Zeitplan pro Job:
**manuell · stündlich · täglich · wöchentlich · monatlich · Cron**.

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
- Daten: `/opt/sitebackup/data/jobs/<job-id>/pages/*.html` + `mapping.json` + `job-<id>-backup.zip`
- Container-Shell: `pct enter <CTID>`

## Bedienung

1. Job anlegen: Name + Start-URL (z. B. `https://example.com`), max. Seiten (Default 100), Tiefe (Default 3).
2. Zeitplan wählen: z. B. wöchentlich Montag 03:00, monatlich am 1. um 02:00, oder Cron wie `0 3 * * 1`.
3. „Jetzt sichern“ für Sofort-Backup, danach Einzelseiten als HTML oder alles als ZIP laden.

## API (Auszug)

| Methode | Pfad | Beschreibung |
|---|---|---|
| `GET` | `/api/health` | Healthcheck |
| `GET/POST` | `/api/jobs` | Jobs listen / anlegen |
| `PUT/DELETE` | `/api/jobs/{id}` | Job ändern / löschen |
| `POST` | `/api/jobs/{id}/run` | Sofort-Backup starten |
| `GET` | `/api/runs?job_id=` | Läufe + Status |
| `GET` | `/api/jobs/{id}/pages` | Gesicherte URLs + Dateien |
| `GET` | `/api/jobs/{id}/page?file=` | Einzelne HTML-Datei |
| `GET` | `/api/jobs/{id}/download` | Alles als ZIP |

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
