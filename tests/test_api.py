# API-Tests (TestClient, kein echtes Netzwerk: Crawler via Monkeypatch gefaket).
import json

import pytest

PNG_1PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
           b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

PAGE_HOME = """<!DOCTYPE html><html><head><title>Startseite</title>
<link rel="stylesheet" href="https://cdn.example.com/style.css"></head>
<body><h1>Hallo Welt</h1><p>Willkommen auf der Startseite.</p>
<a href="/kontakt/">Kontakt</a>
<img src="https://cdn.example.com/pic.jpg?v=1" alt="Bild"></body></html>"""

PAGE_KONTAKT = """<!DOCTYPE html><html><head><title>Kontakt</title></head>
<body><h1>Kontakt</h1><p>Impressum und Kontaktseite.</p>
<a href="/">Zur Startseite</a></body></html>"""


def fake_fetch_page_factory():
    def fake_fetch_page(client, url, max_bytes=8 * 1024 * 1024):
        if url.endswith("style.css"):
            return {"url": url, "status": 200, "content_type": "text/css",
                    "text": "body{color:red}", "truncated": False, "error": ""}
        if url == "https://example.com/":
            return {"url": url, "status": 200, "content_type": "text/html; charset=utf-8",
                    "text": PAGE_HOME, "truncated": False, "error": ""}
        if url == "https://example.com/kontakt":
            return {"url": url, "status": 200, "content_type": "text/html; charset=utf-8",
                    "text": PAGE_KONTAKT, "truncated": False, "error": ""}
        if url.endswith("robots.txt") or url.endswith("sitemap.xml"):
            return {"url": url, "status": 404, "content_type": "text/plain",
                    "text": "", "truncated": False, "error": ""}
        return {"url": url, "status": 0, "content_type": "", "text": "",
                "truncated": False, "error": "offline"}
    return fake_fetch_page


def fake_fetch_raw_factory():
    def fake_fetch_raw(client, url, max_bytes=0):
        return {"url": url, "status": 200, "content_type": "image/jpeg",
                "raw": PNG_1PX, "truncated": False, "error": ""}
    return fake_fetch_raw


@pytest.fixture()
def simulated_backup(client, job, monkeypatch, a):
    """Laeuft crawl_job komplett mit gefaktem Netz (Discovery + Seiten + Assets)."""
    monkeypatch.setattr(a, "fetch_page", fake_fetch_page_factory())
    monkeypatch.setattr(a, "fetch_raw", fake_fetch_raw_factory())
    monkeypatch.setattr(a, "fetch_robots_sitemaps", lambda c, u: ([], []))
    r = client.post(f"/api/jobs/{job['id']}/run")
    assert r.status_code == 200, r.text
    # Warten bis Run fertig (threaded, aber Mock ist sofort fertig)
    import time
    for _ in range(50):
        runs = client.get("/api/runs", params={"job_id": job["id"]}).json()
        if runs and runs[0]["status"] in ("ok", "error"):
            return runs[0]
        time.sleep(0.1)
    raise AssertionError("Run nicht fertig geworden")


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["app"] == "SiteBackup"


# ---------------------------------------------------------------- Job-CRUD
def test_create_job_validation(client):
    for payload, why in [
        ({"name": "x", "start_url": "example.com", "schedule": "manual"}, "URL ohne Schema"),
        ({"name": "x", "start_url": "https://e.com", "schedule": "falsch"}, "unbekannter Zeitplan"),
        ({"name": "x", "start_url": "https://e.com", "schedule": "daily", "schedule_time": "25:99"}, "ungueltige Zeit"),
        ({"name": "x", "start_url": "https://e.com", "schedule": "cron", "cron_expr": ""}, "cron ohne Ausdruck"),
    ]:
        r = client.post("/api/jobs", json=payload)
        assert r.status_code == 400, f"{why}: {r.status_code} {r.text}"


def test_create_manual_job_without_time(client):
    """Fix: manual-Jobs brauchen keine schedule_time mehr."""
    r = client.post("/api/jobs", json={"name": "Manual", "start_url": "https://a.example/",
                                       "schedule": "manual", "schedule_time": ""})
    assert r.status_code == 200, r.text
    assert r.json()["schedule"] == "manual"


def test_job_roundtrip(client, job):
    jobs = client.get("/api/jobs").json()
    assert any(j["id"] == job["id"] and j["name"] == "Testwebseite" for j in jobs)
    r = client.put(f"/api/jobs/{job['id']}", json={
        "name": "Umbenannt", "start_url": "https://example.com/", "max_pages": 5,
        "max_depth": 1, "same_domain": True, "schedule": "daily",
        "schedule_time": "04:30", "enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Umbenannt" and r.json()["schedule_time"] == "04:30"


def test_delete_job(client, job):
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
    assert client.get("/api/jobs/{job['id']}".replace("{job['id']}", str(job["id"]))).status_code == 404


def test_missing_job_404(client):
    assert client.get("/api/jobs/9999/pages").status_code == 404
    assert client.get("/api/jobs/9999/download").status_code == 404
    assert client.get("/api/jobs/9999/download-html").status_code == 404


# ---------------------------------------------------------------- Downloads vor dem Backup
def test_downloads_before_backup_404(client, job):
    assert client.get(f"/api/jobs/{job['id']}/download").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}/download-html").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}/single-page",
                      params={"url": "https://example.com"}).status_code == 404


