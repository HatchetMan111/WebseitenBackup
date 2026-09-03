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
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
import traceback
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

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

# Fetch-Limits (DoS-Schutz: gestreamt, nichts unbegrenzt in RAM laden)
REQ_TIMEOUT = float(os.getenv("SITEBACKUP_TIMEOUT", "20"))
MAX_HTML_BYTES = int(os.getenv("SITEBACKUP_MAX_HTML_MB", "8")) * 1024 * 1024
MAX_SITEMAP_BYTES = 5 * 1024 * 1024
MAX_SITEMAP_FILES = 25
MAX_SITEMAP_URLS_PER_FILE = 20000
# Asset-Limits (Offline-Kopie): pro Datei, gesamt pro Job, Anzahl
MAX_ASSET_BYTES = int(os.getenv("SITEBACKUP_MAX_ASSET_MB", "10")) * 1024 * 1024
MAX_ASSETS_BYTES = int(os.getenv("SITEBACKUP_MAX_ASSETS_MB", "500")) * 1024 * 1024
MAX_ASSETS = int(os.getenv("SITEBACKUP_MAX_ASSETS", "2000"))
MAX_INDEX_CHARS = 200000  # Volltext-Cap pro Seite
# SSRF-Schutz: nur oeffentliche IPs (Tests/Dev: SITEBACKUP_ALLOW_PRIVATE=1)
ALLOW_PRIVATE = os.getenv("SITEBACKUP_ALLOW_PRIVATE", "") == "1"

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


class PageIndex(Base):
    """Volltext-Index des Archivs (wird pro Backup neu aufgebaut)."""
    __tablename__ = "page_index"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, index=True)
    url: Mapped[str] = mapped_column(String(2000))
    title: Mapped[str] = mapped_column(String(300), default="")
    text: Mapped[str] = mapped_column(Text, default="")


Base.metadata.create_all(engine)

# FTS5-Spiegel (eigene Tabelle statt Trigger: simpler + robust)
try:
    with engine.begin() as _conn:
        from sqlalchemy import text as _stext
        _conn.execute(_stext("CREATE VIRTUAL TABLE IF NOT EXISTS page_fts "
                             "USING fts5(job_id UNINDEXED, url, title, text)"))
    FTS_AVAILABLE = True
except Exception as _e:
    print(f"[fts] FTS5 nicht verfuegbar, Suche deaktiviert: {_e}")
    FTS_AVAILABLE = False


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Nur zur Laufzeit aufgeloest (Scheduler weiter unten definiert)
    refresh_schedule()
    if not scheduler.running:
        try:
            scheduler.start()
        except Exception:
            pass  # bereits gestartet (z.B. Reload)
    yield


app = FastAPI(title="SiteBackup", version="1.3.0", lifespan=_lifespan)

# ---------------------------------------------------------------- Optionales Passwort (LAN-Schutz)
# Setzen via Env SITEBACKUP_PASSWORD oder Datei data/app.password
# (Installer fragt danach). Ohne Passwort: offen im LAN wie bisher.
PASSWORD = os.getenv("SITEBACKUP_PASSWORD", "").strip()
_PW_FILE = DATA_DIR / "app.password"
if not PASSWORD and _PW_FILE.exists():
    PASSWORD = _PW_FILE.read_text().strip()

