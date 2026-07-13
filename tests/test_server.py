"""
Basic test suite for the Starr DB Repair Flask app.
Run: pytest tests/ -v
"""

import json
import re
import sys
import os
import tempfile
import sqlite3
import pytest

# Don't spin up the background APScheduler when importing the server module
# under tests — its persistent thread would block pytest from exiting.
os.environ["STARR_DISABLE_SCHEDULER"] = "1"

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
    srv.app.config["APPDATA_DIR"] = tmp_path / "data"
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
                                     "url": "http://127.0.0.1:1867"}),
                    content_type="application/json")
    # 202 = job started (will fail at preflight since no real Sportarr running)
    # 409 = already running from a previous test leaking state — both are acceptable
    assert r.status_code in (202, 409)


def test_start_missing_url(client):
    """Without a URL (and no discovery) the resolver should reject with a clear
    error before checking apikey."""
    r = client.post("/api/repair/start",
                    data=json.dumps({"app": "sonarr", "apikey": "x"}),
                    content_type="application/json")
    assert r.status_code == 400
    assert b"URL" in r.data or b"url" in r.data


def test_start_missing_apikey(client):
    """A URL with no apikey (no env apikey either) is rejected with an apikey
    error."""
    r = client.post("/api/repair/start",
                    data=json.dumps({"app": "sonarr",
                                     "url": "http://127.0.0.1:8989"}),
                    content_type="application/json")
    assert r.status_code == 400
    assert b"apikey" in r.data


