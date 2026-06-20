#!/usr/bin/env python3
"""
Starr DB Repair – Flask web server
===================================
Serves the dashboard UI and exposes REST + SSE endpoints.

Endpoints
---------
  GET  /                   → dashboard HTML
  GET  /api/apps           → list configured apps
  POST /api/repair/start   → start a repair job (JSON body)
  POST /api/repair/stop    → abort the running job
  GET  /api/repair/status  → current job state (JSON)
  GET  /api/repair/stream  → Server-Sent Events live log
  GET  /healthz            → liveness probe (Docker/k8s)
  GET  /readyz             → readiness probe
"""

import json
import logging
import os
import queue
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from flask_cors import CORS

# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("starr-repair")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration (env-vars with sane defaults)
# ---------------------------------------------------------------------------
class Config:
    SECRET_KEY          = os.environ.get("SECRET_KEY", "change-me-in-production")
    MAX_BACKUP_AGE_DAYS = int(os.environ.get("MAX_BACKUP_AGE_DAYS", "7"))
    BACKUP_DIR          = Path(os.environ.get("BACKUP_DIR", "/backups"))
    # Host's appdata root, mounted in once; per-app paths are derived from
    # Docker introspection at runtime.
    APPDATA_DIR         = Path(os.environ.get("APPDATA_DIR", "/appdata"))
    LOG_LEVEL           = os.environ.get("LOG_LEVEL", "INFO")
    CORS_ORIGINS        = os.environ.get("CORS_ORIGINS", "http://localhost:8877")
    # Only secrets and an optional URL override per app. Everything else
    # (container name, host port, /config host path) is auto-discovered via
    # the Docker socket — see app/discovery.py.
    SONARR_APIKEY       = os.environ.get("SONARR_APIKEY", "")
    SONARR_URL          = os.environ.get("SONARR_URL", "")
    RADARR_APIKEY       = os.environ.get("RADARR_APIKEY", "")
    RADARR_URL          = os.environ.get("RADARR_URL", "")
    LIDARR_APIKEY       = os.environ.get("LIDARR_APIKEY", "")
    LIDARR_URL          = os.environ.get("LIDARR_URL", "")
    SPORTARR_APIKEY     = os.environ.get("SPORTARR_APIKEY", "")
    SPORTARR_URL        = os.environ.get("SPORTARR_URL", "")

app.config.from_object(Config)
logging.getLogger().setLevel(app.config["LOG_LEVEL"])

# Restrict CORS to configured origins only
CORS(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}})

APP_DEFAULTS = {
    # api: Sonarr/Radarr (and the Sonarr-fork Sportarr) speak /api/v3;
    # Lidarr (like Readarr) speaks /api/v1.
    "sonarr":   {"port": 8989, "dbname": "sonarr.db",   "api": "v3"},
    "radarr":   {"port": 7878, "dbname": "radarr.db",   "api": "v3"},
    "lidarr":   {"port": 8686, "dbname": "lidarr.db",   "api": "v1"},
    "sportarr": {"port": 1867, "dbname": "sportarr.db", "api": "v3"},
}

# After the app first reads offline, re-poll this many times at this interval
# to make sure it STAYS offline (a Docker restart policy can bring it back).
# ~5 × 3s = ~15s, enough to catch a typical container restart.
SHUTDOWN_STABILITY_CHECKS   = int(os.environ.get("SHUTDOWN_STABILITY_CHECKS", "5"))
SHUTDOWN_STABILITY_INTERVAL = int(os.environ.get("SHUTDOWN_STABILITY_INTERVAL", "3"))

ALL_OPS = ["integrity", "foreign_keys", "wal_checkpoint", "vacuum", "reindex", "analyze"]
OP_DESC = {
    "integrity":      "PRAGMA integrity_check – full page-level scan",
    "foreign_keys":   "PRAGMA foreign_key_check – find & remove orphaned FK rows",
    "wal_checkpoint": "PRAGMA wal_checkpoint(TRUNCATE) – flush WAL to main file",
    "vacuum":         "VACUUM – defragment and reclaim free pages",
    "reindex":        "REINDEX – drop and rebuild every index",
    "analyze":        "ANALYZE – refresh query-planner statistics",
}

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def require_api_key(f):
    """Protect endpoints with the SECRET_KEY.

    Accepts the key via:
      - X-Api-Key request header  (fetch / XHR)
      - ?api_key= query parameter  (EventSource / SSE, which can't set headers)
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        secret = app.config["SECRET_KEY"]
        # If still using the default key, skip enforcement so out-of-box
        # experience works, but log a warning on every request.
        if secret == "change-me-in-production":
            log.warning("SECRET_KEY is still the default — set a real value in .env!")
            return f(*args, **kwargs)
        provided = (
            request.headers.get("X-Api-Key")
            or request.args.get("api_key")
        )
        if provided != secret:
            return jsonify({"error": "Unauthorized — invalid or missing API key"}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------
class JobState:
    def __init__(self):
        # The SSE subscriber list and the lock that guards it survive across
        # jobs — they're per-process state owned by the SSE generator(s) and
        # the emit() helper, not by an individual repair run. Initialise them
        # here so reset() (which clears job-specific state) never disturbs
        # connected clients.
        self.subscribers: list[queue.Queue] = []
        self.lock        = threading.Lock()
        self.reset()

    def reset(self):
        """Clear per-job state ONLY. SSE subscribers + the lock are left
        alone — clearing them mid-run would silently sever every dashboard
        that's listening to /api/repair/stream."""
        self.running     = False
        self.aborted     = False
        self.start_time  = None
        self.history     = []          # list of log entry dicts
        self.result      = None        # populated on completion

