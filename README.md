# WebseitenBackup (SiteBackup)

Komplette Webseite lokal sichern – für immer, auf jeder Plattform:

1. **Discovery:** alle Unterseiten herausfinden via `robots.txt` + `sitemap.xml`
   + Link-Crawl → indexierte Liste (Nr, Seitenname, URL, Quelle).
2. **Backup:** jede Unterseite einzeln als `.html`-Datei sichern, mit Titel-Index
   und Inhaltsverzeichnis. **Bilder/CSS/JS werden mitgeladen und Links
   umgeschrieben → offline lesbar.** Download komplett als ZIP
   (`<job>_job-<id>_<datum>.zip`) oder jede Seite einzeln.
3. **Einzel-HTML (Kern-Feature):** die **gesamte Website als EINE einzige
   HTML-Datei** herunterladen — alle Bilder/CSS/Fonts als data-URI eingebettet,
   alle Seiten als Kapitel mit klickbarem Inhaltsverzeichnis, Navigation offline.
   Diese eine Datei lässt sich für immer archivieren und auf jeder Plattform
   (Windows, Mac, Linux, Handy, USB-Stick, Cloud) öffnen — ganz egal, wo die
   Original-Website heute gehostet ist oder ob sie irgendwann verschwindet.
4. **Suche:** Volltextsuche (FTS5) über Titel + Text aller gesicherten Seiten.

Zeitplan pro Job: manuell · stündlich · täglich · wöchentlich · monatlich · Cron.
Details: [`sitebackup/README.md`](sitebackup/README.md).

## Web UI

React/Vite-Dashboard (dunkles Theme): URL eingeben → **⚡ Jetzt sichern** —
Discovery, Backup und Fortschritt laufen automatisch; danach lassen sich
**1 HTML-Datei**, **ZIP** oder einzelne Seiten (mit Offline-Vorschau inkl.
Bilder) herunterladen. Der Fertig-Build liegt committed unter
`sitebackup/static/` — der Container braucht **kein Node**.

## Installation auf Proxmox (Einzeiler)

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/WebseitenBackup/main/install/sitebackup.sh)"
```

Das Script erstellt einen LXC-Container (Default-ID 251, 2 vCPU, 2 GB RAM,
8 GB Disk), installiert die App als systemd-Service und gibt am Ende die URL
`http://[LXC-IP]:8090` aus.

## Layout

- `install/sitebackup.sh` – Proxmox-LXC-Installer (Community-Scripts-Stil)
- `sitebackup/app.py` – FastAPI-Backend, Crawler, Scheduler, Einzel-HTML-Export
- `sitebackup/static/` – gebaute React/Vite-UI (committed, kein Node im CT)
- `web/` – React/Vite-Quellcode des Dashboards (`npm run build` → `sitebackup/static/`)
- `sitebackup/sitebackup.service` – systemd-Unit
- `sitebackup/requirements.txt` – Python-Abhängigkeiten
- `tests/` – pytest-Suite (läuft ohne echtes Netzwerk)
