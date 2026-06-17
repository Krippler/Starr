"""
Basic test suite for the Starr DB Repair Flask app.
Run: pytest tests/ -v
"""

import json
import sys
import os
import tempfile
import sqlite3
import pytest

# Make sure we can import the app
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import server as srv


@pytest.fixture
def client(tmp_path):
    srv.app.config["TESTING"]    = True
    srv.app.config["BACKUP_DIR"] = tmp_path / "backups"
    srv.app.config["DB_DIR"]     = tmp_path / "data"
    (tmp_path / "backups").mkdir()
    (tmp_path / "data").mkdir()
    with srv.app.test_client() as c:
        yield c


# ── Health endpoints ──────────────────────────────────────────────────────────
def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert body["status"] == "ok"
    assert "time" in body


def test_readyz(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    assert json.loads(r.data)["status"] == "ready"


# ── Dashboard ─────────────────────────────────────────────────────────────────
def test_index_returns_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Starr DB Repair" in r.data


# ── Repair start validation ───────────────────────────────────────────────────
def test_start_missing_app(client):
    r = client.post("/api/repair/start",
                    data=json.dumps({"apikey": "x"}),
                    content_type="application/json")
    assert r.status_code == 400
    assert b"app must be" in r.data


def test_start_invalid_app(client):
    r = client.post("/api/repair/start",
                    data=json.dumps({"app": "prowlarr", "apikey": "x"}),
                    content_type="application/json")
    assert r.status_code == 400


def test_start_sportarr_recognized(client):
    """Sportarr should be recognized as a valid app (will fail at connection, not validation)."""
    r = client.post("/api/repair/start",
                    data=json.dumps({"app": "sportarr", "apikey": "x",
                                     "host": "127.0.0.1", "port": 1867}),
                    content_type="application/json")
    # 202 = job started (will fail at preflight since no real Sportarr running)
    # 409 = already running from a previous test leaking state — both are acceptable
    assert r.status_code in (202, 409)


def test_start_missing_apikey(client):
    r = client.post("/api/repair/start",
                    data=json.dumps({"app": "sonarr"}),
                    content_type="application/json")
    assert r.status_code == 400
    assert b"apikey" in r.data


def test_start_invalid_ops(client):
    r = client.post("/api/repair/start",
                    data=json.dumps({"app": "sonarr", "apikey": "x", "ops": ["bad_op"]}),
                    content_type="application/json")
    assert r.status_code == 400
    assert b"Unknown ops" in r.data


# ── Status while idle ─────────────────────────────────────────────────────────
def test_status_idle(client):
    r = client.get("/api/repair/status")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert body["running"] is False


# ── Stop while idle ───────────────────────────────────────────────────────────
def test_stop_when_idle(client):
    r = client.post("/api/repair/stop")
    assert r.status_code == 409


# ── Backups endpoint ──────────────────────────────────────────────────────────
def test_backups_empty(client):
    r = client.get("/api/backups")
    assert r.status_code == 200
    assert json.loads(r.data) == []


def test_backups_lists_files(client, tmp_path):
    # Create a fake backup file
    backup_dir = tmp_path / "backups"
    fake = backup_dir / "sonarr_20250101_120000.db"
    fake.write_bytes(b"x" * 1024)
    srv.app.config["BACKUP_DIR"] = backup_dir
    r = client.get("/api/backups")
    body = json.loads(r.data)
    assert len(body) == 1
    assert body[0]["name"] == "sonarr_20250101_120000.db"


# ── API apps endpoint ─────────────────────────────────────────────────────────
def test_api_apps_empty(client):
    # With no env vars set, should return empty list
    r = client.get("/api/apps")
    assert r.status_code == 200
    assert isinstance(json.loads(r.data), list)


def test_api_apps_returns_urlbase_and_masks_apikey(client):
    """All env-configured fields should round-trip; apikey is never sent verbatim."""
    srv.app.config["SONARR_HOST"]    = "sonarr.local"
    srv.app.config["SONARR_PORT"]    = 8989
    srv.app.config["SONARR_APIKEY"]  = "supersecret"
    srv.app.config["SONARR_URLBASE"] = "/sonarr"
    try:
        body = json.loads(client.get("/api/apps").data)
        sonarr = next(a for a in body if a["app"] == "sonarr")
        assert sonarr["host"]       == "sonarr.local"
        assert sonarr["port"]       == 8989
        assert sonarr["urlbase"]    == "/sonarr"
        assert sonarr["configured"] is True
        assert sonarr["apikey"]     == "***"          # masked
        assert "supersecret" not in client.get("/api/apps").data.decode()
    finally:
        srv.app.config["SONARR_HOST"]    = ""
        srv.app.config["SONARR_APIKEY"]  = ""
        srv.app.config["SONARR_URLBASE"] = ""


def test_api_apps_includes_app_when_only_apikey_set(client):
    """An app with apikey but no host should still appear (previously it was filtered out)."""
    srv.app.config["RADARR_APIKEY"] = "key-only"
    try:
        body = json.loads(client.get("/api/apps").data)
        assert any(a["app"] == "radarr" for a in body)
    finally:
        srv.app.config["RADARR_APIKEY"] = ""


# ── SQLite repair logic (unit tests, no network) ──────────────────────────────
def test_sqlite_integrity(tmp_path):
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    con.execute("INSERT INTO t VALUES (1, 'hello')")
    con.commit(); con.close()

    rows = sqlite3.connect(str(db)).execute("PRAGMA integrity_check").fetchall()
    assert rows[0][0] == "ok"


def test_sqlite_vacuum(tmp_path):
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, data TEXT)")
    for i in range(1000):
        con.execute("INSERT INTO t VALUES (?, ?)", (i, "x" * 100))
    con.commit()
    con.execute("DELETE FROM t WHERE id > 100")
    con.commit()
    before = db.stat().st_size
    con.execute("VACUUM"); con.commit(); con.close()
    after = db.stat().st_size
    assert after <= before   # VACUUM should not increase size


def test_sqlite_reindex(tmp_path):
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    con.execute("CREATE INDEX idx_val ON t(val)")
    con.execute("INSERT INTO t VALUES (1, 'a')")
    con.commit()
    con.execute("REINDEX"); con.commit(); con.close()
    # If we get here without an exception, REINDEX succeeded


def test_emit_appends_history():
    srv._job.reset()
    srv._job.start_time = srv.time.time()
    initial = len(srv._job.history)
    srv.emit("TEST", "hello world", "info")
    assert len(srv._job.history) == initial + 1
    assert srv._job.history[-1]["tag"] == "TEST"
    assert srv._job.history[-1]["msg"] == "hello world"


def test_sportarr_in_app_defaults():
    """Sportarr must be registered with correct port and dbname."""
    assert "sportarr" in srv.APP_DEFAULTS
    assert srv.APP_DEFAULTS["sportarr"]["port"] == 1867
    assert srv.APP_DEFAULTS["sportarr"]["dbname"] == "sportarr.db"