LOGIN_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SiteBackup – Anmeldung</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f172a;font-family:Inter,system-ui,sans-serif;color:#e2e8f0}
.card{background:#1e293b;border:1px solid #334155;border-radius:14px;padding:28px;width:min(92vw,340px);text-align:center}
h1{font-size:18px;margin:0 0 4px}p{color:#94a3b8;font-size:13px;margin:0 0 18px}
input{width:100%;padding:10px 12px;border-radius:9px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;margin-bottom:10px}
button{width:100%;padding:10px;border:0;border-radius:9px;background:#38bdf8;color:#0f172a;font-weight:700;cursor:pointer}
.err{color:#f87171;font-size:13px;min-height:18px;margin-top:8px}
</style></head><body>
<div class="card"><h1>SiteBackup</h1><p>Bitte mit Passwort anmelden</p>
<input type="password" id="pw" placeholder="Passwort" autofocus>
<button onclick="go()">Anmelden</button><div class="err" id="err"></div></div>
<script>
const pw=document.getElementById('pw'),err=document.getElementById('err');
function go(){fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw.value})})
.then(r=>{if(r.ok)location='/';else{err.textContent='Falsches Passwort';pw.value='';pw.focus();}}).catch(()=>err.textContent='Server nicht erreichbar');}
pw.addEventListener('keydown',e=>{if(e.key==='Enter')go();});
</script></body></html>"""

if PASSWORD:
    _SECRET_FILE = DATA_DIR / "session.secret"
    if not _SECRET_FILE.exists():
        _SECRET_FILE.write_text(secrets.token_hex(32))
    _SECRET = _SECRET_FILE.read_text().strip()

    def _make_token() -> str:
        exp = int(time.time()) + 60 * 60 * 24 * 30
        sig = hmac.new(_SECRET.encode(), f"sb:{exp}:{PASSWORD}".encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def _valid_session(token: str | None) -> bool:
        try:
            exp, sig = (token or "").split(".", 1)
            if int(exp) < time.time():
                return False
            expected = hmac.new(_SECRET.encode(), f"sb:{exp}:{PASSWORD}".encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, expected)
        except Exception:
            return False

    @app.middleware("http")
    async def auth_middleware(request, call_next):
        path = request.url.path
        if path in ("/login", "/api/login", "/api/health"):
            return await call_next(request)
        if _valid_session(request.cookies.get("sb_session")):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Nicht angemeldet"}, status_code=401)
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=302)

    class LoginIn(BaseModel):
        password: str

    @app.get("/login")
    def login_page():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(LOGIN_HTML)

    @app.post("/api/login")
    def login(payload: LoginIn):
        if not hmac.compare_digest(payload.password.encode(), PASSWORD.encode()):
            time.sleep(0.5)
            raise HTTPException(401, "Falsches Passwort")
        response = JSONResponse({"ok": True})
        response.set_cookie("sb_session", _make_token(), httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30, path="/")
        return response

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
        m = re.match(r"^(\d{1,2}):(\d{2})$", (self.schedule_time or "").strip())
        if not m:
            raise ValueError("schedule_time muss HH:MM sein (z.B. 03:00)")
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            raise ValueError("schedule_time ausserhalb 00:00–23:59")
        sched_time = f"{hh:02d}:{mm:02d}"
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
            "schedule_time": sched_time,
            "schedule_weekday": wd,
            "schedule_day": self.schedule_day,
            "cron_expr": self.cron_expr.strip(),
            "enabled": self.enabled,
        }


def serialize_job(j: Job) -> dict:
    with Session(engine) as db:
        last = db.scalar(select(Run).where(Run.job_id == j.id).order_by(Run.id.desc()))
        last_run = None
        if last is not None:
            last_run = {"status": last.status,
                        "finished_at": last.finished_at.isoformat() if last.finished_at else None,
                        "pages_ok": last.pages_ok, "pages_failed": last.pages_failed,
                        "total_bytes": last.total_bytes}
    disc = load_discovery(j.id)
    return {
        "id": j.id, "name": j.name, "start_url": j.start_url,
        "max_pages": j.max_pages, "max_depth": j.max_depth,
        "same_domain": j.same_domain, "schedule": j.schedule,
        "schedule_time": j.schedule_time, "schedule_weekday": j.schedule_weekday,
        "schedule_day": j.schedule_day, "cron_expr": j.cron_expr,
        "enabled": j.enabled,
        "next_run": next_run_iso(j),
        "discovered": disc["count"] if disc else None,
        "last_run": last_run,
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

def canon_url(u: str) -> str:
    """/a und /a/ sowie Gross-/Kleinschreibung im Host vereinheitlichen,
    damit dieselbe Seite nicht doppelt gesichert wird."""
    u, _ = urldefrag(u.strip())
    p = urlparse(u)
    host = (p.hostname or "").lower()
    port = p.port
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), host, path, "", p.query, ""))


def validate_url_host(url: str) -> str | None:
    """SSRF-Schutz: http(s), keine Zugangsdaten, Host muss oeffentlich aufloesbar sein.

    Gibt None zurueck wenn OK, sonst Fehlertext. (Restrisiko DNS-Rebinding
    zwischen Check und Request ist fuer ein LAN-Tool akzeptiert.)
    """
    try:
        p = urlparse(url)
    except Exception:
        return "ungueltige URL"
    if p.scheme not in ("http", "https"):
        return "nur http(s)-URLs erlaubt"
    if p.username or p.password:
        return "URLs mit Zugangsdaten blockiert"
    host = p.hostname or ""
    if not host or len(host) > 253:
        return "ungueltiger Hostname"
    if ALLOW_PRIVATE:
        return None
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return f"Host nicht aufloesbar: {host}"
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return f"nicht-oeffentliche IP blockiert ({host})"
        except ValueError:
            return f"ungueltige IP fuer {host}"
    return None


def fetch_raw(client: httpx.Client, url: str, max_bytes: int) -> dict:
    """GET mit SSRF-Check, validierten Redirects und Streaming-Limit.
    Gibt ROHE BYTES zurueck (binärsicher). keys: url, status, content_type,
    raw, truncated, error.
    """
    current = url
    for _ in range(6):  # Start-URL + max. 5 Redirects
        blocked = validate_url_host(current)
        if blocked:
            return {"url": current, "status": 0, "content_type": "",
                    "raw": b"", "truncated": False, "error": blocked}
        try:
            with client.stream("GET", current, follow_redirects=False) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    continue
                ctype = resp.headers.get("content-type", "")
                chunks: list[bytes] = []
                size = 0
                truncated = False
                for chunk in resp.iter_bytes(65536):
                    size += len(chunk)
                    if size > max_bytes:
                        truncated = True
                        break
                    chunks.append(chunk)
                return {"url": str(resp.url), "status": resp.status_code,
                        "content_type": ctype, "raw": b"".join(chunks),
                        "truncated": truncated, "error": ""}
        except Exception as exc:
            return {"url": current, "status": 0, "content_type": "",
                    "raw": b"", "truncated": False,
                    "error": f"{type(exc).__name__}: {exc}"}
    return {"url": current, "status": 0, "content_type": "",
            "raw": b"", "truncated": False, "error": "Zu viele Redirects"}


def fetch_page(client: httpx.Client, url: str, max_bytes: int = MAX_HTML_BYTES) -> dict:
    """Eine Seite holen mit SSRF-Check, manuellen (validierten) Redirects und
    Streaming-Limit (kein unbegrenztes Laden in RAM).

    keys: url, status, content_type, text, truncated, error
    """
    r = fetch_raw(client, url, max_bytes)
    try:
        enc = "utf-8"
        text = r["raw"].decode(enc, errors="replace")
    except Exception:
        text = ""
    return {"url": r["url"], "status": r["status"], "content_type": r["content_type"],
            "text": text, "truncated": r["truncated"], "error": r["error"]}


def require_job(job_id: int) -> Job:
    """Job aus DB laden (404 statt leere Verzeichnisse fuer beliebige IDs anzulegen)."""
    with Session(engine) as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job nicht gefunden")
        db.expunge(job)
        return job


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


def slugify(name: str) -> str:
    """Job-Namen dateisystemtauglich machen: 'Meine Firmen-Webseite!' -> 'meine_firmen_webseite'."""
    repl = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"}
    for a, b in repl.items():
        name = name.replace(a, b)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    name = re.sub(r"_+", "_", name)[:60].strip("_").lower()
    return name or "webseite"


def backup_download_name(job: Job, suffix: str) -> str:
    """Sprechender Download-Name: '<job>_job-<id>_<datum>.<suffix>'.

    Datum = letzter erfolgreicher Lauf, sonst heute. Beispiel:
    'meine_firmen_webseite_job-3_2026-09-04.zip'
    """
    day = datetime.utcnow().date().isoformat()
    with Session(engine) as db:
        last = db.scalar(select(Run).where(Run.job_id == job.id, Run.status == "ok")
                         .order_by(Run.id.desc()))
        if last is not None and last.finished_at:
            day = last.finished_at.date().isoformat()
    return f"{slugify(job.name or 'webseite')}_job-{job.id}_{day}.{suffix}"


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


# ---------------------------------------------------------------- Offline-Kopie (Assets)

ASSET_ATTRS = {
    "link": ("href",),  # nur Stylesheets (s. Filter unten)
    "script": ("src",), "img": ("src", "srcset"),
    "source": ("src", "srcset"), "video": ("src", "poster"),
    "audio": ("src",), "embed": ("src",), "track": ("src",),
}
_URL_RE = re.compile(r"url\(\s*['\"]?([^'\"\s)]+)['\"]?\s*\)", re.IGNORECASE)


def asset_local_path(url: str) -> str | None:
    """Zielpfad unter pages/: assets/<host>/<pfad>. Query -> Hash-Suffix."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    host = re.sub(r"[^a-z0-9.-]", "_", p.hostname.lower())[:80] or "host"
    path = p.path.strip("/") or "index"
    suffix = ""
    if p.query:
        suffix = "_" + hashlib.sha256(p.query.encode()).hexdigest()[:8]
    safe = re.sub(r"[^A-Za-z0-9._/-]", "_", path).strip("/_") or "index"
    stem = safe + suffix
    parts = [re.sub(r"_+", "_", x)[:60] or "_" for x in stem.split("/") if x not in ("", ".", "..")]
    rel = "assets/" + host + "/" + "/".join(parts[-5:])
    return rel[:240]


