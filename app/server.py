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

import hmac
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
    # Compress backups to .db.zst (zstd). Big space win; default on.
    BACKUP_COMPRESS     = os.environ.get("BACKUP_COMPRESS", "true").lower() == "true"
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
    READARR_APIKEY      = os.environ.get("READARR_APIKEY", "")
    READARR_URL         = os.environ.get("READARR_URL", "")
    PROWLARR_APIKEY     = os.environ.get("PROWLARR_APIKEY", "")
    PROWLARR_URL        = os.environ.get("PROWLARR_URL", "")
    WHISPARR_APIKEY     = os.environ.get("WHISPARR_APIKEY", "")
    WHISPARR_URL        = os.environ.get("WHISPARR_URL", "")
    BAZARR_APIKEY       = os.environ.get("BAZARR_APIKEY", "")
    BAZARR_URL          = os.environ.get("BAZARR_URL", "")

app.config.from_object(Config)
logging.getLogger().setLevel(app.config["LOG_LEVEL"])

# Restrict CORS to configured origins only
CORS(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}})

APP_DEFAULTS = {
    # api: Sonarr/Radarr/Whisparr (and the Sonarr-fork Sportarr) speak
    # /api/v3; Lidarr / Readarr / Prowlarr speak /api/v1.
    "sonarr":   {"port": 8989, "dbname": "sonarr.db",   "api": "v3"},
    "radarr":   {"port": 7878, "dbname": "radarr.db",   "api": "v3"},
    "lidarr":   {"port": 8686, "dbname": "lidarr.db",   "api": "v1"},
    "sportarr": {"port": 1867, "dbname": "sportarr.db", "api": "v3"},
    "readarr":  {"port": 8787, "dbname": "readarr.db",  "api": "v1"},
    "prowlarr": {"port": 9696, "dbname": "prowlarr.db", "api": "v1"},
    "whisparr": {"port": 6969, "dbname": "whisparr.db", "api": "v3"},
    # Bazarr is the odd one out: versionless API (/api/...) and its DB lives at
    # /config/db/bazarr.db rather than /config/bazarr.db.
    "bazarr":   {"port": 6767, "dbname": "db/bazarr.db", "api": ""},
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
            or ""
        )
        # Constant-time compare — a plain != leaks how many leading characters
        # matched via response timing (the LAN-only threat model here still
        # doesn't make this a priority, but it's a one-line fix).
        if not hmac.compare_digest(provided, secret):
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
        # The SQLite connection of the in-flight repair, if any. Held so the
        # stop endpoint can call .interrupt() from another thread to abort a
        # long-running VACUUM / REINDEX mid-statement (a plain aborted flag
        # only takes effect between ops). Set/cleared by _step_repair.
        self.active_conn = None

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


def _api_path(api: str, endpoint: str) -> str:
    """Build the API path for an endpoint. Most *arr apps version their API
    (/api/v3/... , /api/v1/...); Bazarr is versionless (/api/...). An empty
    `api` selects the versionless form."""
    return f"/api/{api}/{endpoint}" if api else f"/api/{endpoint}"


