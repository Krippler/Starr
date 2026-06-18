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


@pytest.fixture(autouse=True)
def _isolate_job():
    """Some tests POST /api/repair/start, which spawns a real worker thread.
    Make sure no leaked, still-running job bleeds into the next test (which
    would make /api/repair/start return 409 instead of validating input)."""
    srv._job.aborted = True           # signal any leaked worker to wind down
    for _ in range(40):               # wait up to ~2s for it to exit
        if not srv._job.running:
            break
        srv.time.sleep(0.05)
    srv._job.reset()
    yield


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


def test_api_apps_returns_full_config(client):
    """All env-configured fields, including the apikey, round-trip so the
    dashboard form can pre-fill them. The endpoint is gated by SECRET_KEY."""
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
        assert sonarr["apikey"]     == "supersecret"
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


def test_shutdown_uses_docker_when_container_name_provided(monkeypatch):
    """When container_name is set and the Docker SDK is reachable, shutdown
    must stop the container directly (no app-API call, no stability polling)."""
    srv._job.reset()
    srv._job.start_time = srv.time.time()
    monkeypatch.setattr(srv.time, "sleep", lambda *a, **k: None)

    api_called = []
    monkeypatch.setattr(srv, "_shutdown_app", lambda *a, **k: api_called.append(a))

    stopped = []
    class FakeContainer:
        def stop(self, timeout=30): stopped.append(timeout)
    monkeypatch.setattr(srv, "_docker_container", lambda n: ("client", FakeContainer()))

    # Container is "stopped" → next status read returns None
    monkeypatch.setattr(srv, "_get_status", lambda *a, **k: None)

    cfg = {"app": "sonarr", "host": "h", "port": 1, "apikey": "k",
           "container_name": "sonarr"}
    assert srv._step_shutdown(cfg) is True
    assert stopped == [30],  "container.stop must be called with the default timeout"
    assert api_called == [], "the *arr shutdown API must NOT be called when the container is being stopped"
    assert cfg.get("_docker_managed") == "sonarr"


def test_shutdown_falls_back_to_api_when_socket_unavailable(monkeypatch):
    """If container_name is set but the daemon is unreachable, shutdown must
    fall back to the *arr shutdown API path (warn + continue)."""
    srv._job.reset()
    srv._job.start_time = srv.time.time()
    monkeypatch.setattr(srv.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_docker_container", lambda n: (None, None))
    monkeypatch.setattr(srv, "_shutdown_app", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_get_status", lambda *a, **k: None)

    cfg = {"app": "sonarr", "host": "h", "port": 1, "apikey": "k",
           "container_name": "sonarr"}
    assert srv._step_shutdown(cfg) is True
    assert cfg.get("_docker_managed") is None, "Should not mark managed when fallback used"


def test_shutdown_succeeds_when_app_stays_offline(monkeypatch):
    """A normal shutdown: app goes offline and stays offline through the
    stability window."""
    srv._job.reset()
    srv._job.start_time = srv.time.time()
    monkeypatch.setattr(srv.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_shutdown_app", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_get_status", lambda *a, **k: None)  # always offline
    assert srv._step_shutdown({"app": "sonarr", "host": "h", "port": 1, "apikey": "k"}) is True


def test_shutdown_aborts_if_app_restarts(monkeypatch):
    """If the app comes back online during the stability window (e.g. a Docker
    restart policy), shutdown must fail rather than report success."""
    srv._job.reset()
    srv._job.start_time = srv.time.time()
    monkeypatch.setattr(srv.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_shutdown_app", lambda *a, **k: None)
    # offline once (enters the stability check), then back online (restarted)
    seq = iter([None, {"version": "x"}])
    monkeypatch.setattr(srv, "_get_status", lambda *a, **k: next(seq, {"version": "x"}))
    assert srv._step_shutdown({"app": "sonarr", "host": "h", "port": 1, "apikey": "k"}) is False


def test_repair_worker_aborts_when_backup_fails(monkeypatch, tmp_path):
    """If _step_backup returns None on a non-dry, non-no_backup run, the
    worker MUST abort before running any SQLite operations on the source DB."""
    srv._job.reset()
    srv.app.config["BACKUP_DIR"] = tmp_path / "backups"
    (tmp_path / "backups").mkdir()

    # Make a real source DB so _step_repair would otherwise mutate it
    db = tmp_path / "src.db"
    import sqlite3
    sqlite3.connect(str(db)).execute("CREATE TABLE t(x)").connection.commit()

    monkeypatch.setattr(srv, "_step_preflight", lambda cfg: str(db))
    monkeypatch.setattr(srv, "_step_shutdown",  lambda cfg: True)
    monkeypatch.setattr(srv, "_step_backup",    lambda cfg, p: None)   # simulate failure
    monkeypatch.setattr(srv, "_step_restart",   lambda cfg, r: None)

    repair_calls = []
    def fail_if_called(cfg, p):
        repair_calls.append((cfg, p))
        return {}
    monkeypatch.setattr(srv, "_step_repair", fail_if_called)

    srv._repair_worker({"app": "sonarr", "host": "x", "port": 1, "apikey": "k", "ops": ["integrity"]})

    assert repair_calls == [], "Repair must NOT run when backup fails"
    assert srv._job.result["status"] == "error"
    assert "backup" in srv._job.result["message"].lower()


def test_worker_emits_done_event_on_preflight_failure(monkeypatch):
    """If _step_preflight fails (e.g. DB not mounted), the worker must still
    emit a terminal __DONE__ event so the UI clears 'running' state and the
    Stop button isn't required."""
    srv._job.reset()
    monkeypatch.setattr(srv, "_step_preflight", lambda cfg: None)
    monkeypatch.setattr(srv, "_step_shutdown",  lambda cfg: True)  # not reached
    monkeypatch.setattr(srv, "_step_backup",    lambda cfg, p: None)  # not reached
    monkeypatch.setattr(srv, "_step_repair",    lambda cfg, p: {})    # not reached
    monkeypatch.setattr(srv, "_step_restart",   lambda cfg, r: None)  # not reached

    srv._repair_worker({"app": "sonarr", "host": "x", "port": 1, "apikey": "k",
                        "ops": ["integrity"]})

    assert srv._job.running is False
    done_entries = [h for h in srv._job.history if h.get("cls") == "__done__"]
    assert len(done_entries) == 1, "exactly one __DONE__ should be emitted"
    assert srv._job.result["status"] == "error"


def test_lidarr_uses_api_v1():
    """Lidarr must be registered with the v1 API; Sonarr/Radarr/Sportarr v3."""
    assert srv.APP_DEFAULTS["lidarr"]["api"] == "v1"
    assert srv.APP_DEFAULTS["sonarr"]["api"] == "v3"
    assert srv.APP_DEFAULTS["radarr"]["api"] == "v3"
    assert srv.APP_DEFAULTS["sportarr"]["api"] == "v3"


def test_get_status_uses_api_version(monkeypatch):
    """_get_status must hit /api/<version>/system/status for the given app."""
    seen = {}
    class FakeResp:
        status_code = 200
        def json(self): return {"version": "x"}
    def fake_get(url, headers=None, timeout=None):
        seen["url"] = url
        return FakeResp()
    monkeypatch.setattr(srv.requests, "get", fake_get)

    srv._get_status("h", 8686, "k", api="v1")
    assert "/api/v1/system/status" in seen["url"]
    srv._get_status("h", 8989, "k", api="v3")
    assert "/api/v3/system/status" in seen["url"]