# ---------------------------------------------------------------- Path-Traversal-Schutz
def test_page_endpoint_blocks_traversal(client, job):
    r = client.get(f"/api/jobs/{job['id']}/page", params={"file": "../../mapping.json"})
    assert r.status_code in (400, 404)


def test_preview_blocks_traversal(client, job):
    r = client.get(f"/api/jobs/{job['id']}/preview/..%2F..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (400, 404)


def test_preview_nonexistent_404(client, job):
    assert client.get(f"/api/jobs/{job['id']}/preview/gibts-nicht.html").status_code == 404


# ---------------------------------------------------------------- Kompletter Backup-Durchlauf
def test_backup_run_e2e(client, job, simulated_backup, a):
    run = simulated_backup
    assert run["status"] == "ok", run["error"]
    assert run["pages_ok"] == 2 and run["pages_failed"] == 0

    dest = a.job_dir(job["id"])
    mapping = json.loads((dest / "mapping.json").read_text(encoding="utf-8"))
    assert mapping["https://example.com/"]["file"] == "index.html"
    assert mapping["https://example.com/kontakt"]["file"] == "kontakt.html"

    # Asset-Offline-Kopie: Query-Hash VOR der Extension (Fix); Seite im Root
    # -> Pfad ohne ../-Prefix
    home_html = (dest / "pages" / "index.html").read_text(encoding="utf-8")
    assert 'src="assets/cdn.example.com/pic_' in home_html
    assert '.jpg"/>' in home_html
    asset_files = list((dest / "pages" / "assets").rglob("*.jpg"))
    assert len(asset_files) == 1

    # <base> entfernt (Offline-Kopie-Fix)
    assert "<base" not in home_html

    # Pass 2: interner Link umgeschrieben
    assert 'href="kontakt.html"' in home_html
    kontakt_html = (dest / "pages" / "kontakt.html").read_text(encoding="utf-8")
    assert 'href="index.html"' in kontakt_html

    # ZIP enthaelt Seiten + Assets + mapping + index
    import zipfile
    zf = zipfile.ZipFile(dest / f"job-{job['id']}-backup.zip")
    names = zf.namelist()
    assert "pages/index.html" in names and "pages/kontakt.html" in names
    assert "mapping.json" in names and "index.html" in names
    assert any(n.startswith("pages/assets/") for n in names)


def test_download_zip_endpoint(client, job, simulated_backup):
    r = client.get(f"/api/jobs/{job['id']}/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"].endswith('.zip"')


def test_pages_listing(client, job, simulated_backup):
    d = client.get(f"/api/jobs/{job['id']}/pages").json()
    assert len(d["pages"]) == 2
    assert d["pages"][0]["title"] == "Startseite"
    assert d["pages"][0]["file"] == "index.html"


# ---------------------------------------------------------------- Single-File-Export (Kern-Feature)
def test_download_single_html(client, job, simulated_backup):
    r = client.get(f"/api/jobs/{job['id']}/download-html")
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"].endswith('.html"')
    doc = r.text
    # Asset als data-URI eingebettet
    assert "data:image/jpeg;base64," in doc or "data:image/png;base64," in doc
    # kein externes CSS mehr verlinkt (eingebettet oder verworfen)
    assert 'href="https://cdn.example.com/style.css"' not in doc
    # Inhaltsverzeichnis + Sprunganker + offline Navigation
    assert "#pg-1" in doc and 'id="pg-1"' in doc and 'id="pg-2"' in doc
    assert "Startseite" in doc and "Kontakt" in doc
    # interne Links der Seiten zeigen jetzt auf Anker statt Pfad
    assert 'href="#pg-2"' in doc and 'href="#pg-1"' in doc


def test_single_page_download(client, job, simulated_backup):
    r = client.get(f"/api/jobs/{job['id']}/single-page", params={"url": "https://example.com"})
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"].endswith('.html"')
    doc = r.text
    assert "data:image/" in doc          # Asset inline
    assert 'href="https://example.com/kontakt"' in doc  # interne Links -> Original-URL
    assert "data:text/css;base64," in doc or "css" not in doc


def test_single_page_unknown_url_404(client, job, simulated_backup):
    r = client.get(f"/api/jobs/{job['id']}/single-page", params={"url": "https://example.com/gibtsnicht"})
    assert r.status_code == 404


# ---------------------------------------------------------------- Preview (In-App-Browsing inkl. Assets)
def test_preview_serves_page_and_assets(client, job, simulated_backup, a):
    r = client.get(f"/api/jobs/{job['id']}/preview/index.html")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    asset = next((p for p in (a.job_dir(job["id"]) / "pages" / "assets").rglob("*.jpg")
                  if p.is_file()), None)
    assert asset is not None
    rel = asset.relative_to(a.job_dir(job["id"]) / "pages").as_posix()
    r2 = client.get(f"/api/jobs/{job['id']}/preview/{rel}")
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("image/")
    assert r2.content == asset.read_bytes()


# ---------------------------------------------------------------- Suche (FTS)
def test_search_after_backup(client, job, simulated_backup):
    r = client.get("/api/search", params={"q": "Startseite"})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results and results[0]["file"] == "index.html"


def test_search_too_short(client):
    assert client.get("/api/search", params={"q": "a"}).status_code == 400


def test_search_no_result_ok(client, job, simulated_backup):
    r = client.get("/api/search", params={"q": "zzznotfoundzzz"})
    assert r.status_code == 200 and r.json()["count"] == 0


# ---------------------------------------------------------------- Discovery-Endpoint
def test_discover_endpoint(client, job, monkeypatch, a):
    monkeypatch.setattr(a, "fetch_page", fake_fetch_page_factory())
    monkeypatch.setattr(a, "fetch_robots_sitemaps", lambda c, u: ([], []))
    r = client.post(f"/api/jobs/{job['id']}/discover")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["count"] == 2
    urls = [p["url"] for p in payload["pages"]]
    assert "https://example.com/" in urls and "https://example.com/kontakt" in urls
    assert payload["pages"][0]["title"] == "Startseite"


def test_discover_error_502(client, job, monkeypatch, a):
    def boom(*args, **kwargs):
        raise RuntimeError("kaputt")
    monkeypatch.setattr(a, "discover_site", boom)
    r = client.post(f"/api/jobs/{job['id']}/discover")
    assert r.status_code == 502 and "RuntimeError" in r.json()["detail"]


# ---------------------------------------------------------------- Auswahl-Backup (nur gewaehlte Seiten)
def test_run_with_selected_urls(client, job, monkeypatch, a):
    """POST /run mit {"urls": [...]} sichert nur die ausgewaehlten Seiten."""
    monkeypatch.setattr(a, "fetch_page", fake_fetch_page_factory())
    monkeypatch.setattr(a, "fetch_raw", fake_fetch_raw_factory())
    monkeypatch.setattr(a, "fetch_robots_sitemaps", lambda c, u: ([], []))
    r = client.post(f"/api/jobs/{job['id']}/run",
                    json={"urls": ["https://example.com/kontakt"]})
    assert r.status_code == 200 and r.json()["selected"] == 1
    import time
    for _ in range(50):
        runs = client.get("/api/runs", params={"job_id": job["id"]}).json()
        if runs and runs[0]["status"] in ("ok", "error"):
            break
        time.sleep(0.1)
    run = runs[0]
    assert run["status"] == "ok", run["error"]
    assert run["pages_ok"] == 1
    mapping = json.loads((a.job_dir(job["id"]) / "mapping.json").read_text(encoding="utf-8"))
    assert list(mapping.keys()) == ["https://example.com/kontakt"]
    assert (a.job_dir(job["id"]) / "pages" / "kontakt.html").is_file()
    assert not (a.job_dir(job["id"]) / "pages" / "index.html").exists()


# ---------------------------------------------------------------- max_pages=0 = alle
def test_max_pages_zero_means_all(client, monkeypatch, a):
    """Fix: 0 bedeutet 'alle Seiten finden' (statt 100er-Deckel)."""
    r = client.post("/api/jobs", json={
        "name": "Alle", "start_url": "https://example.com/",
        "max_pages": 0, "max_depth": 2, "same_domain": True, "schedule": "manual"})
    assert r.status_code == 200, r.text
    assert r.json()["max_pages"] == 0
    monkeypatch.setattr(a, "fetch_page", fake_fetch_page_factory())
    monkeypatch.setattr(a, "fetch_raw", fake_fetch_raw_factory())
    monkeypatch.setattr(a, "fetch_robots_sitemaps", lambda c, u: ([], []))
    r = client.post(f"/api/jobs/{r.json()['id']}/run")
    assert r.status_code == 200
    import time
    for _ in range(50):
        runs = client.get("/api/runs", params={"job_id": r.json()["job_id"]}).json()
        if runs and runs[0]["status"] in ("ok", "error"):
            break
        time.sleep(0.1)
    assert runs[0]["status"] == "ok" and runs[0]["pages_ok"] == 2


def test_max_pages_above_cap_rejected(client):
    r = client.post("/api/jobs", json={"name": "X", "start_url": "https://e.example/",
                                       "max_pages": 50000, "schedule": "manual"})
    assert r.status_code == 422  # pydantic-Limit (Cap 20000)


def test_default_max_pages_is_1000(client):
    r = client.post("/api/jobs", json={"name": "Default", "start_url": "https://e.example/",
                                       "schedule": "manual"})
    assert r.status_code == 200
    assert r.json()["max_pages"] == 1000