def css_refs(css: str, base: str) -> list[str]:
    """url(...)-Referenzen aus CSS-Text (fuer Fonts/Hintergruende, 1 Ebene)."""
    out, seen = [], set()
    for m in _URL_RE.finditer(css):
        ref = m.group(1).strip()
        if not ref or ref.startswith(("#", "data:")):
            continue
        abs_url, _ = urldefrag(urljoin(base, ref))
        if urlparse(abs_url).scheme not in ("http", "https"):
            continue
        if abs_url not in seen:
            seen.add(abs_url)
            out.append(abs_url)
    return out


def split_srcset(value: str) -> list[tuple[str, str]]:
    """'a.jpg 1x, b.jpg 2x' -> [(url, deskriptor), ...]."""
    out = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        out.append((bits[0], (" " + " ".join(bits[1:])) if len(bits) > 1 else ""))
    return out


def localize_page(client: httpx.Client, html: str, page_url: str, page_file: str,
                  pages_dir: Path, budget: dict) -> tuple[str, int]:
    """Assets einer Seite laden (binaersicher) + Referenzen auf lokale Pfade
    umschreiben (wget -k-Prinzip). Interne <a>-Links werden spaeter in Pass 2
    umgeschrieben (Mapping erst dann komplett).
    Gibt (html_neu, anzahl_assets) zurueck.
    budget = {"bytes": Rest, "count": Rest} wird pro Job geteilt und updated.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return html, 0
    asset_map: dict[str, str] = {}
    page_dir = str(Path(page_file).parent).replace("\\", "/")

    def rel_from_page(rel: str) -> str:
        """Pfad pages/... relativ zur Seiten-Datei (kann in Unterordnern liegen)."""
        if page_dir in (".", ""):
            return rel
        try:
            return str(Path(*([".."] * len(Path(page_dir).parts))) / rel).replace("\\", "/")
        except Exception:
            return rel

    def store(url: str, raw: bytes) -> str | None:
        rel = asset_local_path(url)
        if not rel:
            return None
        target = pages_dir / rel
        if not target.is_file():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            except Exception:
                return None
        asset_map[url] = rel
        return rel

    def fetch_asset(url: str, _css_depth: int = 0) -> str | None:
        if url in asset_map:
            return asset_map[url]
        if budget["count"] <= 0 or budget["bytes"] <= 0:
            return None
        cap = min(MAX_ASSET_BYTES, budget["bytes"])
        r = fetch_raw(client, url, max_bytes=cap)
        if r["error"] or r["status"] != 200 or r["truncated"] or not r["raw"]:
            return None
        budget["bytes"] -= len(r["raw"])
        budget["count"] -= 1
        rel = store(url, r["raw"])
        if rel is None:
            return None
        # CSS eine Ebene tiefer folgen (Fonts, Hintergrundbilder) + Refs umschreiben
        if _css_depth < 1 and ("css" in r["content_type"].lower() or url.endswith(".css")):
            try:
                css_text = r["raw"][:MAX_SITEMAP_BYTES].decode("utf-8", errors="replace")
                css_target = pages_dir / rel
                css_dir = css_target.parent
                subs: dict[str, str] = {}
                for sub in css_refs(css_text, url):
                    sub_rel = fetch_asset(sub, _css_depth + 1)
                    if sub_rel:
                        subs[sub] = sub_rel
                if subs:
                    def _css_sub(m: "re.Match") -> str:
                        au, _ = urldefrag(urljoin(url, m.group(1).strip()))
                        sub_rel = subs.get(au)
                        if not sub_rel:
                            return m.group(0)
                        try:
                            relpath = str(Path(sub_rel).relative_to(css_dir.relative_to(pages_dir)))
                        except Exception:
                            try:
                                relpath = str(os.path.relpath(str(pages_dir / sub_rel),
                                                             str(css_dir))).replace("\\", "/")
                            except Exception:
                                return m.group(0)
                        return f"url({relpath})"
                    css_target.write_text(_URL_RE.sub(_css_sub, css_text), encoding="utf-8")
            except Exception:
                pass
        return rel

    def abs_url(ref: str) -> str | None:
        ref = (ref or "").strip()
        if not ref or ref.startswith(("#", "data:", "blob:")):
            return None
        au, _ = urldefrag(urljoin(page_url, ref))
        return au if urlparse(au).scheme in ("http", "https") else None

    def local_ref(absu: str | None) -> str | None:
        if not absu:
            return None
        rel = asset_map.get(absu)
        return rel_from_page(rel) if rel else None

    # 1) link/script/img/... einsammeln + laden
    jobs: list[str] = []
    for tag_name, attrs in ASSET_ATTRS.items():
        for tag in soup.find_all(tag_name):
            if tag_name == "link":
                rel_attr = " ".join(tag.get("rel", [])).lower()
                href = tag.get("href", "")
                if "stylesheet" not in rel_attr and not href.lower().endswith(".css"):
                    continue
            for attr in attrs:
                val = tag.get(attr)
                if not val:
                    continue
                if attr == "srcset":
                    for u, _ in split_srcset(val):
                        au = abs_url(u)
                        if au:
                            jobs.append(au)
                else:
                    au = abs_url(val)
                    if au:
                        jobs.append(au)
    for style_tag in soup.find_all("style"):
        for u in css_refs(style_tag.get_text() or "", page_url):
            jobs.append(u)
    for tag in soup.find_all(style=True):
        for u in css_refs(tag.get("style") or "", page_url):
            jobs.append(u)
    for au in dict.fromkeys(jobs):  # laden, Reihenfolge stabil, Duplikate raus
        fetch_asset(au)

    # 2) Referenzen umschreiben
    for tag_name, attrs in ASSET_ATTRS.items():
        for tag in soup.find_all(tag_name):
            if tag_name == "link":
                rel_attr = " ".join(tag.get("rel", [])).lower()
                href = tag.get("href", "")
                if "stylesheet" not in rel_attr and not href.lower().endswith(".css"):
                    continue
            for attr in attrs:
                val = tag.get(attr)
                if not val:
                    continue
                if attr == "srcset":
                    tag[attr] = ", ".join(
                        ((local_ref(abs_url(u)) or u) + desc)
                        for u, desc in split_srcset(val))
                else:
                    lr = local_ref(abs_url(val))
                    if lr:
                        tag[attr] = lr
    for tag in soup.find_all(style=True):
        tag["style"] = _URL_RE.sub(
            lambda m: f"url({local_ref(abs_url(m.group(1)))})"
            if local_ref(abs_url(m.group(1))) else m.group(0),
            tag.get("style") or "")
    for style_tag in soup.find_all("style"):
        if style_tag.string:
            style_tag.string.replace_with(_URL_RE.sub(
                lambda m: f"url({local_ref(abs_url(m.group(1)))})"
                if local_ref(abs_url(m.group(1))) else m.group(0),
                style_tag.string))
    return str(soup), len(asset_map)


def rewrite_internal_links(html: str, page_url: str, page_file: str,
                            file_map: dict[str, str]) -> str:
    """Interne <a>-Links auf lokale HTML-Dateien umschreiben (Pass 2)."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return html
    page_dir = Path(page_file).parent
    prefix = [".."] * len(page_dir.parts) if str(page_dir) != "." else []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        target = canon_url(urljoin(page_url, href))
        local = file_map.get(target)
        if local:
            try:
                a["href"] = str(Path(*prefix, local)).replace("\\", "/") if prefix \
                    else local
            except Exception:
                pass
    return str(soup)