_job = JobState()


# ---------------------------------------------------------------------------
# Emit helper – writes to history + all SSE subscribers
# ---------------------------------------------------------------------------
def emit(tag: str, msg: str, cls: str = "") -> None:
    if not cls:
        cls = tag.lower()
    entry = {
        "tag": tag,
        "msg": msg,
        "cls": cls,
        "ts":  _elapsed(),
    }
    log.info("[%s] %s", tag, msg)
    with _job.lock:
        _job.history.append(entry)
        payload = json.dumps(entry)
        dead = []
        for q in _job.subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _job.subscribers.remove(q)


def _elapsed() -> str:
    if _job.start_time is None:
        return "00:00:00"
    s = int(time.time() - _job.start_time)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# ---------------------------------------------------------------------------
# URL helpers — a single 'url' field replaces host / port / urlbase.
# ---------------------------------------------------------------------------
from urllib.parse import urlparse, urlunparse  # noqa: E402


def _split_url(url: str, default_port: int = 80) -> tuple[str, int, str]:
    """Return (host, port, urlbase) parsed from a URL like
    'http://172.17.0.12:8989/sonarr'. Schemes other than http/https are
    normalised to http. urlbase has a leading slash if present, no trailing."""
    if not url:
        return "", default_port, ""
    if "://" not in url:
        url = "http://" + url
    p = urlparse(url)
    host = p.hostname or ""
    port = p.port or default_port
    base = (p.path or "").rstrip("/")
    return host, port, base


def _base_url_from_parts(host, port, urlbase="") -> str:
    ub = (urlbase or "").rstrip("/")
    return f"http://{host}:{port}{ub}"