def _get_status(host, port, apikey, urlbase="", timeout=5, api="v3"):
    try:
        url = _base_url_from_parts(host, port, urlbase) + _api_path(api, "system/status")
        # Send the apikey both as a header (Sonarr-style; case-insensitive so
        # Bazarr's X-API-KEY matches) and as a query param (Bazarr also accepts
        # ?apikey=) for maximum compatibility.
        r = requests.get(url, headers={"X-Api-Key": apikey},
                         params={"apikey": apikey}, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _shutdown_app(host, port, apikey, urlbase="", api="v3"):
    try:
        url = _base_url_from_parts(host, port, urlbase) + _api_path(api, "system/shutdown")
        r = requests.post(url, headers={"X-Api-Key": apikey},
                          params={"apikey": apikey}, timeout=10)
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

    # Bazarr nests its status under "data" and uses different key names, so
    # fall back gracefully when the usual Sonarr-style fields are absent.
    sd = st.get("data", st) if isinstance(st, dict) else {}
    version = sd.get("version") or sd.get("bazarr_version") or st.get("version", "?")
    osname  = sd.get("osName") or sd.get("operating_system") or st.get("osName", "?")
    emit("OK",   f"Connected – {cfg['app'].capitalize()} v{version} on {osname}", "ok")
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


def _compress_file(src: str, dest: str) -> None:
    """Stream-compress src → dest with zstd. Raises if zstandard is missing."""
    import zstandard as zstd
    cctx = zstd.ZstdCompressor(level=int(os.environ.get("BACKUP_ZSTD_LEVEL", "10")))
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        cctx.copy_stream(fin, fout)


def _step_backup(cfg, db_path: str) -> str | None:
    emit("PHASE", "── Step 3/6  Backup ─────────────────────────────────────", "phase")
    if cfg.get("no_backup"):
        emit("WARN", "Backup skipped (no_backup=true).", "warn"); return None

    app.config["BACKUP_DIR"].mkdir(parents=True, exist_ok=True)
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    label   = cfg.get("label") or cfg["app"]      # instance id, or app for the default
    compress = bool(app.config.get("BACKUP_COMPRESS", True))
    suffix  = ".db.zst" if compress else ".db"
    dest = app.config["BACKUP_DIR"] / f"{label}_{ts}{suffix}"

    if cfg.get("dry_run"):
        emit("DRY", f"[DRY] Would back up {db_path} → {dest}", "dry"); return str(dest)

    emit("INFO", f"Source : {db_path}", "info")
    emit("INFO", f"Dest   : {dest}", "info")
    src_mb = Path(db_path).stat().st_size / 1_048_576
    try:
        if compress:
            try:
                _compress_file(db_path, str(dest))
            except ImportError:
                # zstandard not available — fall back to a plain copy so a
                # backup is still made (never block the safety backup).
                emit("WARN", "zstandard not installed — storing uncompressed.", "warn")
                dest = app.config["BACKUP_DIR"] / f"{label}_{ts}.db"
                shutil.copy2(db_path, dest)
                compress = False
        else:
            shutil.copy2(db_path, dest)
    except Exception as e:
        emit("ERR", f"Backup failed: {e}", "err"); return None

    out_mb = dest.stat().st_size / 1_048_576
    if compress:
        ratio = (1 - out_mb / src_mb) * 100 if src_mb else 0
        emit("OK", f"Backup created ({out_mb:.1f} MB, compressed {ratio:.0f}% from {src_mb:.1f} MB)", "ok")
    else:
        emit("OK", f"Backup created ({out_mb:.1f} MB)", "ok")

    # Prune old backups (match both .db and .db.zst for this instance label).
    # Per-instance override wins, then saved global, then the env-var boot
    # default. The glob is already scoped to this label, so a longer-retention
    # neighbour (e.g. sonarr-4k) can't be pruned by a shorter sibling.
    max_days = _settings.max_backup_age_days(
        app.config["MAX_BACKUP_AGE_DAYS"], instance=label)
    if max_days > 0:
        cutoff  = time.time() - max_days * 86400
        removed = 0
        for old in app.config["BACKUP_DIR"].glob(f"{label}_*.db*"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True); removed += 1
        if removed:
            emit("INFO", f"Pruned {removed} backup(s) older than {max_days} days.", "info")

    return str(dest)


def _flag_backup(backup_path: str | None, results: dict) -> str | None:
    """After repair, rename the backup to record the outcome:
      …_clean.db[.zst]    no issues found
      …_repaired.db[.zst] issues were found/fixed
      …_aborted.db[.zst]  cancelled mid-run (kept — it predates any changes)
    Returns the new path (or the original if no rename happened)."""
    if not backup_path:
        return backup_path
    p = Path(backup_path)
    if not p.exists():
        return backup_path
    if any(s == "aborted" for s, _ in results.values()):
        flag = "aborted"
    else:
        issues = sum(1 for s, _ in results.values() if s in ("issues", "fixed", "error"))
        flag = "repaired" if issues else "clean"
    # Insert the flag before the .db/.db.zst suffix.
    name = p.name
    for suf in (".db.zst", ".db"):
        if name.endswith(suf):
            stem = name[: -len(suf)]
            new = p.with_name(f"{stem}_{flag}{suf}")
            try:
                p.rename(new)
                return str(new)
            except OSError:
                return backup_path
    return backup_path


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

    # Publish the connection so api_stop can interrupt() a long op mid-flight.
    _job.active_conn = con
    # An interrupt may have arrived between shutdown and opening the DB.
    if _job.aborted:
        con.interrupt()

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
            # interrupt() surfaces here as OperationalError("interrupted").
            # Treat any error during an abort as a clean cancellation, not a
            # repair failure.
            if _job.aborted:
                emit("WARN", f"{op.upper()} interrupted by user.", "warn")
                results[op] = ("aborted", 0)
                break
            emit("ERR", f"{op} failed: {e}", "err")
            results[op] = ("error", str(e))

    _job.active_conn = None
    try:
        con.rollback()   # undo any partial work from an interrupted statement
    except sqlite3.Error:
        pass
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

    emit("SYS", f"Starr DB Repair v1.2.0 – job started for {cfg['app'].upper()}", "sys")
    emit("SYS", f"Dry run: {cfg.get('dry_run', False)}", "sys")

    db_path = None
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

        # Tag the backup file with the outcome (…_clean / …_repaired) now that
        # we know whether anything was found, so it's obvious which to keep.
        if not cfg.get("dry_run"):
            backup = _flag_backup(backup, results)

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
        # Record the run in persistent history (powers the last-run pill,
        # pre-repair estimate, and trend chart). Best-effort.
        _record_history(cfg, _job.result or {}, db_path)
        # Fire notifications (best-effort, never raises). Scheduled runs carry
        # their own notify level override in cfg["notify"]; manual runs fall
        # back to the global level.
        _notify.maybe_notify(
            _notify_config, cfg.get("app", "?"), _job.result or {},
            level_override=cfg.get("notify"),
            scheduled=bool(cfg.get("_scheduled")),
            schedule_name=cfg.get("_schedule_name"),
        )


def _record_history(cfg: dict, result: dict, db_path) -> None:
    """Append one record to the run-history store. Never raises."""
    try:
        db_bytes = None
        if db_path:
            try:
                db_bytes = os.path.getsize(db_path)
            except OSError:
                db_bytes = None
        duration_s = round(time.time() - _job.start_time, 1) if _job.start_time else 0
        _history.record({
            "app":           (cfg.get("app") or "?").lower(),
            "instance":      (cfg.get("label") or cfg.get("app") or "?").lower(),
            "status":        result.get("status", "unknown"),
            "fixed":         result.get("fixed", 0),
            "errors":        result.get("errors", 0),
            "duration_s":    duration_s,
            "elapsed":       result.get("elapsed") or _elapsed(),
            "backup":        result.get("backup"),
            "db_bytes":      db_bytes,
            "dry_run":       bool(cfg.get("dry_run")),
            "scheduled":     bool(cfg.get("_scheduled")),
            "schedule_name": cfg.get("_schedule_name"),
            "message":       result.get("message"),
        })
    except Exception:
        log.exception("Failed to record run history")


# ---------------------------------------------------------------------------
# Restore from backup
# ---------------------------------------------------------------------------
def _decompress_file(src: str, dest: str) -> None:
    """Stream-decompress a .zst file → dest."""
    import zstandard as zstd
    dctx = zstd.ZstdDecompressor()
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        dctx.copy_stream(fin, fout)


def _step_restore(cfg, db_path: str, backup_path: str) -> bool:
    """Replace db_path with the contents of backup_path. Makes a safety copy
    of the current DB first, then removes stale -wal/-shm sidecars so SQLite
    doesn't replay an old journal over the restored file."""
    emit("PHASE", "── Restore ──────────────────────────────────────────────", "phase")
    # 1. Safety-snapshot the CURRENT db so a restore is itself undoable.
    try:
        app.config["BACKUP_DIR"].mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safety = app.config["BACKUP_DIR"] / f"{cfg['app']}_{ts}_pre-restore.db.zst"
        _compress_file(db_path, str(safety))
        emit("OK", f"Saved pre-restore snapshot: {safety.name}", "ok")
    except Exception as e:
        emit("ERR", f"Could not snapshot current DB — aborting restore: {e}", "err")
        return False
    # 2. Write the backup over the live DB path.
    try:
        if backup_path.endswith(".zst"):
            emit("INFO", f"Decompressing {Path(backup_path).name} → {db_path}", "info")
            _decompress_file(backup_path, db_path)
        else:
            emit("INFO", f"Copying {Path(backup_path).name} → {db_path}", "info")
            shutil.copy2(backup_path, db_path)
    except Exception as e:
        emit("ERR", f"Restore failed: {e}", "err")
        return False
    # 3. Drop stale WAL/SHM sidecars from the replaced DB.
    for sidecar in (db_path + "-wal", db_path + "-shm"):
        try:
            Path(sidecar).unlink(missing_ok=True)
        except OSError:
            pass
    mb = Path(db_path).stat().st_size / 1_048_576
    emit("OK", f"Restored database ({mb:.1f} MB).", "ok")
    return True


def _restore_worker(cfg: dict) -> None:
    _job.start_time = time.time()
    _job.running    = True
    _job.aborted    = False
    _job.history    = []
    _job.result     = None

    emit("SYS", f"Starr DB Restore – job started for {cfg['app'].upper()}", "sys")
    emit("SYS", f"Backup: {Path(cfg['backup_path']).name}", "sys")
    try:
        db_path = cfg.get("db_path")
        if not db_path:
            _job.result = {"status": "error", "message": "Could not resolve the target database path."}
            return
        if not _step_shutdown(cfg):
            _job.result = {"status": "error", "message": "Shutdown failed or aborted; database not touched."}
            return
        # Defence in depth: never write over a DB the app may still hold open.
        if not cfg.get("skip_shutdown"):
            if _get_status(cfg["host"], cfg["port"], cfg["apikey"], cfg.get("urlbase", ""),
                           api=cfg.get("api", "v3")) is not None:
                emit("ERR", "App is back ONLINE before restore — aborting to protect the database.", "err")
                _step_restart(cfg, {})
                _job.result = {"status": "error", "message": "App restarted before restore; aborted"}
                return
        if not _step_restore(cfg, db_path, cfg["backup_path"]):
            _step_restart(cfg, {})
            _job.result = {"status": "error", "message": "Restore failed; app restarted"}
            return
        _step_restart(cfg, {})
        _job.result = {
            "status":  "aborted" if _job.aborted else "ok",
            "message": f"Restored {Path(cfg['backup_path']).name}",
            "elapsed": _elapsed(),
        }
    except Exception as e:
        emit("ERR", f"Unexpected error: {e}", "err")
        log.exception("Restore worker crashed")
        _job.result = {"status": "error", "message": str(e)}
    finally:
        _job.running = False
        if not any(h.get("cls") == "__done__" for h in _job.history):
            emit("__DONE__", json.dumps({
                "fixed": 0,
                "errors": 1 if (_job.result or {}).get("status") == "error" else 0,
                "elapsed": _elapsed(), "dry_run": False,
                "status": (_job.result or {}).get("status", "error"),
                "message": (_job.result or {}).get("message", ""),
            }), "__done__")
        emit("SYS", "Job finished. SSE stream remains open.", "sys")
        _notify.maybe_notify(_notify_config, cfg.get("app", "?"), _job.result or {})


def _resolve_db_path(app_name: str) -> str | None:
    """Best-effort resolve the on-disk DB path for an app (discovery first,
    then APPDATA_DIR/<app>/<dbname>)."""
    disc = _discovered_for(app_name)
    if disc.get("db_path") and Path(disc["db_path"]).exists():
        return disc["db_path"]
    dbname = APP_DEFAULTS[app_name]["dbname"]
    cand = app.config["APPDATA_DIR"] / app_name / dbname
    return str(cand) if cand.exists() else None


def _resolve_conn_lenient(cfg: dict) -> None:
    """Like _resolve_request_cfg but never errors — restore only strictly needs
    the container name (to stop) + db path (to write); url/apikey are optional
    and used only for the offline re-check and online-after-restart wait."""
    app_name = cfg["app"]
    upper = app_name.upper()
    disc = _discovered_for(app_name)
    raw_url = (cfg.get("url") or "").strip() or app.config.get(f"{upper}_URL", "") or (disc.get("url") or "")
    if raw_url:
        h, p, b = _split_url(raw_url, default_port=APP_DEFAULTS[app_name]["port"])
        cfg["host"], cfg["port"], cfg["urlbase"] = h, p, b
    else:
        cfg.setdefault("host", ""); cfg.setdefault("port", APP_DEFAULTS[app_name]["port"]); cfg.setdefault("urlbase", "")
    cfg["api"] = APP_DEFAULTS[app_name]["api"]
    cfg.setdefault("apikey", app.config.get(f"{upper}_APIKEY", ""))
    if not cfg.get("container_name"):
        cfg["container_name"] = disc.get("container_name") or ""


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
    for name in APP_DEFAULTS:
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


def _apply_instance(cfg: dict) -> str | None:
    """If the request names an instance_id, overlay that instance's connection
    onto cfg (request body still wins per field). Sets cfg['label'] — the
    filename/history key — to the instance id (or the bare app name for the
    env/discovery default). UI-saved per-instance overrides win over the
    instance's own stored fields and over env/discovery; the request body
    still wins over both. Returns an error string or None."""
    iid = (cfg.get("instance_id") or "").strip().lower()
    if iid and iid not in APP_DEFAULTS:
        inst = _instances.get(iid)
        if not inst:
            return f"unknown instance '{iid}'"
        cfg["app"] = inst["app"]
        for k in ("url", "apikey", "container_name", "db_path"):
            if inst.get(k) and not cfg.get(k):
                cfg[k] = inst[k]
    # Apply per-instance UI overrides. The lookup key is the explicit
    # instance_id when given, otherwise the bare app name — that's the id of
    # the env/discovery default instance. Without this fallback, scheduled
    # runs that target the default (which carry instance_id="") would skip
    # the override the user just saved in the dashboard and fail with the
    # generic "apikey is required" error.
    ov_key = iid or (cfg.get("app") or "").lower()
    if ov_key:
        ov = _instances.get_override(ov_key)
        for k, v in ov.items():
            if v and not cfg.get(k):
                cfg[k] = v
    cfg["label"] = iid or (cfg.get("app") or "").lower()
    return None


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
    err = _apply_instance(cfg)
    if err:
        return jsonify({"error": err}), 400
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
    # Interrupt any in-flight SQLite statement (e.g. a long VACUUM/REINDEX)
    # so the abort takes effect immediately rather than after the op finishes.
    # Connection.interrupt() is safe to call from this request thread.
    con = _job.active_conn
    interrupted = False
    if con is not None:
        try:
            con.interrupt()
            interrupted = True
        except Exception:
            log.exception("Failed to interrupt active DB connection")
    msg = ("Stop requested – interrupting the running database operation."
           if interrupted else
           "Stop requested by user – aborting after current step.")
    emit("WARN", msg, "warn")
    return jsonify({"status": "aborting", "interrupted": interrupted}), 200


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
    """List backup files (.db and .db.zst) in the backup directory."""
    backup_dir = app.config["BACKUP_DIR"]
    backups = []
    if backup_dir.exists():
        for f in sorted(backup_dir.glob("*.db*"), reverse=True):
            if not (f.name.endswith(".db") or f.name.endswith(".db.zst")):
                continue
            stat = f.stat()
            backups.append({
                "name":       f.name,
                "size_mb":    round(stat.st_size / 1_048_576, 1),
                "created":    datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "compressed": f.name.endswith(".zst"),
                "result":     ("repaired" if "_repaired." in f.name else
                               "clean" if "_clean." in f.name else
                               "aborted" if "_aborted." in f.name else None),
            })
    return jsonify(backups)


def _resolve_backup(name: str):
    """Validate a backup filename and return its safe absolute Path, or None
    if it's invalid / escapes BACKUP_DIR."""
    if name != Path(name).name or not (name.endswith(".db") or name.endswith(".db.zst")):
        return None
    backup_dir = Path(app.config["BACKUP_DIR"])
    target = (backup_dir / name).resolve()
    try:
        target.relative_to(backup_dir.resolve())
    except ValueError:
        return None
    return target


@app.route("/api/backups/<name>", methods=["DELETE"])
@require_api_key
def api_backup_delete(name):
    """Delete a single backup file (path-traversal guarded)."""
    target = _resolve_backup(name)
    if target is None:
        return jsonify({"error": "invalid backup name"}), 400
    if not target.exists():
        return jsonify({"error": "not found"}), 404
    try:
        target.unlink()
    except OSError as e:
        return jsonify({"error": f"delete failed: {e}"}), 500
    log.info("Deleted backup %s", name)
    return jsonify({"status": "deleted", "name": name})


@app.route("/api/backups/delete", methods=["POST"])
@require_api_key
def api_backups_bulk_delete():
    """Delete several backups in one call. Body: {"names": [...]}.
    Returns per-name results; invalid/missing names are reported, not fatal."""
    names = (request.get_json(force=True) or {}).get("names") or []
    if not isinstance(names, list):
        return jsonify({"error": "names must be a list"}), 400
    deleted, errors = [], {}
    for name in names:
        target = _resolve_backup(str(name))
        if target is None:
            errors[name] = "invalid name"
            continue
        if not target.exists():
            errors[name] = "not found"
            continue
        try:
            target.unlink()
            deleted.append(name)
        except OSError as e:
            errors[name] = str(e)
    log.info("Bulk-deleted %d backup(s)", len(deleted))
    return jsonify({"deleted": deleted, "errors": errors})


@app.route("/api/backups/<name>/restore", methods=["POST"])
@require_api_key
def api_backup_restore(name):
    """Restore a backup over the app's live database. Runs as a streamed job:
    stop container → snapshot current DB → write backup over it → start.
    The instance (and its app type) is inferred from the filename prefix —
    e.g. sonarr_… (default) or sonarr-4k_… (a named instance)."""
    if _job.running:
        return jsonify({"error": "A job is already running."}), 409
    target = _resolve_backup(name)
    if target is None:
        return jsonify({"error": "invalid backup name"}), 400
    if not target.exists():
        return jsonify({"error": "not found"}), 404
    label    = name.split("_", 1)[0].lower()
    app_name = _instances.app_for(label)
    if app_name not in APP_DEFAULTS:
        return jsonify({"error": f"cannot infer a known app from '{name}'"}), 400

    cfg = {"app": app_name, "label": label, "backup_path": str(target)}
    # For a named instance, use its stored connection + db path.
    inst = _instances.get(label) if label != app_name else None
    if inst:
        for k in ("url", "apikey", "container_name", "db_path"):
            if inst.get(k):
                cfg[k] = inst[k]
    if not cfg.get("db_path"):
        db_path = _resolve_db_path(app_name)
        if not db_path:
            return jsonify({"error": f"could not locate {app_name}'s database to restore into"}), 400
        cfg["db_path"] = db_path
    _resolve_conn_lenient(cfg)
    if not cfg.get("container_name") and not cfg.get("apikey"):
        return jsonify({"error": "no way to stop the app safely — need a discoverable "
                                 "container (mount docker.sock) or an apikey"}), 400

    _job.reset()
    threading.Thread(target=_restore_worker, args=(cfg,), daemon=True).start()
    return jsonify({"status": "started", "app": app_name, "backup": name}), 202


def _run_scheduled(cfg: dict) -> dict:
    """Synchronously run a scheduled repair via _repair_worker. Resolves
    host/port/urlbase/apikey/container_name/db_path the same way the
    /api/repair/start endpoint does (env + Docker discovery)."""
    if _job.running:
        return {"status": "skipped", "reason": "another job in progress"}
    sched_name = cfg.get("_schedule_name") or "schedule"
    log.info("Scheduled run firing: %s", sched_name)
    err = _apply_instance(cfg)
    if err:
        return {"status": "error", "message": err}
    cfg, err = _resolve_request_cfg(cfg)
    if err:
        return {"status": "error", "message": err}
    _repair_worker(cfg)
    return dict(_job.result or {"status": "unknown"})


from schedules import ScheduleStore, ScheduleRunner   # noqa: E402
from history import HistoryStore                       # noqa: E402
from instances import InstanceStore                    # noqa: E402
from settings import SettingsStore, MIN_RETENTION_DAYS, MAX_RETENTION_DAYS  # noqa: E402
import notify as _notify                               # noqa: E402
import atexit                                          # noqa: E402

# Notification config (Apprise + Signal). Persisted alongside schedules.
_notify_config = _notify.NotifyConfig(app.config["BACKUP_DIR"] / ".starr-notify.json")

# Persisted Starr settings (e.g. backup retention adjustable from the UI).
_settings = SettingsStore(app.config["BACKUP_DIR"] / ".starr-settings.json")

# Persistent run history (last-run pill, pre-repair estimate, trend chart).
_history = HistoryStore(app.config["BACKUP_DIR"] / ".starr-history.json")

# User-added extra instances (e.g. a second Sonarr). Defaults stay env/discovery.
_instances = InstanceStore(app.config["BACKUP_DIR"] / ".starr-instances.json",
                           APP_DEFAULTS.keys())


def _synthesized_defaults() -> list[dict]:
    """The env/discovery-derived default instance for each app (id == app),
    in the same shape the UI uses for stored instances. User-typed
    overrides from the dashboard win over env/discovery so a key typed in
    the UI survives reloads and reaches scheduled runs."""
    discovered = {d["app"]: d for d in (_discovery_cache.get("apps") or [])}
    browser_host = _request_host_only()
    out = []
    for name in APP_DEFAULTS:
        upper = name.upper()
        ov = _instances.get_override(name)
        apikey = ov.get("apikey") or app.config.get(f"{upper}_APIKEY", "")
        env_url = ov.get("url") or app.config.get(f"{upper}_URL", "")
        disc = discovered.get(name, {})
        display_url = env_url
        if not display_url and disc.get("published_port"):
            display_url = f"http://{browser_host}:{disc['published_port']}" + (disc.get("urlbase") or "")
        if not display_url:
            display_url = disc.get("url") or ""
        if not (apikey or display_url):
            continue
        out.append({
            "id":             name,
            "app":            name,
            "name":           name.capitalize(),
            "url":            display_url,
            "internal_url":   disc.get("url") or "",
            "apikey":         apikey,
            "container_name": ov.get("container_name") or disc.get("container_name") or "",
            "db_path":        ov.get("db_path") or disc.get("db_path") or "",
            "default":        True,
            "discovered":     bool(disc),
            "configured":     bool(apikey),
            "overridden":     bool(ov),
        })
    return out


@app.route("/api/instances")
@require_api_key
def api_instances():
    """All instances: synthesized env/discovery defaults (one per configured
    app, id == app) merged with user-added extras, grouped under each app.

    Each item also reports its retention picture so the UI can render the
    "keep for" picker per instance: `retention_days` is the override (or
    None if inherited), and `retention_effective_days` is what would
    actually be applied at prune time."""
    items = _synthesized_defaults()
    for s in _instances.all():
        items.append({**s, "default": False, "configured": bool(s.get("apikey"))})
    env_default = app.config["MAX_BACKUP_AGE_DAYS"]
    per_inst = _settings.instance_retention_all()
    for it in items:
        iid = it["id"]
        it["retention_days"] = per_inst.get(iid)        # None = inherit
        it["retention_effective_days"] = _settings.max_backup_age_days(
            env_default, instance=iid)
    return jsonify(items)


@app.route("/api/instances", methods=["POST"])
@require_api_key
def api_instances_add():
    try:
        return jsonify(_instances.add(request.get_json(force=True) or {})), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/instances/<iid>", methods=["PUT"])
@require_api_key
def api_instances_update(iid):
    try:
        updated = _instances.update(iid, request.get_json(force=True) or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if updated is None:
        return jsonify({"error": "not found (defaults are env/discovery-managed)"}), 404
    return jsonify(updated)


@app.route("/api/instances/<iid>", methods=["DELETE"])
@require_api_key
def api_instances_delete(iid):
    if not _instances.delete(iid):
        return jsonify({"error": "not found (defaults are env/discovery-managed)"}), 404
    return jsonify({"status": "deleted", "id": iid})


@app.route("/api/instances/<iid>/credentials", methods=["PUT"])
@require_api_key
def api_instances_set_credentials(iid):
    """Persist UI-entered credentials (apikey / url / container_name /
    db_path) for an instance — works for both the env/discovery default
    (id == app name) and named extras. Lets a user enter the apikey in
    the dashboard once and have it survive reloads and scheduled runs
    without having to set the env var."""
    iid_l = (iid or "").strip().lower()
    if not iid_l:
        return jsonify({"error": "instance id required"}), 400
    if iid_l not in APP_DEFAULTS and not _instances.get(iid_l):
        return jsonify({"error": "unknown instance"}), 404
    try:
        ov = _instances.set_override(iid_l, request.get_json(force=True) or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "ok", "id": iid_l, "override": ov})


@app.route("/api/instances/<iid>/retention", methods=["PUT"])
@require_api_key
def api_instances_set_retention(iid):
    """Set how long this instance's backups are kept before auto-pruning.
    Body: {"max_backup_age_days": 0–365} or {"max_backup_age_days": null}
    (null clears the override so the global / env value takes over)."""
    iid_l = (iid or "").strip().lower()
    if not iid_l:
        return jsonify({"error": "instance id required"}), 400
    if iid_l not in APP_DEFAULTS and not _instances.get(iid_l):
        return jsonify({"error": "unknown instance"}), 404
    body = request.get_json(force=True) or {}
    days = body.get("max_backup_age_days", body.get("days"))
    try:
        ret = _settings.set_instance_retention(iid_l, days)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({
        "id":                       iid_l,
        "retention_days":           ret.get(iid_l),   # None when cleared
        "retention_effective_days": _settings.max_backup_age_days(
            app.config["MAX_BACKUP_AGE_DAYS"], instance=iid_l),
    })


@app.route("/api/settings")
@require_api_key
def api_settings_get():
    """Return saved settings merged with effective defaults so the UI can
    show what's currently in force without having to re-derive it."""
    saved = _settings.get()
    env_default = app.config["MAX_BACKUP_AGE_DAYS"]
    return jsonify({
        "max_backup_age_days":           saved.get("max_backup_age_days", env_default),
        "max_backup_age_days_source":    "saved" if "max_backup_age_days" in saved else "env",
        "max_backup_age_days_env":       env_default,
        "max_backup_age_days_min":       MIN_RETENTION_DAYS,
        "max_backup_age_days_max":       MAX_RETENTION_DAYS,
    })


@app.route("/api/settings", methods=["PUT"])
@require_api_key
def api_settings_update():
    try:
        _settings.update(request.get_json(force=True) or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return api_settings_get()


@app.route("/api/history")
@require_api_key
def api_history():
    """Recent run records, newest first. Optional ?instance= (preferred,
    per-instance) or ?app= filter, and ?limit=."""
    instance_filter = (request.args.get("instance") or "").strip().lower() or None
    app_filter = (request.args.get("app") or "").strip().lower() or None
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 500))
    except ValueError:
        limit = 50
    return jsonify(_history.recent(app=app_filter, instance=instance_filter, limit=limit))