def test_start_invalid_ops(client):
    r = client.post("/api/repair/start",
                    data=json.dumps({"app": "sonarr", "apikey": "x", "ops": ["bad_op"],
                                     "url": "http://127.0.0.1:8989"}),
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


def test_api_apps_returns_url_and_apikey(client):
    """Env-configured URL + apikey round-trip through /api/apps for the UI to
    pre-fill. The endpoint is gated by SECRET_KEY."""
    srv.app.config["SONARR_URL"]    = "http://sonarr.local:8989/sonarr"
    srv.app.config["SONARR_APIKEY"] = "supersecret"
    try:
        body = json.loads(client.get("/api/apps").data)
        sonarr = next(a for a in body if a["app"] == "sonarr")
        assert sonarr["url"]        == "http://sonarr.local:8989/sonarr"
        assert sonarr["apikey"]     == "supersecret"
        assert sonarr["configured"] is True
    finally:
        srv.app.config["SONARR_URL"]    = ""
        srv.app.config["SONARR_APIKEY"] = ""


def test_api_apps_includes_app_when_only_apikey_set(client):
    """An app with apikey but no URL should still appear so the UI shows it
    (the discovery hint will tell the user to add a URL)."""
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


def test_new_arr_apps_registered():
    """Readarr/Prowlarr (v1) and Whisparr (v3) are registered with correct
    ports + API versions, and surface through /api/apps when an apikey is set."""
    assert srv.APP_DEFAULTS["readarr"]  == {"port": 8787, "dbname": "readarr.db",  "api": "v1"}
    assert srv.APP_DEFAULTS["prowlarr"] == {"port": 9696, "dbname": "prowlarr.db", "api": "v1"}
    assert srv.APP_DEFAULTS["whisparr"] == {"port": 6969, "dbname": "whisparr.db", "api": "v3"}


def test_new_apps_appear_in_api_apps(client):
    srv.app.config["WHISPARR_APIKEY"] = "wk"
    try:
        body = json.loads(client.get("/api/apps").data)
        assert any(a["app"] == "whisparr" for a in body)
    finally:
        srv.app.config["WHISPARR_APIKEY"] = ""


def test_get_status_uses_api_version(monkeypatch):
    """_get_status must hit /api/<version>/system/status for the given app."""
    seen = {}
    class FakeResp:
        status_code = 200
        def json(self): return {"version": "x"}
    def fake_get(url, headers=None, params=None, timeout=None):
        seen["url"] = url
        return FakeResp()
    monkeypatch.setattr(srv.requests, "get", fake_get)

    srv._get_status("h", 8686, "k", api="v1")
    assert "/api/v1/system/status" in seen["url"]
    srv._get_status("h", 8989, "k", api="v3")
    assert "/api/v3/system/status" in seen["url"]


# ── Scheduler ─────────────────────────────────────────────────────────────────
def test_schedule_store_round_trip(tmp_path):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
    from schedules import ScheduleStore
    store = ScheduleStore(tmp_path / ".schedules.json")
    sched = store.add({
        "name": "Nightly",
        "app": "sonarr",
        "ops": ["integrity", "foreign_keys"],
        "cron": "0 3 * * *",
    })
    assert sched["id"]
    assert sched["skip_if_clean"] is True
    assert sched["enabled"] is True
    assert (tmp_path / ".schedules.json").exists()

    # Reload from disk
    store2 = ScheduleStore(tmp_path / ".schedules.json")
    assert len(store2.all()) == 1
    assert store2.get(sched["id"])["name"] == "Nightly"

    # Update
    updated = store2.update(sched["id"], {"enabled": False})
    assert updated["enabled"] is False

    # Delete
    assert store2.delete(sched["id"]) is True
    assert store2.get(sched["id"]) is None


def test_schedule_store_validates(tmp_path):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
    from schedules import ScheduleStore
    store = ScheduleStore(tmp_path / ".s.json")
    with pytest.raises(ValueError, match="app"):
        store.add({"app": "notarealapp", "ops": ["integrity"], "cron": "0 3 * * *"})
    with pytest.raises(ValueError, match="ops"):
        store.add({"app": "sonarr", "ops": ["bad"], "cron": "0 3 * * *"})
    with pytest.raises(ValueError, match="cron"):
        store.add({"app": "sonarr", "ops": ["integrity"], "cron": "every other tuesday"})


def test_probe_db_clean_skips_repair(monkeypatch, tmp_path):
    """When skip_if_clean is set and the probe reports clean, the worker must
    NOT call _step_shutdown / _step_backup / _step_repair."""
    srv._job.reset()
    db = tmp_path / "src.db"
    sqlite3.connect(str(db)).execute("CREATE TABLE t(x)").connection.commit()

    monkeypatch.setattr(srv, "_step_preflight", lambda cfg: str(db))
    monkeypatch.setattr(srv, "_probe_db_clean", lambda p: (True, "clean"))
    called = []
    monkeypatch.setattr(srv, "_step_shutdown", lambda cfg: called.append("shutdown") or True)
    monkeypatch.setattr(srv, "_step_backup",   lambda cfg, p: called.append("backup") or "x")
    monkeypatch.setattr(srv, "_step_repair",   lambda cfg, p: called.append("repair") or {})
    monkeypatch.setattr(srv, "_step_restart",  lambda cfg, r: called.append("restart"))

    srv._repair_worker({"app": "sonarr", "host": "h", "port": 1, "apikey": "k",
                        "ops": ["integrity"], "skip_if_clean": True})

    assert called == [], f"clean probe must short-circuit; got {called}"
    assert srv._job.result["status"] == "clean"


# ── URL parser ────────────────────────────────────────────────────────────────
def test_split_url_handles_common_shapes():
    """_split_url must parse host:port, http://host:port/, and url-base."""
    assert srv._split_url("http://172.17.0.12:8989") == ("172.17.0.12", 8989, "")
    assert srv._split_url("http://sonarr:8989/sonarr") == ("sonarr", 8989, "/sonarr")
    # Bare host:port → treated as http://
    assert srv._split_url("sonarr:8989") == ("sonarr", 8989, "")
    # Default port fallback when missing
    assert srv._split_url("http://sonarr", default_port=8989) == ("sonarr", 8989, "")
    # Empty
    h, p, b = srv._split_url("", default_port=1867)
    assert h == "" and p == 1867 and b == ""


def test_api_discover_endpoint_returns_payload(client, monkeypatch):
    """The /api/discover route returns the discovery cache structure."""
    monkeypatch.setattr(srv._discovery, "discover", lambda: {
        "docker_available": False, "appdata": {"host_root": None, "container_root": "/appdata"},
        "apps": [], "warnings": ["test"]
    })
    r = client.post("/api/discover")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert body["docker_available"] is False
    assert body["warnings"] == ["test"]


def test_apps_url_uses_request_host_and_published_port(client, monkeypatch):
    """The URL returned to the UI should use the host the browser used to
    reach Starr, plus the container's published host port — not the bridge IP."""
    monkeypatch.setitem(srv._discovery_cache, "apps", [{
        "app": "sonarr",
        "container_name": "sonarr",
        "url": "http://172.17.0.29:8989",      # internal bridge URL
        "internal_port": 8989,
        "published_port": 8989,
        "urlbase": "",
        "db_path": "/appdata/sonarr/sonarr.db",
    }])
    srv.app.config["SONARR_APIKEY"] = "k"
    try:
        body = json.loads(client.get("/api/apps", headers={"Host": "192.168.10.37:8877"}).data)
        sonarr = next(a for a in body if a["app"] == "sonarr")
        assert sonarr["url"]          == "http://192.168.10.37:8989"
        assert sonarr["internal_url"] == "http://172.17.0.29:8989"
        assert sonarr["discovered"]   is True
    finally:
        srv.app.config["SONARR_APIKEY"] = ""
        srv._discovery_cache["apps"] = []


def test_resolve_swaps_to_internal_url_when_user_did_not_override(monkeypatch):
    """When the request body's URL matches the host-perspective display URL
    (i.e. the user submitted what we showed them), the resolver should swap
    to the discovered internal/bridge URL for the actual API call."""
    monkeypatch.setitem(srv._discovery_cache, "apps", [{
        "app": "sonarr", "container_name": "sonarr",
        "url": "http://172.17.0.29:8989",
        "internal_port": 8989, "published_port": 8989, "urlbase": "",
    }])
    srv.app.config["SONARR_APIKEY"] = "k"
    try:
        cfg, err = srv._resolve_request_cfg({
            "app": "sonarr", "url": "http://192.168.10.37:8989", "apikey": "k",
        })
        assert err is None
        # _split_url('http://172.17.0.29:8989') -> ('172.17.0.29', 8989, '')
        assert cfg["host"] == "172.17.0.29"
        assert cfg["port"] == 8989
    finally:
        srv.app.config["SONARR_APIKEY"] = ""
        srv._discovery_cache["apps"] = []


def test_job_reset_preserves_sse_subscribers():
    """_job.reset() must NOT clear SSE subscribers — that would silently sever
    connected dashboards. Regression test for the issue where the dashboard
    stayed at 'Initializing... 0%' for a run while events were emitted only to
    the (now-empty) subscriber list."""
    import queue as _queue
    srv._job.reset()
    fake_sub = _queue.Queue(maxsize=8)
    with srv._job.lock:
        srv._job.subscribers.append(fake_sub)
    original_lock = srv._job.lock

    srv._job.reset()

    assert fake_sub in srv._job.subscribers, "subscriber dropped by reset()"
    assert srv._job.lock is original_lock, "reset() must not recreate the lock"

    # Emit reaches the subscriber after reset
    srv.emit("SYS", "post-reset", "sys")
    assert not fake_sub.empty(), "emit() did not push to surviving subscriber"


# ── Backup deletion ───────────────────────────────────────────────────────────
def test_backup_delete_removes_file(client, tmp_path):
    backup_dir = tmp_path / "backups"
    f = backup_dir / "sonarr_20250101_120000.db"
    f.write_bytes(b"x" * 16)
    srv.app.config["BACKUP_DIR"] = backup_dir
    r = client.delete("/api/backups/sonarr_20250101_120000.db")
    assert r.status_code == 200
    assert not f.exists()


def test_backup_delete_missing_returns_404(client, tmp_path):
    backup_dir = tmp_path / "backups"
    srv.app.config["BACKUP_DIR"] = backup_dir
    r = client.delete("/api/backups/nope_20250101_000000.db")
    assert r.status_code == 404


def test_backup_delete_rejects_path_traversal(client, tmp_path):
    backup_dir = tmp_path / "backups"
    srv.app.config["BACKUP_DIR"] = backup_dir
    # A non-.db name and a traversal attempt both rejected at validation.
    assert client.delete("/api/backups/passwd").status_code == 400
    # Flask normalises encoded slashes, so a traversal request 404s rather than
    # escaping the dir — the key assertion is it never 200s.
    assert client.delete("/api/backups/..%2f..%2fetc%2fpasswd").status_code in (400, 404)


# ── Notifications ─────────────────────────────────────────────────────────────
def _notify_mod():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
    import notify
    return notify


def test_notify_should_notify_levels():
    n = _notify_mod()
    # off → never
    assert n.should_notify("off", "error") is False
    # error → only error
    assert n.should_notify("error", "error") is True
    assert n.should_notify("error", "warning") is False
    assert n.should_notify("error", "clean") is False
    # warning → warning + error
    assert n.should_notify("warning", "warning") is True
    assert n.should_notify("warning", "error") is True
    assert n.should_notify("warning", "clean") is False
    # always → everything
    assert n.should_notify("always", "clean") is True
    assert n.should_notify("always", "error") is True


def test_notify_config_round_trip(tmp_path):
    n = _notify_mod()
    cfg = n.NotifyConfig(tmp_path / ".notify.json")
    out = cfg.update({
        "enabled": True, "level": "warning",
        "apprise_urls": "ntfy://ntfy.sh/topic\n\n json://bad ",
        "signal": {"api_url": "http://sig:8080/", "number": "+1555", "recipients": "+1666, +1777"},
    })
    assert out["enabled"] is True
    assert out["level"] == "warning"
    assert out["apprise_urls"] == ["ntfy://ntfy.sh/topic", "json://bad"]
    assert out["signal"]["api_url"] == "http://sig:8080"   # trailing slash stripped
    assert out["signal"]["recipients"] == ["+1666", "+1777"]
    # Reload from disk
    cfg2 = n.NotifyConfig(tmp_path / ".notify.json")
    assert cfg2.get()["level"] == "warning"


def test_notify_config_rejects_bad_level(tmp_path):
    n = _notify_mod()
    cfg = n.NotifyConfig(tmp_path / ".n.json")
    with pytest.raises(ValueError):
        cfg.update({"level": "loud"})


def test_notify_signal_posts_to_rest_api(monkeypatch):
    n = _notify_mod()
    captured = {}
    class FakeResp:
        status_code = 201
        text = ""
    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResp()
    monkeypatch.setattr(n.requests, "post", fake_post)
    sent, errs = n._send_signal(
        {"api_url": "http://sig:8080", "number": "+1555", "recipients": ["+1666"]},
        "hello")
    assert sent == 1 and errs == []
    assert captured["url"] == "http://sig:8080/v2/send"
    assert captured["json"]["recipients"] == ["+1666"]
    assert captured["json"]["number"] == "+1555"


def test_api_notify_get_and_update(client):
    r = client.get("/api/notify")
    assert r.status_code == 200
    assert "level" in json.loads(r.data)
    r = client.put("/api/notify", data=json.dumps({"level": "always", "enabled": True}),
                   content_type="application/json")
    assert r.status_code == 200
    assert json.loads(r.data)["level"] == "always"


# ── Bazarr (versionless API + db subpath) ─────────────────────────────────────
def test_bazarr_registered_versionless():
    assert srv.APP_DEFAULTS["bazarr"]["api"] == ""
    assert srv.APP_DEFAULTS["bazarr"]["port"] == 6767
    assert srv.APP_DEFAULTS["bazarr"]["dbname"] == "db/bazarr.db"


def test_api_path_versionless_vs_versioned():
    assert srv._api_path("v3", "system/status") == "/api/v3/system/status"
    assert srv._api_path("v1", "system/shutdown") == "/api/v1/system/shutdown"
    assert srv._api_path("", "system/status") == "/api/system/status"


def test_get_status_versionless_for_bazarr(monkeypatch):
    seen = {}
    class FakeResp:
        status_code = 200
        def json(self): return {"data": {"bazarr_version": "1.4"}}
    def fake_get(url, headers=None, params=None, timeout=None):
        seen["url"] = url
        return FakeResp()
    monkeypatch.setattr(srv.requests, "get", fake_get)
    srv._get_status("h", 6767, "k", api="")
    assert seen["url"].endswith("/api/system/status")     # no /v3 or /v1 segment


# ── Backup compression / flagging / bulk delete ───────────────────────────────
def test_backup_compress_roundtrip(tmp_path):
    import zstandard as zstd
    src = tmp_path / "src.db"
    payload = b"SQLite format 3\x00" + b"x" * 5000
    src.write_bytes(payload)
    dest = tmp_path / "out.db.zst"
    srv._compress_file(str(src), str(dest))
    assert dest.exists() and dest.stat().st_size < src.stat().st_size
    dctx = zstd.ZstdDecompressor()
    with open(dest, "rb") as f:
        assert dctx.stream_reader(f).read() == payload


def test_flag_backup_clean_vs_repaired(tmp_path):
    clean = tmp_path / "sonarr_20250101_000000.db.zst"
    clean.write_bytes(b"x")
    out = srv._flag_backup(str(clean), {"integrity": ("ok", 0)})
    assert out.endswith("_clean.db.zst") and os.path.exists(out)

    rep = tmp_path / "radarr_20250101_000000.db"
    rep.write_bytes(b"x")
    out2 = srv._flag_backup(str(rep), {"integrity": ("issues", 3), "reindex": ("ok", 0)})
    assert out2.endswith("_repaired.db") and os.path.exists(out2)


def test_backups_list_includes_compressed_and_result(client, tmp_path):
    bdir = tmp_path / "backups"
    (bdir / "sonarr_20250101_000000_clean.db.zst").write_bytes(b"x")
    (bdir / "radarr_20250101_000000_repaired.db").write_bytes(b"y")
    srv.app.config["BACKUP_DIR"] = bdir
    body = json.loads(client.get("/api/backups").data)
    by = {b["name"]: b for b in body}
    s = by["sonarr_20250101_000000_clean.db.zst"]
    assert s["compressed"] is True and s["result"] == "clean"
    r = by["radarr_20250101_000000_repaired.db"]
    assert r["compressed"] is False and r["result"] == "repaired"


def test_delete_accepts_zst_and_bulk(client, tmp_path):
    bdir = tmp_path / "backups"
    a = bdir / "sonarr_1_clean.db.zst"; a.write_bytes(b"x")
    b = bdir / "sonarr_2_clean.db.zst"; b.write_bytes(b"x")
    srv.app.config["BACKUP_DIR"] = bdir
    # single delete of a .zst
    assert client.delete("/api/backups/sonarr_1_clean.db.zst").status_code == 200
    assert not a.exists()
    # bulk delete
    r = client.post("/api/backups/delete", data=json.dumps({"names": ["sonarr_2_clean.db.zst", "../evil"]}),
                    content_type="application/json")
    out = json.loads(r.data)
    assert out["deleted"] == ["sonarr_2_clean.db.zst"]
    assert "../evil" in out["errors"]
    assert not b.exists()


# ── Restore ───────────────────────────────────────────────────────────────────
def test_step_restore_decompresses_and_clears_sidecars(tmp_path, monkeypatch):
    srv._job.reset()
    srv.app.config["BACKUP_DIR"] = tmp_path / "bk"
    # Live DB + stale WAL/SHM sidecars that must be removed on restore.
    db = tmp_path / "sonarr.db"
    db.write_bytes(b"OLD-DB-CONTENT")
    (tmp_path / "sonarr.db-wal").write_bytes(b"stale-wal")
    (tmp_path / "sonarr.db-shm").write_bytes(b"stale-shm")
    # Compressed backup with new content.
    payload = b"SQLite format 3\x00NEW" + b"z" * 2000
    plain = tmp_path / "new.db"; plain.write_bytes(payload)
    bkp = tmp_path / "sonarr_20250101_000000_clean.db.zst"
    srv._compress_file(str(plain), str(bkp))

    ok = srv._step_restore({"app": "sonarr"}, str(db), str(bkp))
    assert ok is True
    assert db.read_bytes() == payload                 # restored content
    assert not (tmp_path / "sonarr.db-wal").exists()  # sidecars cleared
    assert not (tmp_path / "sonarr.db-shm").exists()
    # A pre-restore snapshot was written.
    assert any(p.name.endswith("_pre-restore.db.zst") for p in (tmp_path / "bk").iterdir())


def test_restore_endpoint_validation(client, tmp_path):
    bdir = tmp_path / "backups"
    srv.app.config["BACKUP_DIR"] = bdir
    # invalid name
    assert client.post("/api/backups/..%2fx/restore").status_code in (400, 404)
    # unknown app prefix
    f = bdir / "prowlerz_1.db"; f.write_bytes(b"x")
    r = client.post("/api/backups/prowlerz_1.db/restore")
    assert r.status_code in (400, 404)


def test_restore_endpoint_starts_job(client, tmp_path, monkeypatch):
    bdir = tmp_path / "backups"; ddir = tmp_path / "data"
    srv.app.config["BACKUP_DIR"] = bdir
    srv.app.config["APPDATA_DIR"] = ddir
    # backup + a target DB so _resolve_db_path finds it
    (bdir / "sonarr_1_clean.db").write_bytes(b"x")
    (ddir / "sonarr").mkdir(parents=True)
    (ddir / "sonarr" / "sonarr.db").write_bytes(b"old")
    srv.app.config["SONARR_APIKEY"] = "k"   # gives a "way to stop" path
    # Don't actually run the worker thread's docker calls.
    monkeypatch.setattr(srv, "_restore_worker", lambda cfg: None)
    try:
        r = client.post("/api/backups/sonarr_1_clean.db/restore")
        assert r.status_code == 202
        assert json.loads(r.data)["app"] == "sonarr"
    finally:
        srv.app.config["SONARR_APIKEY"] = ""


# ── Run history ─────────────────────────────────────────────────────────────────
def test_history_store_record_and_recent(tmp_path):
    from history import HistoryStore
    h = HistoryStore(tmp_path / "h.json")
    h.record({"app": "sonarr", "status": "ok", "duration_s": 10})
    h.record({"app": "radarr", "status": "warning", "duration_s": 20})
    h.record({"app": "sonarr", "status": "error", "duration_s": 5})
    assert len(h.all()) == 3
    # newest first, filtered
    son = h.recent(app="sonarr")
    assert [e["status"] for e in son] == ["error", "ok"]
    # limit honoured
    assert len(h.recent(limit=1)) == 1
    # ts auto-filled
    assert all("ts" in e for e in h.all())


def test_history_store_persists(tmp_path):
    from history import HistoryStore
    p = tmp_path / "h.json"
    HistoryStore(p).record({"app": "sonarr", "status": "ok", "duration_s": 1})
    assert HistoryStore(p).last(app="sonarr")["status"] == "ok"


def test_history_store_cap(tmp_path):
    from history import HistoryStore
    h = HistoryStore(tmp_path / "h.json", cap=5)
    for i in range(12):
        h.record({"app": "sonarr", "status": "ok", "duration_s": i})
    items = h.all()
    assert len(items) == 5
    assert items[0]["duration_s"] == 7   # oldest kept is the 8th (0-indexed 7)


def test_history_estimate_median_real_runs_only(tmp_path):
    from history import HistoryStore
    h = HistoryStore(tmp_path / "h.json")
    # real runs that should count
    for d in (10, 20, 30):
        h.record({"app": "sonarr", "status": "ok", "duration_s": d})
    # noise that must be excluded from the estimate
    h.record({"app": "sonarr", "status": "clean", "duration_s": 999})
    h.record({"app": "sonarr", "status": "error", "duration_s": 999})
    h.record({"app": "sonarr", "status": "ok", "duration_s": 999, "dry_run": True})
    est = h.estimate("sonarr")
    assert est["samples"] == 3
    assert est["seconds"] == 20   # median of 10,20,30
    # no data for another app
    assert h.estimate("radarr")["seconds"] is None


def test_history_endpoints(client, tmp_path):
    from history import HistoryStore
    h = HistoryStore(tmp_path / "h.json")
    h.record({"app": "sonarr", "status": "ok", "duration_s": 12})
    h.record({"app": "sonarr", "status": "ok", "duration_s": 18})
    srv._history = h
    r = client.get("/api/history?app=sonarr")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert len(body) == 2 and body[0]["status"] == "ok"
    # estimate endpoint
    r = client.get("/api/history/estimate?app=sonarr")
    assert r.status_code == 200
    assert json.loads(r.data)["seconds"] == 15
    # missing app -> 400
    assert client.get("/api/history/estimate").status_code == 400


def test_record_history_from_worker(tmp_path, monkeypatch):
    from history import HistoryStore
    h = HistoryStore(tmp_path / "h.json")
    monkeypatch.setattr(srv, "_history", h)
    db = tmp_path / "sonarr.db"; db.write_bytes(b"x" * 100)
    monkeypatch.setattr(srv, "_step_preflight", lambda cfg: str(db))
    monkeypatch.setattr(srv, "_step_shutdown",  lambda cfg: True)
    monkeypatch.setattr(srv, "_step_backup",    lambda cfg, p: "sonarr_1.db")
    monkeypatch.setattr(srv, "_flag_backup",    lambda b, r: b)
    monkeypatch.setattr(srv, "_step_report",    lambda *a: None)
    monkeypatch.setattr(srv, "_step_restart",   lambda cfg, r: None)
    monkeypatch.setattr(srv, "_step_repair",    lambda cfg, p: {"integrity": ("ok", "clean")})
    monkeypatch.setattr(srv, "_get_status",     lambda *a, **k: None)
    monkeypatch.setattr(srv.time, "sleep", lambda *a, **k: None)
    srv._job.reset()
    srv._repair_worker({"app": "sonarr", "ops": ["integrity"], "host": "h",
                        "port": 1, "apikey": "k"})
    last = h.last(app="sonarr")
    assert last is not None
    assert last["db_bytes"] == 100
    assert last["app"] == "sonarr"


# ── Webhook notifications ───────────────────────────────────────────────────────
def test_notify_config_round_trip_includes_webhook(tmp_path):
    import notify
    cfg = notify.NotifyConfig(tmp_path / "n.json")
    cfg.update({
        "enabled": True,
        "level":   "warning",
        "webhook_urls": "https://a.example/h\nhttps://b.example/h\n",
    })
    out = notify.NotifyConfig(tmp_path / "n.json").get()
    assert out["webhook_urls"] == ["https://a.example/h", "https://b.example/h"]


def test_send_webhook_posts_json(monkeypatch):
    import notify
    posted = []
    class FakeResp:
        def __init__(self, code): self.status_code = code; self.text = ""
    def fake_post(url, json=None, timeout=None, **kw):
        posted.append((url, json))
        return FakeResp(204 if "ok" in url else 500)
    monkeypatch.setattr(notify.requests, "post", fake_post)
    sent, errs = notify._send_webhook(
        ["https://ok.example/h", "https://bad.example/h"], {"event": "x"})
    assert sent == 1
    assert any("HTTP 500" in e for e in errs)
    assert posted[0][1] == {"event": "x"}


def test_maybe_notify_includes_structured_payload(monkeypatch, tmp_path):
    import notify
    cfg_store = notify.NotifyConfig(tmp_path / "n.json")
    cfg_store.update({"enabled": True, "level": "always",
                      "webhook_urls": "https://hook.example/x"})
    captured = {}
    def fake_dispatch(cfg, title, body, webhook_payload=None):
        captured["payload"] = webhook_payload
        return {"sent": 1, "errors": []}
    monkeypatch.setattr(notify, "dispatch", fake_dispatch)
    notify.maybe_notify(cfg_store, "sonarr",
                        {"status": "ok", "fixed": 6, "errors": 0,
                         "elapsed": "00:00:42", "backup": "sonarr_1_clean.db"},
                        scheduled=True, schedule_name="nightly")
    p = captured["payload"]
    assert p["event"] == "repair_complete"
    assert p["app"] == "sonarr"
    assert p["status"] == "ok"
    assert p["schedule_name"] == "nightly"
    assert p["scheduled"] is True


# ── Schedule store accepts the newer *arr apps ─────────────────────────────────
def test_schedule_store_accepts_new_apps(tmp_path):
    from schedules import ScheduleStore
    s = ScheduleStore(tmp_path / "sch.json")
    for app_name in ("readarr", "prowlarr", "whisparr", "bazarr"):
        sched = s.add({"name": f"{app_name} test", "app": app_name,
                       "ops": ["integrity"], "cron": "0 3 * * *"})
        assert sched["app"] == app_name


# ── Cancel mid-operation (interrupt) ────────────────────────────────────────────
def test_stop_interrupts_active_connection(client):
    class FakeConn:
        def __init__(self): self.interrupted = False
        def interrupt(self): self.interrupted = True
    fake = FakeConn()
    srv._job.running = True
    srv._job.aborted = False
    srv._job.active_conn = fake
    try:
        r = client.post("/api/repair/stop")
        assert r.status_code == 200
        body = json.loads(r.data)
        assert body["interrupted"] is True
        assert fake.interrupted is True
        assert srv._job.aborted is True
    finally:
        srv._job.running = False
        srv._job.active_conn = None


def test_stop_without_active_connection(client):
    srv._job.running = True
    srv._job.aborted = False
    srv._job.active_conn = None
    try:
        r = client.post("/api/repair/stop")
        assert r.status_code == 200
        assert json.loads(r.data)["interrupted"] is False
        assert srv._job.aborted is True
    finally:
        srv._job.running = False


def test_step_repair_marks_op_aborted_on_interrupt(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    real_connect = srv.sqlite3.connect
    c = real_connect(str(db)); c.execute("CREATE TABLE x(i)"); c.commit(); c.close()

    class Wrap:
        def __init__(self, inner): self._inner = inner
        def execute(self, sql, *a):
            if sql.strip().upper().startswith("VACUUM"):
                srv._job.aborted = True                       # simulate stop arriving
                raise srv.sqlite3.OperationalError("interrupted")
            return self._inner.execute(sql, *a)
        def __getattr__(self, n): return getattr(self._inner, n)

    monkeypatch.setattr(srv.sqlite3, "connect",
                        lambda *a, **k: Wrap(real_connect(*a, **k)))
    srv._job.reset()
    res = srv._step_repair({"ops": ["vacuum"], "app": "sonarr"}, str(db))
    assert res["vacuum"][0] == "aborted"
    assert srv._job.active_conn is None   # cleared after the run


def test_flag_backup_marks_aborted(tmp_path):
    p = tmp_path / "sonarr_20260101_000000.db"
    p.write_bytes(b"x")
    out = srv._flag_backup(str(p), {"integrity": ("ok", 0), "vacuum": ("aborted", 0)})
    assert out.endswith("_aborted.db")
    assert os.path.exists(out)


# ── Instances (multiple per app) ────────────────────────────────────────────────
def test_instance_store_add_and_id(tmp_path):
    from instances import InstanceStore
    st = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    inst = st.add({"app": "sonarr", "name": "4K", "url": "http://h:8989", "apikey": "k"})
    assert inst["app"] == "sonarr"
    assert inst["id"] == "sonarr-4k"        # app-prefixed, hyphenated
    assert "-" in inst["id"]                # never collides with bare app default
    # second with same name gets a unique id
    inst2 = st.add({"app": "sonarr", "name": "4K", "url": "http://h:8990"})
    assert inst2["id"] != inst["id"]


def test_instance_store_validation(tmp_path):
    from instances import InstanceStore
    st = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    for bad in ({"app": "nope", "name": "x", "url": "u"},
                {"app": "sonarr", "name": "", "url": "u"},
                {"app": "sonarr", "name": "x", "url": ""}):
        with pytest.raises(ValueError):
            st.add(bad)


def test_instance_store_app_for(tmp_path):
    from instances import InstanceStore
    st = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    st.add({"app": "sonarr", "name": "anime", "url": "http://h:8989"})
    assert st.app_for("sonarr") == "sonarr"          # bare default
    assert st.app_for("sonarr-anime") == "sonarr"    # stored instance id
    assert st.app_for("radarr-foo") == "radarr"      # unknown id, hyphen fallback
    assert st.app_for("bogus") is None


def test_instance_store_update_delete(tmp_path):
    from instances import InstanceStore
    st = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    inst = st.add({"app": "radarr", "name": "uhd", "url": "http://h:7878"})
    up = st.update(inst["id"], {"url": "http://h:9999", "app": "sonarr"})
    assert up["url"] == "http://h:9999"
    assert up["app"] == "radarr"            # app type is immutable on update
    assert st.delete(inst["id"]) is True
    assert st.get(inst["id"]) is None


def test_instances_endpoints(client, tmp_path):
    from instances import InstanceStore
    srv._instances = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    # add
    r = client.post("/api/instances", data=json.dumps(
        {"app": "sonarr", "name": "4K", "url": "http://h:8989", "apikey": "k"}),
        content_type="application/json")
    assert r.status_code == 201
    iid = json.loads(r.data)["id"]
    # list includes the extra (default synthesis is empty with no env config)
    r = client.get("/api/instances")
    listed = json.loads(r.data)
    assert any(x["id"] == iid and x["default"] is False for x in listed)
    # update + delete
    assert client.put(f"/api/instances/{iid}", data=json.dumps({"name": "UHD"}),
                      content_type="application/json").status_code == 200
    assert client.delete(f"/api/instances/{iid}").status_code == 200
    assert client.delete(f"/api/instances/{iid}").status_code == 404


def test_apply_instance_overlay(tmp_path):
    from instances import InstanceStore
    srv._instances = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    inst = srv._instances.add({"app": "sonarr", "name": "4K",
                               "url": "http://h:8989", "apikey": "K",
                               "container_name": "sonarr4k"})
    cfg = {"instance_id": inst["id"], "ops": ["integrity"]}
    err = srv._apply_instance(cfg)
    assert err is None
    assert cfg["app"] == "sonarr"
    assert cfg["label"] == inst["id"]
    assert cfg["apikey"] == "K" and cfg["container_name"] == "sonarr4k"
    # default app (no instance_id) → label is the app name
    cfg2 = {"app": "radarr"}
    assert srv._apply_instance(cfg2) is None
    assert cfg2["label"] == "radarr"
    # unknown instance id → error
    assert srv._apply_instance({"instance_id": "sonarr-ghost"}) is not None


def test_backup_filename_uses_label(monkeypatch, tmp_path):
    bdir = tmp_path / "backups"; bdir.mkdir()
    srv.app.config["BACKUP_DIR"] = bdir
    srv.app.config["BACKUP_COMPRESS"] = False
    db = tmp_path / "src.db"; db.write_bytes(b"data")
    srv._job.reset()
    out = srv._step_backup({"app": "sonarr", "label": "sonarr-4k"}, str(db))
    assert out is not None
    assert os.path.basename(out).startswith("sonarr-4k_")


# ── Instance-scoped history & trends ────────────────────────────────────────────
def test_history_recent_by_instance(tmp_path):
    from history import HistoryStore
    h = HistoryStore(tmp_path / "h.json")
    h.record({"app": "sonarr", "instance": "sonarr",    "status": "ok", "duration_s": 10})
    h.record({"app": "sonarr", "instance": "sonarr-4k", "status": "ok", "duration_s": 20})
    h.record({"app": "sonarr", "instance": "sonarr-4k", "status": "ok", "duration_s": 30})
    # Per-instance — only the matching id
    four_k = h.recent(instance="sonarr-4k")
    assert [e["duration_s"] for e in four_k] == [30, 20]
    # Per-app — still returns every sonarr record
    assert len(h.recent(app="sonarr")) == 3


def test_history_recent_legacy_records_map_to_default(tmp_path):
    """Pre-multi-instance records have no `instance` field. Filtering by the
    bare app name as an instance must still surface them (legacy fallback)."""
    from history import HistoryStore
    h = HistoryStore(tmp_path / "h.json")
    h.record({"app": "sonarr", "status": "ok", "duration_s": 5})   # legacy
    h.record({"app": "sonarr", "instance": "sonarr-4k", "status": "ok", "duration_s": 6})
    legacy = h.recent(instance="sonarr")
    assert len(legacy) == 1 and legacy[0]["duration_s"] == 5


def test_history_estimate_per_instance(tmp_path):
    from history import HistoryStore
    h = HistoryStore(tmp_path / "h.json")
    for d in (10, 20, 30):
        h.record({"app": "sonarr", "instance": "sonarr-4k", "status": "ok", "duration_s": d})
    h.record({"app": "sonarr", "instance": "sonarr-anime", "status": "ok", "duration_s": 999})
    est = h.estimate(instance="sonarr-4k")
    assert est["samples"] == 3 and est["seconds"] == 20
    assert est["instance"] == "sonarr-4k"
    # No instance filter → app-wide median (still includes the 999)
    app_est = h.estimate(app="sonarr")
    assert app_est["samples"] == 4


def test_history_endpoints_instance_filter(client, tmp_path):
    from history import HistoryStore
    h = HistoryStore(tmp_path / "h.json")
    h.record({"app": "sonarr", "instance": "sonarr",    "status": "ok", "duration_s": 10})
    h.record({"app": "sonarr", "instance": "sonarr-4k", "status": "ok", "duration_s": 22})
    srv._history = h
    # Instance-scoped /api/history
    body = json.loads(client.get("/api/history?instance=sonarr-4k").data)
    assert len(body) == 1 and body[0]["duration_s"] == 22
    # Instance-scoped /api/history/estimate
    est = json.loads(client.get("/api/history/estimate?instance=sonarr-4k").data)
    assert est["seconds"] == 22 and est["instance"] == "sonarr-4k"
    # Neither app nor instance → 400
    assert client.get("/api/history/estimate").status_code == 400


# ── UI-saved credentials (overrides) ────────────────────────────────────────────
def test_instance_overrides_round_trip(tmp_path):
    from instances import InstanceStore
    st = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    assert st.get_override("sonarr") == {}
    st.set_override("sonarr", {"apikey": "k", "url": "http://h:8989", "container_name": ""})
    ov = st.get_override("sonarr")
    assert ov["apikey"] == "k" and ov["url"] == "http://h:8989"
    assert "container_name" not in ov   # blanks stripped
    # persists across reloads
    st2 = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    assert st2.get_override("sonarr")["apikey"] == "k"
    # clearing removes it
    st2.set_override("sonarr", {"apikey": ""})
    assert st2.get_override("sonarr") == {}


def test_credentials_endpoint(client, tmp_path):
    from instances import InstanceStore
    srv._instances = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    r = client.put("/api/instances/sonarr/credentials",
                   data=json.dumps({"apikey": "ui-key", "url": "http://h:8989"}),
                   content_type="application/json")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert body["override"]["apikey"] == "ui-key"
    # synthesized default now reflects the override
    defaults = srv._synthesized_defaults()
    son = next(d for d in defaults if d["id"] == "sonarr")
    assert son["apikey"] == "ui-key"
    assert son["overridden"] is True
    # unknown instance → 404
    assert client.put("/api/instances/bogus/credentials",
                      data=json.dumps({"apikey": "x"}),
                      content_type="application/json").status_code == 404


def test_apply_instance_uses_override_when_no_apikey(tmp_path):
    from instances import InstanceStore
    srv._instances = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    srv._instances.set_override("sonarr", {"apikey": "ui-key"})
    cfg = {"instance_id": "sonarr"}
    assert srv._apply_instance(cfg) is None
    assert cfg["apikey"] == "ui-key"     # override surfaced for scheduled runs
    # explicit body still wins
    cfg2 = {"instance_id": "sonarr", "apikey": "body-key"}
    srv._apply_instance(cfg2)
    assert cfg2["apikey"] == "body-key"


def test_apply_instance_default_uses_override_when_id_empty(tmp_path):
    """Regression: scheduled runs targeting the env/discovery default carry
    instance_id="" and must still pick up the UI-saved credentials override
    (previously skipped, causing "apikey is required" on Run now)."""
    from instances import InstanceStore
    srv._instances = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    srv._instances.set_override("prowlarr", {"apikey": "ui-key"})
    # This is what schedules._fire produces for a default-instance schedule:
    cfg = {"app": "prowlarr", "instance_id": "", "ops": ["integrity"]}
    assert srv._apply_instance(cfg) is None
    assert cfg["apikey"] == "ui-key"
    # Sanity: label still reflects the default (= app name).
    assert cfg["label"] == "prowlarr"


# ── Backup retention setting (UI-adjustable) ───────────────────────────────────
def test_settings_store_round_trip(tmp_path):
    from settings import SettingsStore
    s = SettingsStore(tmp_path / "set.json")
    assert s.get() == {}
    s.update({"max_backup_age_days": 30})
    assert s.get()["max_backup_age_days"] == 30
    # persists across reloads
    s2 = SettingsStore(tmp_path / "set.json")
    assert s2.get()["max_backup_age_days"] == 30
    # env fallback when nothing saved
    s3 = SettingsStore(tmp_path / "fresh.json")
    assert s3.max_backup_age_days(env_default=7) == 7


def test_settings_validates_range(tmp_path):
    from settings import SettingsStore
    s = SettingsStore(tmp_path / "set.json")
    s.update({"max_backup_age_days": 0})        # 0 = keep forever, allowed
    s.update({"max_backup_age_days": 365})      # upper bound
    for bad in (-1, 366, "abc"):
        with pytest.raises(ValueError):
            s.update({"max_backup_age_days": bad})


def test_settings_endpoints(client, tmp_path):
    from settings import SettingsStore
    srv._settings = SettingsStore(tmp_path / "set.json")
    # GET: shape includes effective value + source
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert body["max_backup_age_days_source"] == "env"
    assert body["max_backup_age_days_min"] == 0 and body["max_backup_age_days_max"] == 365
    # PUT: save 90, GET reports saved value + source
    r = client.put("/api/settings",
                   data=json.dumps({"max_backup_age_days": 90}),
                   content_type="application/json")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert body["max_backup_age_days"] == 90
    assert body["max_backup_age_days_source"] == "saved"
    # PUT: invalid → 400
    r = client.put("/api/settings",
                   data=json.dumps({"max_backup_age_days": 9999}),
                   content_type="application/json")
    assert r.status_code == 400


def test_step_backup_uses_saved_retention(tmp_path, monkeypatch):
    from settings import SettingsStore
    bdir = tmp_path / "backups"; bdir.mkdir()
    srv.app.config["BACKUP_DIR"] = bdir
    srv.app.config["BACKUP_COMPRESS"] = False
    srv.app.config["MAX_BACKUP_AGE_DAYS"] = 7   # env default
    srv._settings = SettingsStore(tmp_path / "set.json")
    srv._settings.update({"max_backup_age_days": 30})   # saved override

    # Old backup that env-default-7 would prune but saved-30 would keep.
    old = bdir / "sonarr_20260501_000000.db"
    old.write_bytes(b"x")
    old_mtime = srv.time.time() - 14 * 86400
    os.utime(old, (old_mtime, old_mtime))

    db = tmp_path / "src.db"; db.write_bytes(b"data")
    srv._job.reset()
    srv._step_backup({"app": "sonarr", "label": "sonarr"}, str(db))
    assert old.exists()   # saved 30-day retention kept the 14-day-old file

    # Flip saved back to 7 → prune should now remove it.
    srv._settings.update({"max_backup_age_days": 7})
    srv._job.reset()
    srv._step_backup({"app": "sonarr", "label": "sonarr"}, str(db))
    assert not old.exists()


# ── Per-instance backup retention ───────────────────────────────────────────────
def test_per_instance_retention_store(tmp_path):
    from settings import SettingsStore
    s = SettingsStore(tmp_path / "set.json")
    # No overrides → falls back to global, then env default
    assert s.max_backup_age_days(7) == 7
    s.update({"max_backup_age_days": 30})
    assert s.max_backup_age_days(7) == 30
    assert s.max_backup_age_days(7, instance="sonarr") == 30   # inherits global
    # Per-instance override wins
    s.set_instance_retention("sonarr", 14)
    assert s.max_backup_age_days(7, instance="sonarr") == 14
    assert s.max_backup_age_days(7, instance="radarr") == 30   # still inherits
    # Clearing the override falls back to global again
    s.set_instance_retention("sonarr", None)
    assert s.max_backup_age_days(7, instance="sonarr") == 30
    assert s.instance_retention_all() == {}                    # cleaned up

    # Validation: out-of-range rejected
    for bad in (-1, 366, "abc"):
        with pytest.raises(ValueError):
            s.set_instance_retention("sonarr", bad)
    # Forever is allowed
    s.set_instance_retention("sonarr-4k", 0)
    assert s.max_backup_age_days(7, instance="sonarr-4k") == 0


def test_per_instance_retention_persists(tmp_path):
    from settings import SettingsStore
    path = tmp_path / "set.json"
    SettingsStore(path).set_instance_retention("sonarr-4k", 14)
    assert SettingsStore(path).max_backup_age_days(7, instance="sonarr-4k") == 14


def test_per_instance_retention_endpoint(client, tmp_path):
    from settings import SettingsStore
    from instances import InstanceStore
    srv._settings = SettingsStore(tmp_path / "set.json")
    srv._instances = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    inst = srv._instances.add({"app": "sonarr", "name": "4K", "url": "http://h:8989"})
    # Set per-instance retention
    r = client.put(f"/api/instances/{inst['id']}/retention",
                   data=json.dumps({"max_backup_age_days": 14}),
                   content_type="application/json")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert body["retention_days"] == 14
    assert body["retention_effective_days"] == 14
    # Default instance (id == app name) is allowed too
    r = client.put("/api/instances/sonarr/retention",
                   data=json.dumps({"max_backup_age_days": 90}),
                   content_type="application/json")
    assert r.status_code == 200 and json.loads(r.data)["retention_days"] == 90
    # Clearing with null restores inheritance
    r = client.put(f"/api/instances/{inst['id']}/retention",
                   data=json.dumps({"max_backup_age_days": None}),
                   content_type="application/json")
    assert r.status_code == 200 and json.loads(r.data)["retention_days"] is None
    # Out of range → 400
    r = client.put(f"/api/instances/{inst['id']}/retention",
                   data=json.dumps({"max_backup_age_days": 9999}),
                   content_type="application/json")
    assert r.status_code == 400
    # Unknown instance → 404
    r = client.put("/api/instances/bogus/retention",
                   data=json.dumps({"max_backup_age_days": 30}),
                   content_type="application/json")
    assert r.status_code == 404


def test_instances_listing_includes_retention(client, tmp_path):
    from settings import SettingsStore
    from instances import InstanceStore
    srv._settings = SettingsStore(tmp_path / "set.json")
    srv._instances = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    srv._instances.add({"app": "sonarr", "name": "4K", "url": "http://h:8989"})
    srv._settings.set_instance_retention("sonarr-4k", 14)
    srv.app.config["MAX_BACKUP_AGE_DAYS"] = 7
    items = json.loads(client.get("/api/instances").data)
    by_id = {x["id"]: x for x in items}
    s4k = by_id["sonarr-4k"]
    assert s4k["retention_days"] == 14
    assert s4k["retention_effective_days"] == 14


def test_step_backup_prunes_per_instance(tmp_path, monkeypatch):
    """A 20-day-old sonarr-4k backup should be kept when sonarr-4k has its
    own 30-day retention even though the global is 7. A sibling Sonarr
    backup keeps the inherited 7-day cutoff."""
    from settings import SettingsStore
    bdir = tmp_path / "backups"; bdir.mkdir()
    srv.app.config["BACKUP_DIR"] = bdir
    srv.app.config["BACKUP_COMPRESS"] = False
    srv.app.config["MAX_BACKUP_AGE_DAYS"] = 7
    srv._settings = SettingsStore(tmp_path / "set.json")
    srv._settings.set_instance_retention("sonarr-4k", 30)

    # 20-day-old sonarr-4k backup (under 30, over global 7) — should be kept
    s4k_old = bdir / "sonarr-4k_20260601_000000.db"
    s4k_old.write_bytes(b"x")
    old = srv.time.time() - 20 * 86400
    os.utime(s4k_old, (old, old))

    # 20-day-old default sonarr backup (inherits global 7) — should be pruned
    son_old = bdir / "sonarr_20260601_000000.db"
    son_old.write_bytes(b"x")
    os.utime(son_old, (old, old))

    db = tmp_path / "src.db"; db.write_bytes(b"data")
    srv._job.reset()
    srv._step_backup({"app": "sonarr", "label": "sonarr-4k"}, str(db))
    assert s4k_old.exists()         # 4K retention kept it
    assert son_old.exists()         # sibling untouched by the 4K prune

    srv._job.reset()
    srv._step_backup({"app": "sonarr", "label": "sonarr"}, str(db))
    assert not son_old.exists()     # global 7-day pruned it
    assert s4k_old.exists()         # 4K still safe


def test_history_default_instance_excludes_named_extras(tmp_path):
    """The 'last run' pill on the default app tab must NOT show runs that
    actually came from a named extra of the same app.

    Frontend (#52) calls /api/history?instance=sonarr for the default tab and
    ?instance=sonarr-4k for the extra. Each must return only its own runs."""
    from history import HistoryStore
    h = HistoryStore(tmp_path / "h.json")
    # Default instance runs (id == app name).
    h.record({"app": "sonarr", "instance": "sonarr",    "status": "ok",
              "duration_s": 10})
    # Named-extra runs.
    h.record({"app": "sonarr", "instance": "sonarr-4k", "status": "warning",
              "duration_s": 20})
    # A legacy record with no `instance` field — should fall under the default.
    h.record({"app": "sonarr", "status": "ok", "duration_s": 30})

    default = h.recent(instance="sonarr")
    extras  = h.recent(instance="sonarr-4k")
    assert {e["status"] for e in default} == {"ok"}      # default + legacy
    assert {e["duration_s"] for e in default} == {10, 30}
    assert [e["status"] for e in extras] == ["warning"]
    assert [e["duration_s"] for e in extras] == [20]

    # And different apps stay isolated too.
    h.record({"app": "radarr", "instance": "radarr", "status": "error",
              "duration_s": 5})
    assert h.recent(instance="radarr")[0]["status"] == "error"
    assert all(e["app"] == "sonarr" for e in h.recent(instance="sonarr"))


# ── Auth: SECRET_KEY defaults + comparison ──────────────────────────────────────
DEFAULT_SECRET = "change-me-in-production"


def test_shipped_defaults_match_the_insecure_sentinel():
    """docker-compose.yml and .env.example must default SECRET_KEY to the
    EXACT sentinel server.py checks for. If they drift (e.g. someone sets
    compose to 'change-me' while the app checks 'change-me-in-production'),
    an out-of-the-box `docker compose up` with no .env silently authenticates
    every request against a publicly-known password instead of falling back
    to the loud unauthenticated-with-a-warning state."""
    repo_root = os.path.join(os.path.dirname(__file__), "..")

    compose = open(os.path.join(repo_root, "docker-compose.yml")).read()
    m = re.search(r"SECRET_KEY:\s*\$\{SECRET_KEY:-([^}]+)\}", compose)
    assert m, "docker-compose.yml must set a SECRET_KEY default"
    assert m.group(1) == DEFAULT_SECRET

    env_example = open(os.path.join(repo_root, ".env.example")).read()
    m = re.search(r"^SECRET_KEY=(.*)$", env_example, re.MULTILINE)
    assert m, ".env.example must set SECRET_KEY"
    assert m.group(1).strip() == DEFAULT_SECRET

    # And both places server.py hardcodes the sentinel (the Config class
    # default and the require_api_key check) must actually agree with it —
    # if they ever drift apart, the default becomes indistinguishable from a
    # deliberately-chosen (insecure) key and the warning stops firing.
    server_src = open(os.path.join(repo_root, "app", "server.py")).read()
    assert f'"{DEFAULT_SECRET}"' in server_src
    assert server_src.count(f'"{DEFAULT_SECRET}"') >= 2


def test_default_secret_key_disables_enforcement_with_warning(client, caplog):
    """When SECRET_KEY is still the sentinel, every request is let through
    (no key needed) but a warning is logged so it's visible in `docker logs`."""
    srv.app.config["SECRET_KEY"] = DEFAULT_SECRET
    with caplog.at_level("WARNING", logger="starr-repair"):
        r = client.get("/api/backups")
    assert r.status_code == 200
    assert any("SECRET_KEY is still the default" in rec.message
               for rec in caplog.records)


def test_real_secret_key_requires_matching_api_key(client):
    srv.app.config["SECRET_KEY"] = "a-real-secret"
    try:
        # No key at all -> 401
        assert client.get("/api/backups").status_code == 401
        # Wrong key -> 401
        assert client.get("/api/backups",
                          headers={"X-Api-Key": "wrong"}).status_code == 401
        # Wrong-length key (exercises the hmac.compare_digest path with
        # mismatched lengths rather than just mismatched content) -> 401
        assert client.get("/api/backups",
                          headers={"X-Api-Key": "x"}).status_code == 401
        # Correct key -> 200
        assert client.get("/api/backups",
                          headers={"X-Api-Key": "a-real-secret"}).status_code == 200
        # Correct key via query param (used by the SSE stream) -> 200
        assert client.get("/api/backups?api_key=a-real-secret").status_code == 200
    finally:
        srv.app.config["SECRET_KEY"] = DEFAULT_SECRET


def test_missing_api_key_does_not_crash_comparison(client):
    """hmac.compare_digest requires two str/bytes args of the same type —
    make sure the None-coalescing (`or ""`) actually prevents a 500 when no
    key is supplied at all."""
    srv.app.config["SECRET_KEY"] = "a-real-secret"
    try:
        r = client.get("/api/backups")
        assert r.status_code == 401
    finally:
        srv.app.config["SECRET_KEY"] = DEFAULT_SECRET


# ── Custom DB name / db_path override (issue #62) ───────────────────────────────
def test_resolve_db_override(monkeypatch, tmp_path):
    srv.app.config["APPDATA_DIR"] = tmp_path / "data"
    # No discovery → bare filename resolves against APPDATA_DIR/<app>/
    monkeypatch.setattr(srv, "_discovered_for", lambda a: {})
    got = srv._resolve_db_override("whisparr", "whisparr2.db")
    assert got == str(tmp_path / "data" / "whisparr" / "whisparr2.db")
    # Absolute path is used verbatim
    assert srv._resolve_db_override("whisparr", "/x/y/whisparr2.db") == "/x/y/whisparr2.db"
    # Empty → ""
    assert srv._resolve_db_override("whisparr", "") == ""
    assert srv._resolve_db_override("whisparr", None) == ""
    # With discovery → bare filename resolves next to the discovered DB
    monkeypatch.setattr(srv, "_discovered_for",
                        lambda a: {"db_path": "/appdata/whisparr/whisparr.db"})
    assert srv._resolve_db_override("whisparr", "whisparr2.db") == \
        "/appdata/whisparr/whisparr2.db"


def test_preflight_uses_db_path_override(monkeypatch, tmp_path):
    """A bare-filename db_path override should let preflight find a
    non-standard DB (e.g. whisparr2.db) under APPDATA_DIR/<app>/."""
    srv.app.config["APPDATA_DIR"] = tmp_path / "data"
    dbdir = tmp_path / "data" / "whisparr"
    dbdir.mkdir(parents=True)
    (dbdir / "whisparr2.db").write_bytes(b"x" * 2048)   # exists; whisparr.db does NOT
    monkeypatch.setattr(srv, "_get_status", lambda *a, **k: {"version": "2.0", "osName": "linux"})
    monkeypatch.setattr(srv, "_discovered_for", lambda a: {})
    srv._job.reset()
    cfg = {"app": "whisparr", "host": "h", "port": 6969, "apikey": "k",
           "urlbase": "", "api": "v3", "db_path": "whisparr2.db"}
    resolved = srv._step_preflight(cfg)
    assert resolved == str(dbdir / "whisparr2.db")
    # Without the override, the default whisparr.db doesn't exist → preflight fails
    srv._job.reset()
    cfg2 = dict(cfg); cfg2.pop("db_path")
    assert srv._step_preflight(cfg2) is None


def test_instances_expose_db_path_override(client, tmp_path):
    from instances import InstanceStore
    from settings import SettingsStore
    srv._instances = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    srv._settings = SettingsStore(tmp_path / "set.json")
    # Save a db_path override on the default whisparr instance via credentials.
    srv.app.config["WHISPARR_APIKEY"] = "wk"
    try:
        r = client.put("/api/instances/whisparr/credentials",
                       data=json.dumps({"db_path": "whisparr2.db"}),
                       content_type="application/json")
        assert r.status_code == 200
        items = json.loads(client.get("/api/instances").data)
        w = next(i for i in items if i["id"] == "whisparr")
        assert w["db_path_override"] == "whisparr2.db"
    finally:
        srv.app.config["WHISPARR_APIKEY"] = ""


def test_apply_instance_carries_db_path_override(tmp_path):
    from instances import InstanceStore
    srv._instances = InstanceStore(tmp_path / "i.json", srv.APP_DEFAULTS.keys())
    srv._instances.set_override("whisparr", {"db_path": "whisparr2.db"})
    cfg = {"instance_id": "whisparr", "ops": ["integrity"]}
    assert srv._apply_instance(cfg) is None
    assert cfg["db_path"] == "whisparr2.db"     # override reaches the repair cfg
    # Explicit request-body db_path wins over the saved override
    cfg2 = {"instance_id": "whisparr", "db_path": "custom.db"}
    srv._apply_instance(cfg2)
    assert cfg2["db_path"] == "custom.db"


# ── Discovery re-scan on repair (stale container IP fix, issue-driven) ──────────
def _disc(app, ip):
    return {
        "docker_available": True,
        "apps": [{"app": app, "url": f"http://{ip}:7878", "published_port": 7878,
                  "container_name": app, "db_path": f"/appdata/{app}/{app}.db"}],
        "appdata": {}, "warnings": [],
    }


def test_repair_rescans_discovery_for_current_ip(monkeypatch):
    """A repair must resolve the container's CURRENT bridge IP, not a stale
    cached one — the fix for 'Cannot reach radarr at <old ip>' after a
    container is recreated."""
    srv._discovery_cache = _disc("radarr", "172.17.0.21")          # stale cache
    monkeypatch.setattr(srv._discovery, "discover",
                        lambda: _disc("radarr", "172.17.0.99"))     # fresh scan
    # Host-perspective URL whose published port matches → resolver swaps to the
    # discovered bridge URL, which must be the freshly-scanned IP.
    cfg = {"app": "radarr", "url": "http://192.168.10.37:7878", "apikey": "k"}
    out, err = srv._resolve_request_cfg(cfg)
    assert err is None
    assert out["host"] == "172.17.0.99"     # re-scanned, not the stale .21


def test_no_rescan_when_docker_unavailable(monkeypatch):
    srv._discovery_cache = {"docker_available": False, "apps": [],
                            "appdata": {}, "warnings": []}
    called = []
    monkeypatch.setattr(srv._discovery, "discover",
                        lambda: called.append(1) or {"docker_available": True, "apps": []})
    cfg = {"app": "sonarr", "url": "http://sonarr:8989", "apikey": "k"}
    out, err = srv._resolve_request_cfg(cfg)
    assert err is None
    assert called == []                     # no pointless scan without Docker
    assert out["host"] == "sonarr"          # used the provided URL


def test_refresh_discovery_keeps_cache_on_failure(monkeypatch):
    good = _disc("sonarr", "172.17.0.5")
    srv._discovery_cache = good
    def boom():
        raise RuntimeError("docker hiccup")
    monkeypatch.setattr(srv._discovery, "discover", boom)
    srv._refresh_discovery()
    assert srv._discovery_cache == good     # transient failure didn't wipe it


# ── Robust docker stop (read-timeout tolerance) ────────────────────────────────
def test_is_timeout_err():
    import socket
    assert srv._is_timeout_err(Exception("UnixHTTPConnectionPool(...): Read timed out. (read timeout=60)"))
    assert srv._is_timeout_err(socket.timeout("timed out"))
    assert srv._is_timeout_err(TimeoutError())
    assert not srv._is_timeout_err(Exception("permission denied while connecting"))
    assert not srv._is_timeout_err(ValueError("nope"))


def test_docker_stop_timeout_but_container_goes_down(monkeypatch):
    """The reported Sportarr case: docker stop read-times-out, but the daemon
    still stops the container — shutdown must SUCCEED, not fail."""
    srv._job.reset(); srv._job.start_time = 0
    monkeypatch.setattr(srv.time, "sleep", lambda *a, **k: None)

    class FakeContainer:
        def stop(self, timeout=30):
            raise Exception("UnixHTTPConnectionPool(host='localhost', port=None): "
                            "Read timed out. (read timeout=60)")
    monkeypatch.setattr(srv, "_docker_container", lambda n: ("c", FakeContainer()))
    monkeypatch.setattr(srv, "_get_status", lambda *a, **k: None)   # container went down

    cfg = {"app": "sportarr", "host": "h", "port": 1, "apikey": "k",
           "container_name": "Sportarr"}
    assert srv._step_shutdown(cfg) is True
    assert cfg.get("_docker_managed") == "Sportarr"


def test_docker_stop_timeout_and_container_stays_up(monkeypatch):
    """If the stop times out AND the app is still responding after the wait,
    shutdown must fail (never repair a DB the app might have open)."""
    srv._job.reset(); srv._job.start_time = 0
    monkeypatch.setattr(srv.time, "sleep", lambda *a, **k: None)
    clock = {"v": 0.0}
    def fake_time():
        clock["v"] += 5
        return clock["v"]
    monkeypatch.setattr(srv.time, "time", fake_time)

    class FakeContainer:
        def stop(self, timeout=30):
            raise Exception("Read timed out. (read timeout=60)")
    monkeypatch.setattr(srv, "_docker_container", lambda n: ("c", FakeContainer()))
    monkeypatch.setattr(srv, "_get_status", lambda *a, **k: {"version": "x"})  # still up

    cfg = {"app": "sportarr", "host": "h", "port": 1, "apikey": "k",
           "container_name": "Sportarr"}
    assert srv._step_shutdown(cfg) is False


def test_docker_stop_non_timeout_error_fails_fast(monkeypatch):
    """A genuine (non-timeout) stop error still fails immediately, without the
    long offline poll."""
    srv._job.reset(); srv._job.start_time = 0
    monkeypatch.setattr(srv.time, "sleep", lambda *a, **k: None)

    class FakeContainer:
        def stop(self, timeout=30):
            raise Exception("permission denied while trying to connect to the Docker daemon")
    monkeypatch.setattr(srv, "_docker_container", lambda n: ("c", FakeContainer()))
    polled = []
    monkeypatch.setattr(srv, "_get_status", lambda *a, **k: polled.append(1))

    cfg = {"app": "sonarr", "host": "h", "port": 1, "apikey": "k",
           "container_name": "sonarr"}
    assert srv._step_shutdown(cfg) is False
    assert polled == []   # no offline-poll on a hard failure