def _get_status(host, port, apikey, urlbase="", timeout=5, api="v3"):
    try:
        url = f"{_base_url_from_parts(host, port, urlbase)}/api/{api}/system/status"
        r = requests.get(url, headers={"X-Api-Key": apikey}, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _shutdown_app(host, port, apikey, urlbase="", api="v3"):
    try:
        url = f"{_base_url_from_parts(host, port, urlbase)}/api/{api}/system/shutdown"
        r = requests.post(url, headers={"X-Api-Key": apikey}, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("Shutdown request failed: %s", e)


# ---------------------------------------------------------------------------
# Docker helpers (used when the user provides a container_name AND the
# /var/run/docker.sock is mounted). docker is an optional dep — handle absence.
# ---------------------------------------------------------------------------
try:
    import docker as _docker_sdk          # noqa: F401
    _HAVE_DOCKER_SDK = True
except ImportError:
    _HAVE_DOCKER_SDK = False


def _docker_client():
    """Return a docker.DockerClient if the SDK is installed AND we can talk to
    the daemon (socket mounted, user in the right group). Returns None on any
    failure — callers must handle the fallback path."""
    if not _HAVE_DOCKER_SDK:
        return None
    try:
        client = _docker_sdk.from_env(timeout=10)
        client.ping()
        return client
    except Exception as e:
        log.debug("Docker client unavailable: %s", e)
        return None


def _docker_container(name):
    """Look up a container by name. Returns (client, container) or (None, None)
    if the daemon or container is unreachable."""
    client = _docker_client()
    if not client:
        return None, None
    try:
        return client, client.containers.get(name)
    except Exception as e:
        log.debug("Container %s not found: %s", name, e)
        return None, None


# ---------------------------------------------------------------------------
# Repair steps
# ---------------------------------------------------------------------------
def _step_preflight(cfg) -> str | None:
    """Returns resolved db path or None on failure."""
    emit("PHASE", "── Step 1/6  Preflight ──────────────────────────────────", "phase")
    host, port, apikey, urlbase = cfg["host"], cfg["port"], cfg["apikey"], cfg.get("urlbase", "")
    api = cfg.get("api", "v3")
    st = _get_status(host, port, apikey, urlbase, api=api)
    if not st:
        emit("ERR", f"Cannot reach {cfg['app']} at {_base_url_from_parts(host, port, urlbase)}", "err")
        emit("ERR", "Check host / port / apikey settings.", "err")
        return None

    emit("OK",   f"Connected – {cfg['app'].capitalize()} v{st.get('version','?')} on {st.get('osName','?')}", "ok")
    app_data = st.get("appData", "")
    emit("INFO", f"App data dir: {app_data or '(unknown)'}", "info")

    # Resolve the DB path on Starr's side. Priority:
    #   1. Explicit db_path from the request body (advanced override).
    #   2. The discovered path that the auto-detect filled in for this app.
    #   3. Fallback: APPDATA_DIR/<app>/<dbname> if such a file exists.
    dbname = APP_DEFAULTS[cfg["app"]]["dbname"]
    db_path = cfg.get("db_path") or ""
    if not db_path:
        fallback = app.config["APPDATA_DIR"] / cfg["app"] / dbname
        if fallback.exists():
            db_path = str(fallback)
            emit("INFO", f"Auto-detected DB path: {db_path}", "info")
    if not db_path or not Path(db_path).exists():
        emit("ERR", "Could not locate this app's database file.", "err")
        emit("ERR", "Mount your host appdata root at /appdata (or pass an explicit db_path).", "err")
        return None

    mb = Path(db_path).stat().st_size / 1_048_576
    emit("OK",   f"DB confirmed: {db_path}  ({mb:.1f} MB)", "ok")
    return db_path


def _probe_db_clean(db_path: str) -> tuple[bool, str]:
    """Open the DB read-only and run quick_check + foreign_key_check while the
    app may still be running. Returns (is_clean, reason). Used by the scheduler's
    skip-if-clean optimisation — never modifies the file."""
    try:
        uri = f"file:{db_path}?mode=ro&immutable=0"
        con = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as e:
        return False, f"open failed: {e}"
    try:
        rows = con.execute("PRAGMA quick_check").fetchall()
        if not rows or rows[0][0] != "ok":
            return False, f"quick_check: {len(rows)} issue(s)"
        rows = con.execute("PRAGMA foreign_key_check").fetchall()
        if rows:
            return False, f"foreign_key_check: {len(rows)} violation(s)"
        return True, "clean"
    except sqlite3.Error as e:
        return False, f"probe failed: {e}"
    finally:
        con.close()


def _step_shutdown(cfg) -> bool:
    emit("PHASE", "── Step 2/6  Shutdown ───────────────────────────────────", "phase")
    if cfg.get("dry_run"):
        if (cfg.get("container_name") or "").strip():
            emit("DRY", f"[DRY] Would docker stop '{cfg['container_name']}'", "dry")
        else:
            emit("DRY", f"[DRY] Would POST /api/{cfg.get('api','v3')}/system/shutdown", "dry")
        return True
    if cfg.get("skip_shutdown"):
        emit("WARN", "Skipping shutdown (skip_shutdown=true).", "warn"); return True

    host, port, apikey, urlbase = cfg["host"], cfg["port"], cfg["apikey"], cfg.get("urlbase","")
    api = cfg.get("api", "v3")
    container_name = (cfg.get("container_name") or "").strip()

    # Preferred path: if the user supplied a container name and we can reach
    # the Docker daemon, stop the container outright. This sidesteps the
    # restart-policy race that breaks the /api/v3/system/shutdown approach.
    if container_name:
        client, container = _docker_container(container_name)
        if container is None:
            emit("WARN", f"Container '{container_name}' not reachable via docker.sock — falling back to app shutdown API.", "warn")
            emit("INFO", "Mount /var/run/docker.sock and put PUID's group in the docker group to enable container-managed shutdown.", "info")
        else:
            emit("INFO", f"Stopping container '{container_name}' via Docker (timeout 30s)...", "info")
            try:
                container.stop(timeout=30)
            except Exception as e:
                emit("ERR", f"docker stop failed: {e}", "err")
                return False
            cfg["_docker_managed"] = container_name
            # Confirm the app's API is actually gone — a stopped container's
            # network endpoint should refuse connections immediately.
            for _ in range(5):
                if _job.aborted:
                    emit("WARN", "Aborted during shutdown wait.", "warn"); return False
                time.sleep(1)
                if _get_status(host, port, apikey, urlbase, timeout=2, api=api) is None:
                    emit("OK", f"Container '{container_name}' stopped. Waiting 2s for file handles to close...", "ok")
                    time.sleep(2)
                    return True
            emit("WARN", "Container reports stopped but app still responds — proceeding anyway.", "warn")
            return True

    emit("INFO", f"Sending shutdown to {cfg['app'].capitalize()}...", "info")
    _shutdown_app(host, port, apikey, urlbase, api=api)
    emit("OK",   "Shutdown command sent.", "ok")

    emit("INFO", "Polling until offline (2s intervals, 60s timeout)...", "info")
    deadline = time.time() + 60
    while time.time() < deadline:
        if _job.aborted:
            emit("WARN", "Aborted during shutdown wait.", "warn"); return False
        time.sleep(2)
        if _get_status(host, port, apikey, urlbase, timeout=2, api=api) is None:
            # First offline read. But a container with a restart policy
            # (restart: unless-stopped) will be brought back automatically a
            # few seconds after the app process exits — so confirm it STAYS
            # down before we touch the database.
            emit("OK", "App appears offline. Confirming it stays down (~15s)...", "ok")
            for _ in range(SHUTDOWN_STABILITY_CHECKS):
                if _job.aborted:
                    emit("WARN", "Aborted during shutdown wait.", "warn"); return False
                time.sleep(SHUTDOWN_STABILITY_INTERVAL)
                if _get_status(host, port, apikey, urlbase, timeout=2, api=api) is not None:
                    emit("ERR", "App came back ONLINE after shutdown — its container restart policy is restarting it.", "err")
                    emit("ERR", "Starr will not repair a database the app may reopen mid-operation.", "err")
                    emit("ERR", "Stop the app's CONTAINER (not just the app), then re-run with 'Skip shutdown' enabled:", "err")
                    emit("SYS", f"  docker stop {cfg['app']}", "sys")
                    return False
            emit("OK", "App confirmed offline. Waiting 3s for file handles to close...", "ok")
            time.sleep(3)
            return True
        emit("INFO", "Still running, waiting...", "info")

    emit("ERR", "App did not stop within 60s.", "err"); return False


def _step_backup(cfg, db_path: str) -> str | None:
    emit("PHASE", "── Step 3/6  Backup ─────────────────────────────────────", "phase")
    if cfg.get("no_backup"):
        emit("WARN", "Backup skipped (no_backup=true).", "warn"); return None

    app.config["BACKUP_DIR"].mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = app.config["BACKUP_DIR"] / f"{cfg['app']}_{ts}.db"

    if cfg.get("dry_run"):
        emit("DRY", f"[DRY] Would copy {db_path} → {dest}", "dry"); return str(dest)

    emit("INFO", f"Source : {db_path}", "info")
    emit("INFO", f"Dest   : {dest}", "info")
    try:
        shutil.copy2(db_path, dest)
    except Exception as e:
        emit("ERR", f"Backup failed: {e}", "err"); return None

    mb = dest.stat().st_size / 1_048_576
    emit("OK",   f"Backup created ({mb:.1f} MB)", "ok")

    # Prune old backups
    max_days = app.config["MAX_BACKUP_AGE_DAYS"]
    cutoff   = time.time() - max_days * 86400
    removed  = 0
    for old in app.config["BACKUP_DIR"].glob(f"{cfg['app']}_*.db"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True); removed += 1
    if removed:
        emit("INFO", f"Pruned {removed} backup(s) older than {max_days} days.", "info")

    return str(dest)


def _step_repair(cfg, db_path: str) -> dict:
    emit("PHASE", "── Step 4/6  SQLite Repairs ─────────────────────────────", "phase")
    results = {}
    ops = cfg.get("ops") or ALL_OPS

    if cfg.get("dry_run"):
        for op in ops:
            emit("DRY", f"[DRY] {op.upper():<22}  {OP_DESC.get(op,'')}", "dry")
        return results

    try:
        con = sqlite3.connect(db_path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error as e:
        emit("ERR", f"Cannot open DB: {e}", "err"); return results

    for op in ops:
        if _job.aborted:
            emit("WARN", "Repair aborted by user.", "warn"); break
        emit("INFO", f"Running {op.upper():<22}  {OP_DESC.get(op,'')}", "info")
        try:
            if op == "integrity":
                rows = con.execute("PRAGMA integrity_check").fetchall()
                if rows and rows[0][0] == "ok":
                    emit("OK", "integrity_check: ok – no corruption detected.", "ok")
                    results[op] = ("ok", 0)
                else:
                    issues = [r[0] for r in rows]
                    emit("WARN", f"{len(issues)} issue(s) found:", "warn")
                    for i in issues[:8]: emit("WARN", f"  • {i}", "warn")
                    if len(issues) > 8: emit("WARN", f"  … and {len(issues)-8} more", "warn")
                    results[op] = ("issues", len(issues))

            elif op == "foreign_keys":
                rows = con.execute("PRAGMA foreign_key_check").fetchall()
                if not rows:
                    emit("OK", "No FK violations found.", "ok"); results[op] = ("ok", 0)
                else:
                    emit("WARN", f"{len(rows)} violation(s) found – repairing...", "warn")
                    con.execute("PRAGMA foreign_keys = OFF")
                    fixed = 0
                    for tbl, rowid, parent, fkid in rows:
                        try:
                            con.execute(f"DELETE FROM [{tbl}] WHERE rowid=?", (rowid,))
                            emit("INFO", f"  Removed orphan: {tbl}.rowid={rowid} (parent={parent})", "info")
                            fixed += 1
                        except sqlite3.Error as de:
                            emit("WARN", f"  Could not remove {tbl}.rowid={rowid}: {de}", "warn")
                    con.execute("PRAGMA foreign_keys = ON"); con.commit()
                    emit("OK", f"Repaired {fixed}/{len(rows)} FK violations.", "ok")
                    results[op] = ("fixed", fixed)

            elif op == "wal_checkpoint":
                row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                emit("OK", f"WAL checkpoint – log: {row[1]} frames, checkpointed: {row[2]}", "ok")
                results[op] = ("ok", row[2])

            elif op == "vacuum":
                before = Path(db_path).stat().st_size / 1_048_576
                emit("INFO", f"Pre-VACUUM: {before:.1f} MB", "info")
                con.execute("VACUUM"); con.commit()
                after = Path(db_path).stat().st_size / 1_048_576
                emit("OK", f"VACUUM done: {before:.1f} MB → {after:.1f} MB  (reclaimed {before-after:.1f} MB)", "ok")
                results[op] = ("ok", before - after)

            elif op == "reindex":
                con.execute("REINDEX"); con.commit()
                emit("OK", "REINDEX complete – all indexes rebuilt.", "ok")
                results[op] = ("ok", 0)

            elif op == "analyze":
                con.execute("ANALYZE"); con.commit()
                emit("OK", "ANALYZE complete – query-planner stats updated.", "ok")
                results[op] = ("ok", 0)

            else:
                emit("WARN", f"Unknown op '{op}' – skipped.", "warn")
                results[op] = ("skipped", 0)

        except sqlite3.Error as e:
            emit("ERR", f"{op} failed: {e}", "err")
            results[op] = ("error", str(e))

    con.close()
    return results


def _step_report(cfg, backup_path, results) -> None:
    emit("PHASE", "── Step 5/6  Report ─────────────────────────────────────", "phase")
    if cfg.get("dry_run"):
        emit("DRY", "DRY RUN complete – zero disk changes.", "dry")
    else:
        ok_n  = sum(1 for s, _ in results.values() if s in ("ok", "fixed"))
        err_n = sum(1 for s, _ in results.values() if s in ("issues", "error"))
        emit("OK",   f"Operations: {len(results)}   Passed/Fixed: {ok_n}", "ok")
        if err_n: emit("WARN", f"Issues detected: {err_n}  (see log above)", "warn")
        if backup_path:
            emit("OK",   f"Backup: {backup_path}", "ok")
            emit("INFO", "Delete backup once app is confirmed working.", "info")


def _step_restart(cfg, results) -> None:
    """Step 6 -- wait for the app to come back online.

    Relies on Docker restart policy (restart: unless-stopped) to bring
    the container back up automatically after shutdown. We poll the
    status endpoint until the app responds or we time out.
    Skipped entirely on dry-run or if skip_shutdown was set.
    """
    emit("PHASE", "── Step 6/6  Restart ────────────────────────────────────", "phase")

    ok_n  = sum(1 for s, _ in results.values() if s in ("ok", "fixed"))
    err_n = sum(1 for s, _ in results.values() if s in ("issues", "error"))

    if cfg.get("dry_run"):
        emit("DRY", "DRY RUN – skipping restart wait.", "dry")
        emit("__DONE__", json.dumps({
            "fixed": ok_n, "errors": err_n,
            "elapsed": _elapsed(), "dry_run": True,
        }), "__done__")
        return

    if cfg.get("skip_shutdown"):
        emit("INFO", "Shutdown was skipped — app should still be running.", "info")
        emit("__DONE__", json.dumps({
            "fixed": ok_n, "errors": err_n,
            "elapsed": _elapsed(), "dry_run": False,
        }), "__done__")
        return

    host, port, apikey, urlbase = cfg["host"], cfg["port"], cfg["apikey"], cfg.get("urlbase", "")
    api = cfg.get("api", "v3")

    # If we stopped the container ourselves, start it ourselves. The user's
    # restart policy is not enough — `docker stop` cleared the container's
    # exit state, so unless-stopped won't auto-start it.
    docker_managed = cfg.get("_docker_managed")
    if docker_managed:
        _, container = _docker_container(docker_managed)
        if container is None:
            emit("ERR", f"Cannot reach container '{docker_managed}' to start it.", "err")
            emit("SYS", f"  docker start {docker_managed}", "sys")
        else:
            emit("INFO", f"Starting container '{docker_managed}'...", "info")
            try:
                container.start()
                emit("OK", f"Container '{docker_managed}' started.", "ok")
            except Exception as e:
                emit("ERR", f"docker start failed: {e}", "err")
                emit("SYS", f"  docker start {docker_managed}", "sys")

    emit("INFO", f"Waiting for {cfg['app'].capitalize()} to come back online...", "info")
    if not docker_managed:
        emit("INFO", "(Docker restart policy will bring it up automatically)", "info")

    deadline = time.time() + 180   # 3-minute timeout
    attempt  = 0
    while time.time() < deadline:
        if _job.aborted:
            emit("WARN", "Aborted during restart wait.", "warn")
            break
        time.sleep(5)
        attempt += 1
        st = _get_status(host, port, apikey, urlbase, timeout=3, api=api)
        if st:
            emit("OK", f"{cfg['app'].capitalize()} is online — v{st.get('version','?')} OK", "ok")
            emit("OK", "Repair complete. All done!", "ok")
            emit("__DONE__", json.dumps({
                "fixed": ok_n, "errors": err_n,
                "elapsed": _elapsed(), "dry_run": False,
            }), "__done__")
            return
        if attempt % 3 == 0:
            remaining = int(deadline - time.time())
            emit("INFO", f"Still waiting... ({remaining}s remaining)", "info")

    emit("WARN", f"{cfg['app'].capitalize()} did not come back within 3 minutes.", "warn")
    emit("WARN", "It may still be starting up — check your container manager.", "warn")
    emit("SYS",  f"  docker restart {cfg['app']}", "sys")
    emit("__DONE__", json.dumps({
        "fixed": ok_n, "errors": err_n,
        "elapsed": _elapsed(), "dry_run": False,
    }), "__done__")


# ---------------------------------------------------------------------------
# Background repair thread
# ---------------------------------------------------------------------------
def _repair_worker(cfg: dict) -> None:
    _job.start_time = time.time()
    _job.running    = True
    _job.aborted    = False
    _job.history    = []
    _job.result     = None

    emit("SYS", f"Starr DB Repair v1.0 – job started for {cfg['app'].upper()}", "sys")
    emit("SYS", f"Dry run: {cfg.get('dry_run', False)}", "sys")

    try:
        db_path = _step_preflight(cfg)
        if not db_path:
            _job.result = {"status": "error", "message": "Preflight failed"}
            return

        # Skip-if-clean (used by scheduled jobs): probe the DB read-only while
        # the app is still running. If quick_check + FK both pass, abort the
        # whole run — no shutdown, no backup, no mutating ops.
        if cfg.get("skip_if_clean") and not cfg.get("dry_run"):
            emit("PHASE", "── Skip-if-clean probe ─────────────────────────────────", "phase")
            ok, reason = _probe_db_clean(db_path)
            if ok:
                emit("OK", "Database is clean — skipping repair (no shutdown, no backup).", "ok")
                _job.result = {
                    "status":  "clean",
                    "message": "Database is clean; skipped scheduled run.",
                    "elapsed": _elapsed(),
                }
                return
            emit("INFO", f"Probe found issues ({reason}); running full repair.", "info")

        if not _step_shutdown(cfg):
            _job.result = {"status": "error", "message": "Shutdown failed or aborted"}
            return

        backup = _step_backup(cfg, db_path)

        # Safety: if backup was supposed to happen and didn't, never mutate the
        # source DB. _step_backup returns None on both intentional skip
        # (no_backup=true) and on failure, so distinguish here.
        if backup is None and not cfg.get("no_backup") and not cfg.get("dry_run"):
            emit("ERR", "Aborting repair — refusing to run SQLite operations without a backup.", "err")
            emit("ERR", "Fix the backup destination permissions and try again:", "err")
            emit("SYS", f"  chown -R 1000:1000 {app.config['BACKUP_DIR']}", "sys")
            _step_restart(cfg, {})   # bring the app back online
            _job.result = {"status": "error", "message": "Backup failed; repair aborted"}
            return

        # Defense in depth: a container restart policy can bring the app back
        # online after _step_shutdown returned. Re-verify it's still offline
        # immediately before we open and mutate the database.
        if not cfg.get("skip_shutdown") and not cfg.get("dry_run"):
            if _get_status(cfg["host"], cfg["port"], cfg["apikey"], cfg.get("urlbase", ""), api=cfg.get("api", "v3")) is not None:
                emit("ERR", "App is back ONLINE just before repair — aborting to protect the database.", "err")
                emit("ERR", "Its container restart policy likely restarted it. Stop the container and re-run with 'Skip shutdown':", "err")
                emit("SYS", f"  docker stop {cfg['app']}", "sys")
                _step_restart(cfg, {})
                _job.result = {"status": "error", "message": "App restarted before repair; aborted"}
                return

        results = _step_repair(cfg, db_path)

        _step_report(cfg, backup, results)

        _step_restart(cfg, results)

        ok_n  = sum(1 for s, _ in results.values() if s in ("ok", "fixed"))
        err_n = sum(1 for s, _ in results.values() if s in ("issues", "error"))
        _job.result = {
            "status":  "aborted" if _job.aborted else ("ok" if err_n == 0 else "warning"),
            "fixed":   ok_n,
            "errors":  err_n,
            "elapsed": _elapsed(),
            "backup":  backup,
        }
    except Exception as e:
        emit("ERR", f"Unexpected error: {e}", "err")
        log.exception("Repair worker crashed")
        _job.result = {"status": "error", "message": str(e)}
    finally:
        _job.running = False
        # Make sure the UI always gets a terminal __DONE__ event, even on
        # early-return paths (preflight / shutdown / backup-safety / restart
        # guard). _step_restart emits its own __DONE__ on the happy path;
        # only emit here if it didn't.
        if not any(h.get("cls") == "__done__" for h in _job.history):
            err_msg = (_job.result or {}).get("message", "Job ended without completing.")
            emit("ERR", err_msg, "err") if _job.result and _job.result.get("status") == "error" else None
            emit("__DONE__", json.dumps({
                "fixed":    0,
                "errors":   1 if (_job.result or {}).get("status") == "error" else 0,
                "elapsed":  _elapsed(),
                "dry_run":  cfg.get("dry_run", False),
                "status":   (_job.result or {}).get("status", "error"),
                "message":  err_msg,
            }), "__done__")
        emit("SYS", "Job finished. SSE stream remains open.", "sys")


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    # Pass whether a real SECRET_KEY has been configured so the UI can warn
    using_default_key = app.config["SECRET_KEY"] == "change-me-in-production"
    return render_template("index.html", config=app.config, using_default_key=using_default_key)


@app.route("/healthz")
def healthz():
    """Docker / k8s liveness probe — no auth required."""
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()}), 200


@app.route("/readyz")
def readyz():
    """Docker / k8s readiness probe — no auth required."""
    return jsonify({"status": "ready"}), 200


@app.route("/api/config")
def api_config():
    """Public config for the UI — no auth required.
    Tells the frontend whether the default SECRET_KEY is still in use
    so it can display a security warning without needing Jinja2 templating.
    """
    return jsonify({
        "using_default_key": app.config["SECRET_KEY"] == "change-me-in-production",
    }), 200


import discovery as _discovery  # noqa: E402

_discovery_cache: dict = {"apps": [], "appdata": {}, "warnings": [], "docker_available": False}


def _refresh_discovery() -> dict:
    """Rescan via Docker and update the in-memory cache. Safe to call often."""
    global _discovery_cache
    _discovery_cache = _discovery.discover()
    return _discovery_cache


def _discovered_for(app_name: str) -> dict:
    for d in _discovery_cache.get("apps") or []:
        if d.get("app") == app_name:
            return d
    return {}


@app.route("/api/discover", methods=["POST"])
@require_api_key
def api_discover():
    """Trigger a fresh Docker scan, return the cache."""
    return jsonify(_refresh_discovery())


def _request_host_only() -> str:
    """The hostname the browser used to reach Starr, no port. Falls back to
    'localhost' if the request context isn't available."""
    try:
        h = (request.host or "").split(":")[0]
        return h or "localhost"
    except RuntimeError:
        return "localhost"


@app.route("/api/apps")
@require_api_key
def api_apps():
    """Per-app configuration. Layered: discovery → env vars → request body.

    For each app we return:
      - `url`           : what the user sees in the form. Host-perspective
                          (e.g. http://192.168.10.37:8989) so the value is
                          recognisable from a browser on the LAN.
      - `internal_url`  : what Starr will actually use to talk to the *arr
                          container (bridge IP + internal port). This is what
                          actually connects across Docker's default bridge
                          network. The user never sees / edits this.
      - `apikey`, `container_name`, `db_path` come from env / discovery.

    The host used to build `url` is derived from request.host, so it matches
    whichever IP/hostname the user typed into their browser to reach Starr.
    """
    apps = []
    discovered = {d["app"]: d for d in (_discovery_cache.get("apps") or [])}
    browser_host = _request_host_only()
    for name in ("sonarr", "radarr", "lidarr", "sportarr"):
        upper  = name.upper()
        apikey = app.config.get(f"{upper}_APIKEY", "")
        env_url = app.config.get(f"{upper}_URL", "")
        disc   = discovered.get(name, {})

        # Internal URL — used for the actual HTTP call from inside Starr.
        internal_url = disc.get("url") or ""

        # Display URL — what we render in the form. Order of preference:
        #   1. explicit *_URL env override (the user wrote it, respect it)
        #   2. host-perspective URL built from request.host + published port
        #   3. fall back to the internal bridge URL
        display_url = env_url
        if not display_url and disc.get("published_port"):
            base = f"http://{browser_host}:{disc['published_port']}"
            display_url = base + (disc.get("urlbase") or "")
        if not display_url:
            display_url = internal_url

        if not (apikey or display_url):
            continue
        apps.append({
            "app":            name,
            "url":            display_url,
            "internal_url":   internal_url,
            "apikey":         apikey,
            "container_name": disc.get("container_name") or "",
            "db_path":        disc.get("db_path") or "",
            "discovered":     bool(disc),
            "configured":     bool(apikey),
        })
    return jsonify(apps)


def _resolve_request_cfg(cfg: dict) -> tuple[dict, str | None]:
    """Fill cfg with env + discovery defaults; return (cfg, error_message)."""
    app_name = (cfg.get("app") or "").lower()
    if app_name not in APP_DEFAULTS:
        return cfg, "app must be sonarr, radarr, lidarr, or sportarr"
    upper = app_name.upper()
    disc = _discovered_for(app_name)
    # URL: request body wins, else env, else discovery.
    raw_url = (cfg.get("url") or "").strip() \
          or app.config.get(f"{upper}_URL", "") \
          or (disc.get("url") or "")
    if not raw_url:
        return cfg, f"{app_name}: no URL configured and Docker discovery did not find a container."

    # If the request URL matches the host-perspective display URL we returned
    # from /api/apps (i.e. the user didn't override), prefer the discovered
    # bridge URL — that's what's actually reachable from inside Starr's own
    # container.
    if disc.get("url") and disc.get("published_port"):
        display_host = (cfg.get("url") or "").strip()
        h, p, _ = _split_url(display_host) if display_host else ("", 0, "")
        if p == disc["published_port"]:
            raw_url = disc["url"]

    host, port, urlbase = _split_url(raw_url, default_port=APP_DEFAULTS[app_name]["port"])
    cfg["host"]    = host
    cfg["port"]    = port
    cfg["urlbase"] = urlbase
    cfg["api"]     = APP_DEFAULTS[app_name]["api"]
    # apikey: request body wins, else env.
    if not cfg.get("apikey"):
        cfg["apikey"] = app.config.get(f"{upper}_APIKEY", "")
    if not cfg.get("apikey"):
        return cfg, "apikey is required (request body or env)."
    # Container: request body wins, else discovery.
    if not cfg.get("container_name"):
        cfg["container_name"] = disc.get("container_name") or ""
    # DB path: request body wins, else discovery (preflight will resolve if blank).
    if not cfg.get("db_path"):
        cfg["db_path"] = disc.get("db_path") or ""
    return cfg, None


@app.route("/api/repair/start", methods=["POST"])
@require_api_key
def api_start():
    if _job.running:
        return jsonify({"error": "A repair job is already running."}), 409

    cfg = request.get_json(force=True) or {}
    cfg, err = _resolve_request_cfg(cfg)
    if err:
        return jsonify({"error": err}), 400

    # Validate ops list
    ops = cfg.get("ops") or ALL_OPS
    invalid = [o for o in ops if o not in ALL_OPS]
    if invalid:
        return jsonify({"error": f"Unknown ops: {invalid}", "valid": ALL_OPS}), 400
    cfg["ops"] = ops

    _job.reset()
    thread = threading.Thread(target=_repair_worker, args=(cfg,), daemon=True)
    thread.start()

    return jsonify({"status": "started", "app": cfg["app"]}), 202


@app.route("/api/repair/stop", methods=["POST"])
@require_api_key
def api_stop():
    if not _job.running:
        return jsonify({"error": "No job running."}), 409
    _job.aborted = True
    emit("WARN", "Stop requested by user – aborting after current step.", "warn")
    return jsonify({"status": "aborting"}), 200


@app.route("/api/repair/status")
@require_api_key
def api_status():
    return jsonify({
        "running":  _job.running,
        "aborted":  _job.aborted,
        "elapsed":  _elapsed(),
        "lines":    len(_job.history),
        "result":   _job.result,
    })


@app.route("/api/repair/stream")
@require_api_key
def api_stream():
    """Server-Sent Events endpoint – streams live log entries.
    Auth via ?api_key= query param because EventSource cannot set headers.
    """
    def generate():
        # Replay history for late-joining clients
        with _job.lock:
            history_snapshot = list(_job.history)
        for entry in history_snapshot:
            yield f"data: {json.dumps(entry)}\n\n"

        client_q: queue.Queue = queue.Queue(maxsize=512)
        with _job.lock:
            _job.subscribers.append(client_q)

        try:
            while True:
                try:
                    payload = client_q.get(timeout=15)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"   # heartbeat
        finally:
            with _job.lock:
                if client_q in _job.subscribers:
                    _job.subscribers.remove(client_q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/backups")
@require_api_key
def api_backups():
    """List backup files in the backup directory."""
    backup_dir = app.config["BACKUP_DIR"]
    backups = []
    if backup_dir.exists():
        for f in sorted(backup_dir.glob("*.db"), reverse=True):
            stat = f.stat()
            backups.append({
                "name":     f.name,
                "size_mb":  round(stat.st_size / 1_048_576, 1),
                "created":  datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
    return jsonify(backups)


@app.route("/api/backups/<name>", methods=["DELETE"])
@require_api_key
def api_backup_delete(name):
    """Delete a single backup file. Guards against path traversal — only a
    bare filename ending in .db inside BACKUP_DIR is accepted."""
    # Reject anything that isn't a plain filename (no slashes, no '..').
    if name != Path(name).name or not name.endswith(".db"):
        return jsonify({"error": "invalid backup name"}), 400
    backup_dir = app.config["BACKUP_DIR"]
    target = (backup_dir / name).resolve()
    # Ensure the resolved path is still inside BACKUP_DIR.
    try:
        target.relative_to(Path(backup_dir).resolve())
    except ValueError:
        return jsonify({"error": "invalid backup path"}), 400
    if not target.exists():
        return jsonify({"error": "not found"}), 404
    try:
        target.unlink()
    except OSError as e:
        return jsonify({"error": f"delete failed: {e}"}), 500
    log.info("Deleted backup %s", name)
    return jsonify({"status": "deleted", "name": name})
def _run_scheduled(cfg: dict) -> dict:
    """Synchronously run a scheduled repair via _repair_worker. Resolves
    host/port/urlbase/apikey/container_name/db_path the same way the
    /api/repair/start endpoint does (env + Docker discovery)."""
    if _job.running:
        return {"status": "skipped", "reason": "another job in progress"}
    sched_name = cfg.get("_schedule_name") or "schedule"
    log.info("Scheduled run firing: %s", sched_name)
    cfg, err = _resolve_request_cfg(cfg)
    if err:
        return {"status": "error", "message": err}
    _repair_worker(cfg)
    return dict(_job.result or {"status": "unknown"})


from schedules import ScheduleStore, ScheduleRunner   # noqa: E402
import atexit                                          # noqa: E402

# Init schedule store + runner. Tests can disable the runner via env to avoid
# leaving an APScheduler thread alive (which would block pytest from exiting).
_schedule_store = ScheduleStore(app.config["BACKUP_DIR"] / ".starr-schedules.json")
if os.environ.get("STARR_DISABLE_SCHEDULER") == "1":
    _schedule_runner = None
    log.info("Scheduler disabled by STARR_DISABLE_SCHEDULER=1")
else:
    _schedule_runner = ScheduleRunner(_schedule_store, _run_scheduled, lambda: _job.running)
    atexit.register(lambda: _schedule_runner._scheduler.shutdown(wait=False))
    # Seed the Docker discovery cache once at startup so /api/apps returns
    # auto-detected URLs without the user having to click "Detect" first.
    try:
        _refresh_discovery()
    except Exception:
        log.exception("Initial discovery scan failed (will retry on demand)")


def _scheduler_required():
    if _schedule_runner is None:
        return jsonify({"error": "scheduler disabled"}), 503
    return None


def _decorate_schedule(s: dict) -> dict:
    """Attach computed fields (next_run) before sending to the client."""
    next_run = _schedule_runner.next_run_for(s["id"]) if _schedule_runner else None
    return {**s, "next_run": next_run}


@app.route("/api/schedules")
@require_api_key
def api_schedules_list():
    return jsonify([_decorate_schedule(s) for s in _schedule_store.all()])


@app.route("/api/schedules", methods=["POST"])
@require_api_key
def api_schedules_create():
    payload = request.get_json(force=True) or {}
    try:
        sched = _schedule_store.add(payload)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if _schedule_runner: _schedule_runner.reload()
    return jsonify(_decorate_schedule(sched)), 201


@app.route("/api/schedules/<sid>", methods=["PUT"])
@require_api_key
def api_schedules_update(sid):
    payload = request.get_json(force=True) or {}
    try:
        sched = _schedule_store.update(sid, payload)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not sched:
        return jsonify({"error": "not found"}), 404
    if _schedule_runner: _schedule_runner.reload()
    return jsonify(_decorate_schedule(sched))


@app.route("/api/schedules/<sid>", methods=["DELETE"])
@require_api_key
def api_schedules_delete(sid):
    if not _schedule_store.delete(sid):
        return jsonify({"error": "not found"}), 404
    if _schedule_runner: _schedule_runner.reload()
    return jsonify({"status": "deleted"})


@app.route("/api/schedules/<sid>/run-now", methods=["POST"])
@require_api_key
def api_schedules_run_now(sid):
    err = _scheduler_required()
    if err: return err
    if not _schedule_runner.run_now(sid):
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "started"}), 202


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8877"))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    log.info("Starting Starr DB Repair on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
