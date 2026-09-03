"""SiteBackup – komplette Webseiten lokal als HTML-Dateien sichern.

- Ein Job = eine Start-URL. Crawler folgt Unterseiten (same-domain).
- Jede Unterseite wird als .html-Datei unter data/jobs/<id>/ gespeichert.
- Download einzeln oder als ZIP. Zeitplan: manuell/stuendlich/taeglich/
  woechentlich/monatlich/cron (APScheduler, reboot-sicher via DB + Scheduler).
- Web UI: statische Datei static/index.html, keine Build-Tools noetig.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import traceback
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

app = FastAPI(title="SiteBackup", version="1.0.0")

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


def crawl_job(job_id: int, start_url: str, max_pages: int, max_depth: int, same_domain: bool) -> dict:
    """BFS-Crawl, jede Seite als HTML-Datei. Gibt Statistik + Mapping zurueck."""
    dest = job_dir(job_id)
    pages_dir = dest / "pages"
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    mapping: dict[str, dict] = {}
    log_lines: list[str] = []
    ok = failed = total_bytes = 0

    headers = {"User-Agent": "SiteBackup/1.0 (+local backup; polite 0.2s delay)"}
    with httpx.Client(headers=headers, timeout=20, follow_redirects=True, max_redirects=5) as client:
        while queue and len(visited) < max_pages:
            url, depth = queue.popleft()
            canonical, _ = urldefrag(url)
            if canonical in visited:
                continue
            visited.add(canonical)
            try:
                resp = client.get(canonical)
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype.lower() and not resp.text.lstrip().lower().startswith(("<!doctype", "<html")):
                    log_lines.append(f"SKIP (kein HTML, {ctype}): {canonical}")
                    mapping[canonical] = {"file": None, "status": resp.status_code, "note": f"kein HTML ({ctype})"}
                    failed += 1
                    continue
                html = resp.text
                if len(html.encode("utf-8")) > 8 * 1024 * 1024:
                    log_lines.append(f"SKIP (>8MB): {canonical}")
                    mapping[canonical] = {"file": None, "status": resp.status_code, "note": "zu gross"}
                    failed += 1
                    continue
                fname = url_to_filename(canonical)
                fpath = pages_dir / fname
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(html, encoding="utf-8")
                size = fpath.stat().st_size
                total_bytes += size
                ok += 1
                log_lines.append(f"OK [{resp.status_code}] Tiefe {depth}: {canonical} -> {fname} ({size} B)")
                mapping[canonical] = {"file": fname, "status": resp.status_code, "bytes": size}
                if depth < max_depth:
                    for link in extract_links(html, canonical):
                        lcanon, _ = urldefrag(link)
                        if lcanon in visited:
                            continue
                        if same_domain and not same_site(start_url, lcanon):
                            continue
                        if len(visited) + len(queue) >= max_pages * 2:
                            break
                        queue.append((lcanon, depth + 1))
            except Exception as exc:  # komplette Kette ins Log, nicht nur letzte Zeile
                tb = traceback.format_exc(limit=3).strip().splitlines()
                short = f"{type(exc).__name__}: {exc}"
                log_lines.append(f"FEHLER: {canonical} -> {short}")
                mapping[canonical] = {"file": None, "status": 0, "note": short, "trace": tb[-3:]}
                failed += 1

    (dest / "mapping.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    (dest / "last.log").write_text("\n".join(log_lines), encoding="utf-8")
    return {"ok": ok, "failed": failed, "bytes": total_bytes, "log": "\n".join(log_lines)}


def run_job_sync(job_id: int) -> int:
    """Einen Job jetzt ausfuehren (synchron). Gibt run_id zurueck."""
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
        res = crawl_job(job_id, start_url, max_pages, max_depth, same_domain)
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
        if (dest / "mapping.json").exists():
            zf.write(dest / "mapping.json", "mapping.json")
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
    dest = job_dir(job_id)
    mp = dest / "mapping.json"
    if not mp.exists():
        return {"pages": [], "note": "Noch kein Backup gelaufen"}
    mapping = json.loads(mp.read_text(encoding="utf-8"))
    return {"pages": [{"url": u, **v} for u, v in mapping.items()]}


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