@app.route("/api/history/estimate")
@require_api_key
def api_history_estimate():
    """Median duration of comparable past runs. Prefers ?instance= for a
    per-instance estimate; falls back to ?app= for the app-wide median."""
    instance = (request.args.get("instance") or "").strip().lower()
    app_name = (request.args.get("app") or "").strip().lower()
    if not instance and not app_name:
        return jsonify({"error": "app or instance is required"}), 400
    return jsonify(_history.estimate(app=app_name or None,
                                     instance=instance or None))


@app.route("/api/notify")
@require_api_key
def api_notify_get():
    return jsonify(_notify_config.get())


@app.route("/api/notify", methods=["PUT"])
@require_api_key
def api_notify_update():
    try:
        return jsonify(_notify_config.update(request.get_json(force=True) or {}))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/notify/test", methods=["POST"])
@require_api_key
def api_notify_test():
    """Send a test notification. Uses the saved config, with any fields in the
    request body overlaid so users can test edits before saving."""
    body = request.get_json(silent=True) or {}
    cfg = _merge_notify_overrides(_notify_config.get(), body)
    summary = _notify.dispatch(cfg, "🔔 Starr test notification",
                               "If you can read this, notifications are wired up correctly.")
    code = 200 if not summary["errors"] else 207
    return jsonify(summary), code


def _merge_notify_overrides(saved: dict, body: dict) -> dict:
    out = dict(saved)
    out["signal"] = dict(saved.get("signal") or {})
    if "apprise_urls" in body:
        urls = body["apprise_urls"]
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.splitlines()]
        out["apprise_urls"] = [u.strip() for u in (urls or []) if u.strip()]
    if "webhook_urls" in body:
        wh = body["webhook_urls"]
        if isinstance(wh, str):
            wh = [u.strip() for u in wh.splitlines()]
        out["webhook_urls"] = [u.strip() for u in (wh or []) if u.strip()]
    if "signal" in body and isinstance(body["signal"], dict):
        s = body["signal"]
        recips = s.get("recipients", out["signal"].get("recipients", []))
        if isinstance(recips, str):
            recips = [r.strip() for r in recips.replace(",", "\n").splitlines()]
        out["signal"] = {
            "api_url":    (s.get("api_url", out["signal"].get("api_url", "")) or "").strip().rstrip("/"),
            "number":     (s.get("number", out["signal"].get("number", "")) or "").strip(),
            "recipients": [r.strip() for r in (recips or []) if r.strip()],
        }
    return out


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
