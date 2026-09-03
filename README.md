# WebseitenBackup (SiteBackup)

Komplette Webseite lokal sichern – in 2 Phasen (v1.1):

1. **Discovery:** alle Unterseiten herausfinden via `robots.txt` + `sitemap.xml`
   + Link-Crawl → indexierte Liste (Nr, Seitenname, URL, Quelle).
2. **Backup:** jede Unterseite einzeln als `.html`-Datei sichern, mit Titel-Index
   und Inhaltsverzeichnis. Download komplett als ZIP oder jede Seite einzeln.

Zeitplan pro Job: manuell · stündlich · täglich · wöchentlich · monatlich · Cron.
Details: [`sitebackup/README.md`](sitebackup/README.md).

## Installation auf Proxmox (Einzeiler)

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/WebseitenBackup/main/install/sitebackup.sh)"
```

Das Script erstellt einen LXC-Container (Default-ID 251, 2 vCPU, 2 GB RAM,
8 GB Disk), installiert die App als systemd-Service und gibt am Ende die URL
`http://[LXC-IP]:8090` aus.

## Layout

- `install/sitebackup.sh` – Proxmox-LXC-Installer (Community-Scripts-Stil)
- `sitebackup/app.py` – FastAPI-Backend, Crawler, Scheduler
- `sitebackup/static/index.html` – Web UI
- `sitebackup/sitebackup.service` – systemd-Unit
- `sitebackup/requirements.txt` – Python-Abhängigkeiten
