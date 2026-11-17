import os
import sys
import tempfile
from pathlib import Path

import pytest

# Env VOR dem App-Import setzen: eigenes Datenverzeichnis + Private-IPs erlaubt
_TMP = tempfile.mkdtemp(prefix="sitebackup-test-")
os.environ["SITEBACKUP_DATA_DIR"] = _TMP
os.environ["SITEBACKUP_ALLOW_PRIVATE"] = "1"
os.environ["SITEBACKUP_PASSWORD"] = ""  # kein Passwort in Tests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sitebackup"))

import app as appmod  # noqa: E402


@pytest.fixture()
def a():
    return appmod


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    return TestClient(appmod.app)  # ohne lifespan (kein Scheduler in Tests)


@pytest.fixture()
def job(client):
    """Ein einfacher manueller Job."""
    r = client.post("/api/jobs", json={
        "name": "Testwebseite", "start_url": "https://example.com/",
        "max_pages": 10, "max_depth": 2, "same_domain": True,
        "schedule": "manual", "enabled": True,
    })
    assert r.status_code == 200, r.text
    return r.json()