def page_visible_text(html: str) -> str:
    """Sichtbarer Text einer Seite (fuer Volltextsuche)."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for dead in soup(["script", "style", "noscript", "template"]):
            dead.decompose()
        text = soup.get_text(" ", strip=True)
    except Exception:
        return ""
    return re.sub(r"\s+", " ", text)[:MAX_INDEX_CHARS]


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
    page = fetch_page(client, robots_url, max_bytes=256 * 1024)
    if page["status"] != 200 or page["error"]:
        return [], []
    try:
        for line in page["text"].splitlines():
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

    Sammelt <loc>-URLs aus urlset + folgt <sitemap>-Eintraegen aus sitemapindex.
    Schutz: max. MAX_SITEMAP_FILES Dateien, Groessen-Cap, keine XML-Entities
    (Billion-Laughs), Tag-Vergleich case-insensitiv.
    """
    if _seen_idx is None:
        _seen_idx = set()
    if sitemap_url in _seen_idx or len(_seen_idx) >= MAX_SITEMAP_FILES:
        return []
    _seen_idx.add(sitemap_url)
    found: list[str] = []
    page = fetch_page(client, sitemap_url, max_bytes=MAX_SITEMAP_BYTES)
    if page["status"] != 200 or page["error"] or page["truncated"]:
        return []
    if "<!ENTITY" in page["text"][:2000].upper():
        return []  # Entity-Expansion (Billion Laughs) ablehnen
    try:
        root = ET.fromstring(page["text"].encode("utf-8", errors="replace"))
    except Exception:
        return []

    def tag(el) -> str:
        t = el.tag if isinstance(el.tag, str) else ""
        return t.lower().rsplit("}", 1)[-1]

    # Sitemap-Index: enthaltene Sitemaps rekursiv folgen
    for el in root.iter():
        if tag(el) == "sitemap":
            for loc in el.iter():
                if tag(loc) == "loc" and (loc.text or "").strip():
                    found += parse_sitemap_urls(client, loc.text.strip(), start_url, same_domain, _seen_idx)
    # URL-Set: Seiten-URLs sammeln (kanonisiert + same-site-Filter)
    for el in root.iter():
        if tag(el) == "url":
            for loc in el.iter():
                if tag(loc) == "loc" and (loc.text or "").strip():
                    u = canon_url(loc.text.strip())
                    pu = urlparse(u)
                    if pu.scheme not in ("http", "https"):
                        continue
                    if same_domain and not same_site(start_url, u):
                        continue
                    if u not in found:
                        found.append(u)
                    if len(found) >= MAX_SITEMAP_URLS_PER_FILE:
                        return found
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
        canon = canon_url(url)
        if canon in seen or len(ordered) >= max_pages:
            return False
        seen.add(canon)
        ordered.append({"nr": len(ordered) + 1, "url": canon,
                        "title": title, "depth": depth, "source": source})
        return True

    headers = {"User-Agent": "SiteBackup/1.2 (+local discovery; polite 0.2s delay)"}
    with httpx.Client(headers=headers, timeout=REQ_TIMEOUT) as client:
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
        queue: deque[tuple[str, int]] = deque([(canon_url(start_url), 0)])
        visited_fetch: set[str] = set()
        while queue and len(ordered) < max_pages:
            url, depth = queue.popleft()
            canon = canon_url(url)
            if canon in visited_fetch:
                continue
            visited_fetch.add(canon)
            page = fetch_page(client, canon)
            if page["error"]:
                log_lines.append(f"Discovery-Fehler {canon}: {page['error']}")
                continue
            if page["status"] != 200 or "html" not in page["content_type"].lower():
                continue
            html = page["text"]
            title = extract_title(html, canon)
            for e in ordered:
                if e["url"] == canon and not e["title"]:
                    e["title"] = title
            if depth < max_depth:
                for link in extract_links(html, canon):
                    lcanon = canon_url(link)
                    if lcanon in seen:
                        continue
                    if same_domain and not same_site(start_url, lcanon):
                        continue
                    if add(lcanon, depth + 1, "link"):
                        queue.append((lcanon, depth + 1))
                    if len(ordered) >= max_pages:
                        break
        # Titel nachladen fuer Sitemap-URLs, die der BFS-Lauf nicht besucht hat
        for e in ordered:
            if not e["title"] and len(visited_fetch) < max_pages:
                page = fetch_page(client, e["url"])
                if page["status"] == 200 and "html" in page["content_type"].lower():
                    e["title"] = extract_title(page["text"], e["url"])
                    visited_fetch.add(e["url"])
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

    Erstellt zuerst IMMER eine frische Discovery (neue Seiten im Zeitplan
    werden so automatisch erfasst), leert dann das pages-Verzeichnis (keine
    Altlasten im ZIP) und sichert jede Seite einzeln inkl. Assets
    (Offline-Kopie) und Volltext-Index.
    `progress(done, total, ok, failed, bytes, line)` nach jeder Seite.
    Mapping-Eintrag pro URL: {nr, title, page_name, file, status, bytes, assets}.
    Schreibt mapping.json + last.log + index.html (Inhaltsverzeichnis).
    """
    dest = job_dir(job_id)
    pages_dir = dest / "pages"
    disc = discover_site(job_id, start_url, max_pages, max_depth, same_domain)
    targets = [p for p in disc["pages"]][:max_pages]
    total = len(targets)
    # Altlasten entfernen (Seiten, die es nicht mehr gibt, duerfen nicht im ZIP bleiben)
    for old in pages_dir.rglob("*"):
        if old.is_file():
            old.unlink()
    for stale in ("mapping.json", "last.log", "index.html", f"job-{job_id}-backup.zip"):
        f = dest / stale
        if f.exists():
            f.unlink()
    mapping: dict[str, dict] = {}
    used_files: dict[str, str] = {}  # dateiname -> url (Kollisionsschutz)
    fts_rows: list[tuple[int, str, str, str]] = []
    log_lines: list[str] = []
    ok = failed = total_bytes = 0
    budget = {"bytes": MAX_ASSETS_BYTES, "count": MAX_ASSETS}

    def note(done: int, line: str):
        if progress:
            progress(done, total, ok, failed, total_bytes, line)

    headers = {"User-Agent": "SiteBackup/1.3 (+local backup; polite 0.2s delay)"}
    with httpx.Client(headers=headers, timeout=REQ_TIMEOUT) as client:
        for i, entry in enumerate(targets, start=1):
            canon = entry["url"]
            title = entry.get("title") or extract_title("", canon)
            page = fetch_page(client, canon)
            if page["error"]:
                line = f"[{i}/{total}] FEHLER {title} <{canon}> -> {page['error']}"
                log_lines.append(line)
                mapping[canon] = {"nr": i, "title": title, "page_name": title,
                                  "file": None, "status": 0, "note": page["error"]}
                failed += 1
                note(i, line)
                continue
            status, ctype = page["status"], page["content_type"]
            if status != 200:
                line = f"[{i}/{total}] FEHLER {title} <{canon}> -> HTTP {status}"
                log_lines.append(line)
                mapping[canon] = {"nr": i, "title": title, "page_name": title,
                                  "file": None, "status": status, "note": f"HTTP {status}"}
                failed += 1
                note(i, line)
                continue
            if "html" not in ctype.lower() and not page["text"].lstrip().lower().startswith(("<!doctype", "<html")):
                line = f"[{i}/{total}] SKIP (kein HTML, {ctype}): {title} <{canon}>"
                log_lines.append(line)
                mapping[canon] = {"nr": i, "title": title, "page_name": title,
                                  "file": None, "status": status, "note": f"kein HTML ({ctype})"}
                failed += 1
                note(i, line)
                continue
            if page["truncated"]:
                line = f"[{i}/{total}] SKIP (>8MB): {title} <{canon}>"
                log_lines.append(line)
                mapping[canon] = {"nr": i, "title": title, "page_name": title,
                                  "file": None, "status": status, "note": "zu gross"}
                failed += 1
                note(i, line)
                continue
            html = page["text"]
            title = extract_title(html, canon) or title
            text = page_visible_text(html)
            fname = url_to_filename(canon)
            if fname in used_files and used_files[fname] != canon:
                # Dateinamen-Kollision (z.B. gekuerzte Pfade): Hash anhaengen
                stem = fname[:-5] if fname.endswith(".html") else fname
                fname = f"{stem}_{hashlib.sha256(canon.encode()).hexdigest()[:8]}.html"
            used_files[fname] = canon
            # Pass 1: Assets laden + Referenzen umschreiben (Offline-Kopie)
            html, n_assets = localize_page(client, html, canon, fname, pages_dir, budget)
            fpath = pages_dir / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(html, encoding="utf-8")
            size = fpath.stat().st_size
            total_bytes += size
            ok += 1
            fts_rows.append((job_id, canon, title, text))
            line = (f"[{i}/{total}] OK [{status}] {title} <{canon}> -> {fname} "
                    f"({size} B, {n_assets} Assets)")
            log_lines.append(line)
            mapping[canon] = {"nr": i, "title": title, "page_name": title,
                              "file": fname, "status": status, "bytes": size,
                              "assets": n_assets}
            note(i, line)

        # Pass 2: interne <a>-Links auf lokale Dateien umschreiben
        file_map = {u: m["file"] for u, m in mapping.items() if m.get("file")}
        for canon, m in mapping.items():
            if not m.get("file"):
                continue
            fpath = pages_dir / m["file"]
            try:
                raw = fpath.read_text(encoding="utf-8")
                fixed = rewrite_internal_links(raw, canon, m["file"], file_map)
                if fixed != raw:
                    fpath.write_text(fixed, encoding="utf-8")
            except Exception:
                continue

    rebuild_fts(job_id, fts_rows)
    (dest / "mapping.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    (dest / "last.log").write_text("\n".join(log_lines), encoding="utf-8")
    write_index_html(job_id, disc.get("start_url", start_url), mapping)
    return {"ok": ok, "failed": failed, "bytes": total_bytes, "pages": total,
            "log": "\n".join(log_lines)}


def rebuild_fts(job_id: int, rows: list[tuple[int, str, str, str]]) -> None:
    """Volltext-Index eines Jobs komplett ersetzen (Tabelle + FTS-Spiegel)."""
    with Session(engine) as db:
        for old in db.scalars(select(PageIndex).where(PageIndex.job_id == job_id)).all():
            db.delete(old)
        for jid, url, title, text in rows:
            db.add(PageIndex(job_id=jid, url=url, title=title, text=text))
        db.commit()
    if not FTS_AVAILABLE:
        return
    try:
        with engine.begin() as conn:
            from sqlalchemy import text as _stext
            conn.execute(_stext("DELETE FROM page_fts WHERE job_id = :j"), {"j": job_id})
            for jid, url, title, text in rows:
                conn.execute(_stext("INSERT INTO page_fts (job_id, url, title, text) "
                                    "VALUES (:j, :u, :t, :x)"),
                             {"j": jid, "u": url, "t": title, "x": text})
    except Exception as exc:
        print(f"[fts] Rebuild fehlgeschlagen: {exc}")


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
        size = f'{(m.get("bytes") or 0) / 1024:.1f} KB' if m.get("bytes") else "–"
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

        def progress(done: int, total_pages: int, n_ok: int, n_failed: int, n_bytes: int, line: str):
            acc.append(line)
            with Session(engine) as db:
                r = db.get(Run, run_id)
                if r is None:
                    return
                r.log = "\n".join(acc)[-20000:]
                r.pages_ok, r.pages_failed, r.total_bytes = n_ok, n_failed, n_bytes
                db.commit()

        res = crawl_job(job_id, start_url, max_pages, max_depth, same_domain, progress=progress)
        ok, failed, total, log = res["ok"], res["failed"], res["bytes"], res["log"]
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"
        log = (("\n".join(acc) + "\n" if acc else "") + error)[-20000:]
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
        # Alle Dateien unter pages/ (HTML + assets/) + Index-Dateien
        for f in sorted(p for p in (dest / "pages").rglob("*") if p.is_file()):
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
    except Exception as exc:
        # Darf nie still sterben (sonst 'running'-Ruine in der UI)
        print(f"[run] Job {job_id} abgestuerzt: {exc}\n{traceback.format_exc(limit=5)}")
        try:
            with Session(engine) as db:
                latest = db.scalar(select(Run).where(Run.job_id == job_id).order_by(Run.id.desc()))
                if latest is not None and latest.status == "running":
                    latest.status = "error"
                    latest.finished_at = datetime.utcnow()
                    latest.error = f"Absturz: {type(exc).__name__}: {exc}"
                    db.commit()
                else:
                    db.add(Run(job_id=job_id, status="error", finished_at=datetime.utcnow(),
                               error=f"Absturz: {type(exc).__name__}: {exc}"))
                    db.commit()
        except Exception as exc2:
            print(f"[run] Fehlerstatus konnte nicht gespeichert werden: {exc2}")
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
        for p in db.scalars(select(PageIndex).where(PageIndex.job_id == job_id)).all():
            db.delete(p)
        db.delete(job)
        db.commit()
    if FTS_AVAILABLE:
        try:
            with engine.begin() as conn:
                from sqlalchemy import text as _stext
                conn.execute(_stext("DELETE FROM page_fts WHERE job_id = :j"), {"j": job_id})
        except Exception as exc:
            print(f"[fts] Cleanup fehlgeschlagen: {exc}")
    refresh_schedule()
    return {"deleted": job_id}


@app.post("/api/jobs/{job_id}/discover")
def trigger_discover(job_id: int):
    """PHASE 1: alle Unterseiten herausfinden (robots.txt + Sitemap + Links).

    Gibt die indexierte Liste zurueck: [{nr, url, title, depth, source}].
    """
    job = require_job(job_id)
    try:
        payload = discover_site(job.id, job.start_url, job.max_pages, job.max_depth, job.same_domain)
    except Exception as exc:
        # Voller Trace nur ins Server-Log (kein Pfad-Leak an Clients)
        print(f"[discover] Job {job_id}: {exc}\n{traceback.format_exc(limit=5)}")
        raise HTTPException(502, f"Discovery fehlgeschlagen: {type(exc).__name__}: {exc}")
    return payload


@app.get("/api/jobs/{job_id}/discover")
def get_discovery(job_id: int):
    require_job(job_id)
    payload = load_discovery(job_id)
    if not payload:
        return {"pages": [], "count": 0, "note": "Noch keine Discovery gelaufen – Button 'Seiten entdecken' klicken"}
    return payload


@app.post("/api/jobs/{job_id}/run")
def trigger_run(job_id: int):
    require_job(job_id)
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


@app.get("/api/search")
def search(q: str, job_id: int | None = None, limit: int = 20):
    """Volltextsuche ueber gesicherte Seiten (Titel + Text, BM25-Ranking).

    q: Suchbegriffe (min. 2 Zeichen). job_id: optional auf einen Job einschraenken.
    Antwort: [{job_id, url, title, file, snippet}]
    """
    if not FTS_AVAILABLE:
        raise HTTPException(503, "Volltextsuche auf diesem System nicht verfuegbar")
    q = (q or "").strip()[:200]
    if len(q) < 2:
        raise HTTPException(400, "Suchbegriff zu kurz (min. 2 Zeichen)")
    if job_id is not None:
        require_job(job_id)
    limit = max(1, min(50, limit))
    # FTS-Sonderzeichen neutralisieren: Tokens quoten, explizit AND
    tokens = [t.replace('"', '""') for t in q.split() if t.strip('" ')]
    if not tokens:
        raise HTTPException(400, "Kein gueltiger Suchbegriff")
    match = " AND ".join(f'"{t}"' for t in tokens)
    try:
        with engine.begin() as conn:
            from sqlalchemy import text as _stext
            rows = conn.execute(_stext(
                "SELECT job_id, url, title, "
                "snippet(page_fts, 3, '', '', ' …', 24) AS snip "
                "FROM page_fts WHERE page_fts MATCH :m "
                "AND (:j IS NULL OR job_id = :j) "
                "ORDER BY bm25(page_fts) LIMIT :n"),
                {"m": match, "j": job_id, "n": limit}).all()
    except Exception as exc:
        raise HTTPException(400, f"Suche fehlgeschlagen: {exc}")
    # Lokale Dateien dazu laden (ein Mapping pro beteiligtem Job)
    maps: dict[int, dict] = {}
    out = []
    for jid, url, title, snip in rows:
        if jid not in maps:
            try:
                maps[jid] = json.loads((job_dir(jid) / "mapping.json").read_text(encoding="utf-8"))
            except Exception:
                maps[jid] = {}
        out.append({"job_id": jid, "url": url, "title": title,
                    "file": (maps[jid].get(url) or {}).get("file"),
                    "snippet": (snip or "")[:400]})
    return {"query": q, "count": len(out), "results": out}


@app.get("/api/jobs/{job_id}/pages")
def list_pages(job_id: int):
    """Index: jede Unterseite mit Nr, Seitenname/Titel, URL und HTML-Datei."""
    require_job(job_id)
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
    job = require_job(job_id)
    idx = job_dir(job_id) / "index.html"
    if not idx.exists():
        raise HTTPException(404, "Noch kein Index vorhanden – erst Backup starten")
    return FileResponse(idx, media_type="text/html",
                        filename=backup_download_name(job, "index.html"))


@app.get("/api/jobs/{job_id}/download")
def download_zip(job_id: int):
    job = require_job(job_id)
    dest = job_dir(job_id)
    has_pages = any((dest / "pages").rglob("*.html"))
    if not has_pages:
        raise HTTPException(404, "Noch kein Backup vorhanden – erst 'Backup starten'")
    zpath = dest / f"job-{job_id}-backup.zip"
    try:
        make_zip(job_id)
    except Exception as exc:
        raise HTTPException(500, f"ZIP konnte nicht erstellt werden: {exc}")
    return FileResponse(zpath, media_type="application/zip",
                        filename=backup_download_name(job, "zip"))


@app.get("/api/jobs/{job_id}/page")
def download_page(job_id: int, file: str):
    require_job(job_id)
    # Path-Traversal-Schutz: nur relative .html-Pfade unter pages/
    if ("\x00" in file or ".." in file or file.startswith(("/", "\\"))
            or not file.endswith((".html", ".htm"))):
        raise HTTPException(400, "Ungueltiger Dateiname")
    fpath = (job_dir(job_id) / "pages" / file).resolve()
    root = (job_dir(job_id) / "pages").resolve()
    if root not in fpath.parents or not fpath.is_file():
        raise HTTPException(404, "Seite nicht gefunden")
    return FileResponse(fpath, media_type="text/html")


# Statische Web UI (muss NACH den /api-Routen registriert werden)
STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
