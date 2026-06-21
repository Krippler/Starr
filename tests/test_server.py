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
        store.add({"app": "prowlarr", "ops": ["integrity"], "cron": "0 3 * * *"})
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
