"""SiteBackup – komplette Webseiten lokal als HTML-Dateien sichern.

Ablauf pro Job (2 Phasen, wie web-check erst analysieren, dann sichern):
1. DISCOVERY: alle Unterseiten herausfinden – via robots.txt (Sitemap-Zeilen)
   + sitemap.xml (inkl. Sitemap-Index) + BFS-Link-Crawl. Ergebnis ist eine
   indexierte Liste (Nr, Seitenname/Titel, URL, Quelle) in discovery.json.
2. BACKUP: Unterseite fuer Unterseite als .html-Datei unter
   data/jobs/<id>/pages/ sichern, mit Titel-Index (mapping.json) und
   Inhaltsverzeichnis (index.html). Download komplett als ZIP oder einzeln.
- Zeitplan: manuell/stuendlich/taeglich/woechentlich/monatlich/cron
  (APScheduler, reboot-sicher via DB + Scheduler).
- Web UI: statische Datei static/index.html, keine Build-Tools noetig.
"""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import os
import re
import threading
import traceback
import xml.etree.ElementTree as ET
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import (Boolean, DateTime, Integer, String, Text, create_engine,
                        select)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("SITEBACKUP_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
DB_URL = os.getenv("SITEBACKUP_DB_URL", f"sqlite:///{DATA_DIR / 'sitebackup.db'}")
PORT = int(os.getenv("SITEBACKUP_PORT", "8090"))

engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    start_url: Mapped[str] = mapped_column(String(2000))
    max_pages: Mapped[int] = mapped_column(Integer, default=100)
    max_depth: Mapped[int] = mapped_column(Integer, default=3)
    same_domain: Mapped[bool] = mapped_column(Boolean, default=True)
    schedule: Mapped[str] = mapped_column(String(20), default="manual")  # manual|hourly|daily|weekly|monthly|cron
    schedule_time: Mapped[str] = mapped_column(String(10), default="03:00")  # HH:MM
    schedule_weekday: Mapped[str] = mapped_column(String(10), default="mon")  # mon..sun (weekly)
    schedule_day: Mapped[int] = mapped_column(Integer, default=1)  # 1..28 (monthly)
    cron_expr: Mapped[str] = mapped_column(String(100), default="")  # nur bei schedule=cron
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|ok|error
    pages_ok: Mapped[int] = mapped_column(Integer, default=0)
    pages_failed: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[str] = mapped_column(Text, default="")


Base.metadata.create_all(engine)

app = FastAPI(title="SiteBackup", version="1.1.0")

# ---------------------------------------------------------------- Modelle

SCHEDULES = ("manual", "hourly", "daily", "weekly", "monthly", "cron")
WEEKDAYS = {"mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu", "fri": "fri", "sat": "sat", "sun": "sun"}


class JobIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    start_url: str = Field(min_length=8, max_length=2000)
    max_pages: int = Field(default=100, ge=1, le=5000)
    max_depth: int = Field(default=3, ge=0, le=10)
    same_domain: bool = True
    schedule: str = "weekly"
    schedule_time: str = "03:00"
    schedule_weekday: str = "mon"
    schedule_day: int = Field(default=1, ge=1, le=28)
    cron_expr: str = ""
    enabled: bool = True

    def normalized(self) -> dict:
        sched = (self.schedule or "manual").lower()
        if sched not in SCHEDULES:
            raise ValueError(f"schedule muss einer von {SCHEDULES} sein")
        if not re.match(r"^https?://", self.start_url.strip()):
            raise ValueError("start_url muss mit http:// oder https:// beginnen")
        if not re.match(r"^\d{2}:\d{2}$", self.schedule_time or ""):
            raise ValueError("schedule_time muss HH:MM sein (z.B. 03:00)")
        wd = (self.schedule_weekday or "mon").lower()[:3]
        if wd not in WEEKDAYS:
            raise ValueError("schedule_weekday muss mon..sun sein")
        if sched == "cron" and not (self.cron_expr or "").strip():
            raise ValueError("cron_expr fehlt (z.B. '0 3 * * 1' = montags 03:00)")
        return {
            "name": self.name.strip(),
            "start_url": self.start_url.strip(),
            "max_pages": self.max_pages,
            "max_depth": self.max_depth,
            "same_domain": self.same_domain,
            "schedule": sched,
            "schedule_time": self.schedule_time,
            "schedule_weekday": wd,
            "schedule_day": self.schedule_day,
            "cron_expr": self.cron_expr.strip(),
            "enabled": self.enabled,
        }


def serialize_job(j: Job) -> dict:
    return {
        "id": j.id, "name": j.name, "start_url": j.start_url,
        "max_pages": j.max_pages, "max_depth": j.max_depth,
        "same_domain": j.same_domain, "schedule": j.schedule,
        "schedule_time": j.schedule_time, "schedule_weekday": j.schedule_weekday,
        "schedule_day": j.schedule_day, "cron_expr": j.cron_expr,
        "enabled": j.enabled,
        "next_run": next_run_iso(j),
        "created_at": j.created_at.isoformat() if j.created_at else None,
    }


def serialize_run(r: Run) -> dict:
    return {
        "id": r.id, "job_id": r.job_id,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "status": r.status, "pages_ok": r.pages_ok, "pages_failed": r.pages_failed,
        "total_bytes": r.total_bytes, "error": r.error,
    }


# ---------------------------------------------------------------- Helpers

def job_dir(job_id: int) -> Path:
    d = JOBS_DIR / str(job_id)
    (d / "pages").mkdir(parents=True, exist_ok=True)
    return d


def url_to_filename(url: str) -> str:
    """Abbilden: https://example.com/a/b?x=1 -> a/b_<hash8>.html ; / -> index.html"""
    p = urlparse(url)
    path = p.path.strip("/") or "index"
    # Query in Dateinamen einarbeiten (gehasht, kurz)
    suffix = ""
    if p.query:
        suffix = "_" + hashlib.sha256(p.query.encode()).hexdigest()[:8]
    # Pfad saeubern
    safe = re.sub(r"[^A-Za-z0-9._/-]", "_", path).strip("/_") or "index"
    if safe.endswith(".html") or safe.endswith(".htm"):
        stem = safe + suffix
    else:
        stem = safe + suffix + ".html"
    # Verzeichnisse erhalten, aber max. 180 Zeichen
    parts = [re.sub(r"_+", "_", x)[:60] or "_" for x in stem.split("/") if x not in ("", ".", "..")]
    return "/".join(parts[-6:]) or "index.html"


def same_site(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    ha, hb = (pa.hostname or "").lower(), (pb.hostname or "").lower()
    if ha == hb:
        return True
    # Subdomains zulassen: www.example.com <-> example.com
    strip = lambda h: re.sub(r"^www\.", "", h)
    return strip(ha) == strip(hb) or ha.endswith("." + strip(hb)) or hb.endswith("." + strip(ha))


def extract_links(html: str, base: str) -> list[str]:
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    out: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = (tag.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        abs_url, _ = urldefrag(urljoin(base, href))
        pu = urlparse(abs_url)
        if pu.scheme not in ("http", "https"):
            continue
        out.append(abs_url)
    # Reihenfolge stabil, Duplikate raus
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def extract_title(html: str, url: str) -> str:
    """Seitenname aus <title>; Fallback: sprechender Name aus URL-Pfad."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.get_text(strip=True):
            return soup.title.get_text(strip=True)[:200]
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            return h1.get_text(strip=True)[:200]
    except Exception:
        pass
    p = urlparse(url)
    seg = (p.path.strip("/") or "Startseite").split("/")[-1]
    seg = re.sub(r"\.(html?|php|aspx?)$", "", seg)
    name = seg.replace("-", " ").replace("_", " ").strip().title() or "Startseite"
    return name[:200]


def page_name_for(url: str, title: str) -> str:
    """Index-Name: Titel bevorzugen, sonst URL-Pfad."""
    return title or extract_title("", url)


def fetch_robots_sitemaps(client: httpx.Client, start_url: str) -> tuple[list[str], list[str]]:
    """robots.txt lesen (wie web-check 'Crawl Rules'): Sitemap-Zeilen + Disallows.

    Gibt (sitemap_urls, disallows) zurueck. Fehler -> leere Listen (tolerant).
    """
    p = urlparse(start_url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    sitemaps: list[str] = []
    disallows: list[str] = []
    try:
        resp = client.get(robots_url)
        if resp.status_code != 200:
            return [], []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if re.match(r"(?i)^sitemap\s*:", line):
                sm = line.split(":", 1)[1].strip()
                if sm.startswith("http"):
                    sitemaps.append(sm)
            elif re.match(r"(?i)^disallow\s*:", line):
                dis = line.split(":", 1)[1].strip()
                if dis:
                    disallows.append(dis)
    except Exception:
        pass
    return sitemaps, disallows


def parse_sitemap_urls(client: httpx.Client, sitemap_url: str, start_url: str,
                       same_domain: bool, _seen_idx: set[str] | None = None) -> list[str]:
    """sitemap.xml bzw. Sitemap-Index rekursiv parsen (wie web-check 'Listed Pages').

    Sammelt <loc>-URLs aus urlset + folgt <sitemap>-Einträgen aus sitemapindex.
    """
    if _seen_idx is None:
        _seen_idx = set()
    if sitemap_url in _seen_idx:
        return []
    _seen_idx.add(sitemap_url)
    found: list[str] = []
    try:
        resp = client.get(sitemap_url)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
    except Exception:
        return []

    # Sitemap-Index: enthaltene Sitemaps rekursiv folgen
    for el in root.iter():
        if el.tag.endswith("sitemap"):
            for loc in el.iter():
                if loc.tag.endswith("loc") and (loc.text or "").strip():
                    found += parse_sitemap_urls(client, loc.text.strip(), start_url, same_domain, _seen_idx)
    # URL-Set: Seiten-URLs sammeln (same-site-Filter)
    for el in root.iter():
        if el.tag.endswith("url"):
            for loc in el.iter():
                if loc.tag.endswith("loc") and (loc.text or "").strip():
                    u, _ = urldefrag(loc.text.strip())
                    pu = urlparse(u)
                    if pu.scheme not in ("http", "https"):
                        continue
                    if same_domain and not same_site(start_url, u):
                        continue
                    if u not in found:
                        found.append(u)
    return found


def discover_site(job_id: int, start_url: str, max_pages: int, max_depth: int,
                  same_domain: bool) -> dict:
    """PHASE 1 – alle Unterseiten herausfinden (nicht speichern, nur indexieren).

    Quellen in Reihenfolge: Start-URL -> robots.txt/Sitemap -> BFS-Link-Crawl.
    Schreibt data/jobs/<id>/discovery.json + discovery.log und gibt die
    indexierte Liste [{nr, url, title, depth, source}] zurueck.
    """
    dest = job_dir(job_id)
    log_lines: list[str] = []
    ordered: list[dict] = []
    seen: set[str] = set()

    def add(url: str, depth: int, source: str, title: str = ""):
        canon, _ = urldefrag(url)
        if canon in seen or len(ordered) >= max_pages:
            return False
        seen.add(canon)
        ordered.append({"nr": len(ordered) + 1, "url": canon,
                        "title": title, "depth": depth, "source": source})
        return True

    headers = {"User-Agent": "SiteBackup/1.1 (+local discovery; polite 0.2s delay)"}
    with httpx.Client(headers=headers, timeout=20, follow_redirects=True, max_redirects=5) as client:
        add(start_url, 0, "start")
        log_lines.append(f"Start: {start_url}")
        # robots.txt + Sitemap (web-check: Crawl Rules + Listed Pages)
        sm_urls, disallows = fetch_robots_sitemaps(client, start_url)
        if disallows:
            log_lines.append(f"robots.txt: {len(disallows)} Disallow-Regeln (reine Info, Backup ignoriert sie)")
        if not sm_urls:
            p = urlparse(start_url)
            sm_urls = [f"{p.scheme}://{p.netloc}/sitemap.xml"]
        sm_found: list[str] = []
        for sm in sm_urls:
            urls = parse_sitemap_urls(client, sm, start_url, same_domain)
            if urls:
                log_lines.append(f"Sitemap {sm}: {len(urls)} URLs")
            for u in urls:
                if len(ordered) >= max_pages:
                    break
                if add(u, 0, "sitemap"):
                    sm_found.append(u)
        # BFS-Link-Crawl fuer alles, was nicht in der Sitemap steht
        queue: deque[tuple[str, int]] = deque([(start_url, 0)])
        visited_fetch: set[str] = set()
        while queue and len(ordered) < max_pages:
            url, depth = queue.popleft()
            canon, _ = urldefrag(url)
            if canon in visited_fetch:
                continue
            visited_fetch.add(canon)
            try:
                resp = client.get(canon)
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype.lower():
                    continue
                html = resp.text
                title = extract_title(html, canon)
                for e in ordered:
                    if e["url"] == canon and not e["title"]:
                        e["title"] = title
                if depth < max_depth:
                    for link in extract_links(html, canon):
                        lcanon, _ = urldefrag(link)
                        if lcanon in seen:
                            continue
                        if same_domain and not same_site(start_url, lcanon):
                            continue
                        if add(lcanon, depth + 1, "link"):
                            queue.append((lcanon, depth + 1))
                        if len(ordered) >= max_pages:
                            break
            except Exception as exc:
                log_lines.append(f"Discovery-Fehler {canon}: {type(exc).__name__}: {exc}")
                continue
        # Titel nachladen fuer Sitemap-URLs, die der BFS-Lauf nicht besucht hat
        for e in ordered:
            if not e["title"] and len(visited_fetch) < max_pages:
                try:
                    resp = client.get(e["url"])
                    if resp.status_code == 200 and "html" in resp.headers.get("content-type", ""):
                        e["title"] = extract_title(resp.text, e["url"])
                        visited_fetch.add(e["url"])
                except Exception:
                    pass
            if not e["title"]:
                e["title"] = extract_title("", e["url"])

    n_sm = sum(1 for e in ordered if e["source"] == "sitemap")
    n_link = sum(1 for e in ordered if e["source"] == "link")
    log_lines.append(f"Discovery fertig: {len(ordered)} Seiten ({n_sm} aus Sitemap, {n_link} via Links, Rest Start)")
    payload = {"start_url": start_url, "count": len(ordered),
               "discovered_at": datetime.utcnow().isoformat(), "pages": ordered}
    (dest / "discovery.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (dest / "discovery.log").write_text("\n".join(log_lines), encoding="utf-8")
    return payload


def load_discovery(job_id: int) -> dict | None:
    f = job_dir(job_id) / "discovery.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
def crawl_job(job_id: int, start_url: str, max_pages: int, max_depth: int, same_domain: bool,
              progress=None) -> dict:
    """PHASE 2 – Unterseite fuer Unterseite als HTML-Datei sichern.

    Nutzt die Discovery-Liste (discovery.json), erstellt sie bei Bedarf neu.
    `progress(done, total, line)` wird nach jeder Seite aufgerufen (Live-Status).
    Mapping-Eintrag pro URL: {nr, title, page_name, file, status, bytes}.
    Schreibt mapping.json + last.log + index.html (Inhaltsverzeichnis).
    """
    dest = job_dir(job_id)
    pages_dir = dest / "pages"
    disc = load_discovery(job_id)
    if not disc or disc.get("start_url") != start_url or not disc.get("pages"):
        disc = discover_site(job_id, start_url, max_pages, max_depth, same_domain)
    targets = [p for p in disc["pages"]][:max_pages]
    total = len(targets)
    mapping: dict[str, dict] = {}
    log_lines: list[str] = []
    ok = failed = total_bytes = 0

    headers = {"User-Agent": "SiteBackup/1.1 (+local backup; polite 0.2s delay)"}
    with httpx.Client(headers=headers, timeout=20, follow_redirects=True, max_redirects=5) as client:
        for i, entry in enumerate(targets, start=1):
            canon = entry["url"]
            title = entry.get("title") or extract_title("", canon)
            try:
                resp = client.get(canon)
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype.lower() and not resp.text.lstrip().lower().startswith(("<!doctype", "<html")):
                    line = f"[{i}/{total}] SKIP (kein HTML, {ctype}): {title} <{canon}>"
                    log_lines.append(line)
                    mapping[canon] = {"nr": i, "title": title, "page_name": title,
                                      "file": None, "status": resp.status_code,
                                      "note": f"kein HTML ({ctype})"}
                    failed += 1
                    if progress:
                        progress(i, total, line)
                    continue
                html = resp.text
                if len(html.encode("utf-8")) > 8 * 1024 * 1024:
                    line = f"[{i}/{total}] SKIP (>8MB): {title} <{canon}>"
                    log_lines.append(line)
                    mapping[canon] = {"nr": i, "title": title, "page_name": title,
                                      "file": None, "status": resp.status_code, "note": "zu gross"}
                    failed += 1
                    if progress:
                        progress(i, total, line)
                    continue
                title = extract_title(html, canon) or title
                fname = url_to_filename(canon)
                fpath = pages_dir / fname
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(html, encoding="utf-8")
                size = fpath.stat().st_size
                total_bytes += size
                ok += 1
                line = f"[{i}/{total}] OK [{resp.status_code}] {title} <{canon}> -> {fname} ({size} B)"
                log_lines.append(line)
                mapping[canon] = {"nr": i, "title": title, "page_name": title,
                                  "file": fname, "status": resp.status_code, "bytes": size}
            except Exception as exc:  # komplette Kette ins Log, nicht nur letzte Zeile
                tb = traceback.format_exc(limit=3).strip().splitlines()
                short = f"{type(exc).__name__}: {exc}"
                line = f"[{i}/{total}] FEHLER {title} <{canon}> -> {short}"
                log_lines.append(line)
                mapping[canon] = {"nr": i, "title": title, "page_name": title,
                                  "file": None, "status": 0, "note": short, "trace": tb[-3:]}
                failed += 1
            if progress:
                progress(i, total, log_lines[-1])

    (dest / "mapping.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    (dest / "last.log").write_text("\n".join(log_lines), encoding="utf-8")
    write_index_html(job_id, disc.get("start_url", start_url), mapping)
    return {"ok": ok, "failed": failed, "bytes": total_bytes, "pages": total,
            "log": "\n".join(log_lines)}


def write_index_html(job_id: int, start_url: str, mapping: dict[str, dict]) -> Path:
    """Inhaltsverzeichnis: Nr, Seitenname/Titel, URL, Datei, Groesse."""
    rows = sorted(mapping.items(), key=lambda kv: (kv[1].get("nr") or 999999))
    trs = []
    for url, m in rows:
        nr = m.get("nr", "?")
        title = htmlmod.escape(str(m.get("title") or url))
        file = m.get("file")
        if file:
            dl = f'<a href="pages/{htmlmod.escape(file)}">{htmlmod.escape(file)}</a>'
        else:
            dl = f'<span class="miss">– ({htmlmod.escape(str(m.get("note", "Fehler"))[:80])})</span>'
        size = f'{m.get("bytes", 0) / 1024:.1f} KB' if m.get("bytes") else "–"
        trs.append(f"<tr><td>{nr}</td><td><b>{title}</b></td>"
                   f'<td><a href="{htmlmod.escape(url)}">{htmlmod.escape(url)}</a></td>'
                   f"<td>{dl}</td><td>{size}</td></tr>")
    doc = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8"><title>SiteBackup-Index – {htmlmod.escape(start_url)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#111}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccc;padding:6px 8px;text-align:left;font-size:14px}}
th{{background:#f0f0f0}}.miss{{color:#a00}}</style></head><body>
<h1>Backup-Index: {htmlmod.escape(start_url)}</h1>
<p>{len(rows)} Unterseiten, gesichert am {htmlmod.escape(datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC"))}</p>
<table><tr><th>Nr</th><th>Seitenname</th><th>URL</th><th>HTML-Datei</th><th>Groesse</th></tr>
{''.join(trs)}</table></body></html>"""
    idx = job_dir(job_id) / "index.html"
    idx.write_text(doc, encoding="utf-8")
    return idx


def run_job_sync(job_id: int) -> int:
    """Einen Job jetzt ausfuehren (synchron). Gibt run_id zurueck.

    Schreibt den Fortschritt nach jeder Unterseite in die DB (Live-Polling),
    erstellt am Ende ZIP + Index.
    """
    with Session(engine) as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job nicht gefunden")
        run = Run(job_id=job.id, status="running")
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id
        start_url, max_pages, max_depth, same_domain = job.start_url, job.max_pages, job.max_depth, job.same_domain
    error, log, ok, failed, total = "", "", 0, 0, 0
    status = "ok"
    try:
        acc: list[str] = []

        def progress(done: int, total_pages: int, line: str):
            acc.append(line)
            with Session(engine) as db:
                r = db.get(Run, run_id)
                r.log = "\n".join(acc)[-20000:]
                r.pages_ok = sum(1 for ln in acc if ln.split("] ", 1)[-1].startswith("OK"))
                r.pages_failed = sum(1 for ln in acc if "SKIP" in ln or "FEHLER" in ln)
                db.commit()

        res = crawl_job(job_id, start_url, max_pages, max_depth, same_domain, progress=progress)
        ok, failed, total, log = res["ok"], res["failed"], res["bytes"], res["log"]
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"
        log = error
    with Session(engine) as db:
        run = db.get(Run, run_id)
        run.status = status
        run.finished_at = datetime.utcnow()
        run.pages_ok, run.pages_failed, run.total_bytes = ok, failed, total
        run.error, run.log = error, log[-20000:]
        db.commit()
    # ZIP aktualisieren
    try:
        make_zip(job_id)
    except Exception as exc:
        with Session(engine) as db:
            run = db.get(Run, run_id)
            run.error += f"\nZIP-Fehler: {exc}"
            db.commit()
    return run_id


def make_zip(job_id: int) -> Path:
    dest = job_dir(job_id)
    zpath = dest / f"job-{job_id}-backup.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted((dest / "pages").rglob("*.html")):
            zf.write(f, f.relative_to(dest))
        for extra in ("mapping.json", "discovery.json", "index.html"):
            if (dest / extra).exists():
                zf.write(dest / extra, extra)
    return zpath


# ---------------------------------------------------------------- Scheduler

try:
    scheduler = BackgroundScheduler(timezone="local")
except Exception:
    scheduler = BackgroundScheduler()  # Fallback ohne tzdata (Container: tzdata installiert)
_run_lock = threading.Semaphore(2)  # max. 2 parallele Backups


def _trigger_for_job(j: Job):
    hh, mm = (j.schedule_time or "03:00").split(":")
    h, m = int(hh) % 24, int(mm) % 60
    if j.schedule == "hourly":
        return CronTrigger(minute=m)
    if j.schedule == "daily":
        return CronTrigger(hour=h, minute=m)
    if j.schedule == "weekly":
        return CronTrigger(day_of_week=j.schedule_weekday or "mon", hour=h, minute=m)
    if j.schedule == "monthly":
        return CronTrigger(day=max(1, min(28, j.schedule_day or 1)), hour=h, minute=m)
    if j.schedule == "cron":
        from apscheduler.triggers.cron import CronTrigger as CT
        parts = (j.cron_expr or "").split()
        if len(parts) == 5:
            mi, h_, dom, mon, dow = parts
            return CT(minute=mi, hour=h_, day=dom, month=mon, day_of_week=dow)
        raise ValueError(f"Ungueltiger Cron-Ausdruck: {j.cron_expr!r} (erwartet 5 Felder 'm h dom mon dow')")
    return None


def _scheduled_run(job_id: int):
    if not _run_lock.acquire(blocking=False):
        with Session(engine) as db:
            db.add(Run(job_id=job_id, status="error", finished_at=datetime.utcnow(),
                       error="Uebersprungen: anderes Backup laeuft noch (max. 2 parallel)."))
            db.commit()
        return
    try:
        run_job_sync(job_id)
    finally:
        _run_lock.release()


def refresh_schedule():
    scheduler.remove_all_jobs()
    with Session(engine) as db:
        jobs = db.scalars(select(Job).where(Job.enabled == True)).all()  # noqa: E712
        for j in jobs:
            try:
                trig = _trigger_for_job(j)
            except Exception as exc:
                print(f"[scheduler] Job {j.id} ({j.name}): {exc}")
                continue
            if trig is None:
                continue
            scheduler.add_job(_scheduled_run, trig, args=[j.id], id=f"job-{j.id}",
                              max_instances=1, coalesce=True, misfire_grace_time=3600)


def next_run_iso(j: Job) -> str | None:
    try:
        job = scheduler.get_job(f"job-{j.id}")
        if job and job.next_run_time:
            return job.next_run_time.isoformat()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- API

@app.get("/api/health")
def health():
    return {"status": "ok", "app": "SiteBackup", "version": app.version}


@app.get("/api/jobs")
def list_jobs():
    with Session(engine) as db:
        return [serialize_job(j) for j in db.scalars(select(Job).order_by(Job.id)).all()]


@app.post("/api/jobs")
def create_job(payload: JobIn):
    try:
        data = payload.normalized()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with Session(engine) as db:
        job = Job(**data)
        db.add(job)
        db.commit()
        db.refresh(job)
        jid = job.id
    refresh_schedule()
    with Session(engine) as db:
        return serialize_job(db.get(Job, jid))


@app.put("/api/jobs/{job_id}")
def update_job(job_id: int, payload: JobIn):
    try:
        data = payload.normalized()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with Session(engine) as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job nicht gefunden")
        for k, v in data.items():
            setattr(job, k, v)
        job.updated_at = datetime.utcnow()
        db.commit()
    refresh_schedule()
    with Session(engine) as db:
        return serialize_job(db.get(Job, job_id))


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int):
    with Session(engine) as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job nicht gefunden")
        for r in db.scalars(select(Run).where(Run.job_id == job_id)).all():
            db.delete(r)
        db.delete(job)
        db.commit()
    refresh_schedule()
    return {"deleted": job_id}


@app.post("/api/jobs/{job_id}/discover")
def trigger_discover(job_id: int):
    """PHASE 1: alle Unterseiten herausfinden (robots.txt + Sitemap + Links).

    Gibt die indexierte Liste zurueck: [{nr, url, title, depth, source}].
    """
    with Session(engine) as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job nicht gefunden")
        start_url, max_pages, max_depth, same_domain = job.start_url, job.max_pages, job.max_depth, job.same_domain
    try:
        payload = discover_site(job_id, start_url, max_pages, max_depth, same_domain)
    except Exception as exc:
        raise HTTPException(502, f"Discovery fehlgeschlagen: {type(exc).__name__}: {exc}\n"
                                 f"{traceback.format_exc(limit=5)}")
    return payload


@app.get("/api/jobs/{job_id}/discover")
def get_discovery(job_id: int):
    payload = load_discovery(job_id)
    if not payload:
        return {"pages": [], "count": 0, "note": "Noch keine Discovery gelaufen – Button 'Seiten entdecken' klicken"}
    return payload


@app.post("/api/jobs/{job_id}/run")
def trigger_run(job_id: int):
    with Session(engine) as db:
        if not db.get(Job, job_id):
            raise HTTPException(404, "Job nicht gefunden")
    t = threading.Thread(target=_scheduled_run, args=(job_id,), daemon=True)
    t.start()
    return {"started": True, "job_id": job_id}


@app.get("/api/runs")
def list_runs(job_id: int | None = None, limit: int = 50):
    with Session(engine) as db:
        q = select(Run).order_by(Run.id.desc()).limit(max(1, min(200, limit)))
        if job_id:
            q = select(Run).where(Run.job_id == job_id).order_by(Run.id.desc()).limit(max(1, min(200, limit)))
        return [serialize_run(r) for r in db.scalars(q).all()]


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    with Session(engine) as db:
        r = db.get(Run, run_id)
        if not r:
            raise HTTPException(404, "Run nicht gefunden")
        d = serialize_run(r)
        d["log"] = r.log or ""
        return JSONResponse(d)


@app.get("/api/jobs/{job_id}/pages")
def list_pages(job_id: int):
    """Index: jede Unterseite mit Nr, Seitenname/Titel, URL und HTML-Datei."""
    dest = job_dir(job_id)
    mp = dest / "mapping.json"
    if not mp.exists():
        disc = load_discovery(job_id)
        if disc and disc.get("pages"):
            return {"pages": [], "discovered": disc["pages"],
                    "note": "Discovery vorhanden, noch kein Backup gelaufen"}
        return {"pages": [], "note": "Noch keine Discovery / noch kein Backup gelaufen"}
    mapping = json.loads(mp.read_text(encoding="utf-8"))
    pages = sorted(({"url": u, **v} for u, v in mapping.items()),
                   key=lambda p: (p.get("nr") or 999999))
    return {"pages": pages}


@app.get("/api/jobs/{job_id}/index")
def download_index(job_id: int):
    """Inhaltsverzeichnis (Nr, Seitenname, URL, Datei) als HTML."""
    idx = job_dir(job_id) / "index.html"
    if not idx.exists():
        raise HTTPException(404, "Noch kein Index vorhanden – erst Backup starten")
    return FileResponse(idx, media_type="text/html", filename=f"sitebackup-job-{job_id}-index.html")


@app.get("/api/jobs/{job_id}/download")
def download_zip(job_id: int):
    zpath = job_dir(job_id) / f"job-{job_id}-backup.zip"
    if not zpath.exists():
        try:
            make_zip(job_id)
        except Exception as exc:
            raise HTTPException(404, f"Noch kein Backup vorhanden ({exc})")
        if not zpath.exists():
            raise HTTPException(404, "Noch kein Backup vorhanden")
    return FileResponse(zpath, media_type="application/zip", filename=f"sitebackup-job-{job_id}.zip")


@app.get("/api/jobs/{job_id}/page")
def download_page(job_id: int, file: str):
    # Path-Traversal-Schutz: nur .html unter pages/
    if ".." in file or file.startswith("/") or not file.endswith((".html", ".htm")):
        raise HTTPException(400, "Ungueltiger Dateiname")
    fpath = (job_dir(job_id) / "pages" / file).resolve()
    root = (job_dir(job_id) / "pages").resolve()
    if not str(fpath).startswith(str(root)) or not fpath.is_file():
        raise HTTPException(404, "Seite nicht gefunden")
    return FileResponse(fpath, media_type="text/html")


# Statische Web UI (muss NACH den /api-Routen registriert werden)
STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


@app.on_event("startup")
def _startup():
    refresh_schedule()
    if not scheduler.running:
        try:
            scheduler.start()
        except Exception:
            pass  # bereits gestartet (z.B. Reload)
