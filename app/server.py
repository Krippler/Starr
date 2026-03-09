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
import sqlite3
import threading
import time
from datetime import datetime
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
CORS(app)

# ---------------------------------------------------------------------------
# Configuration (env-vars with sane defaults)
# ---------------------------------------------------------------------------
class Config:
    SECRET_KEY        = os.environ.get("SECRET_KEY", "change-me-in-production")
    MAX_BACKUP_AGE_DAYS = int(os.environ.get("MAX_BACKUP_AGE_DAYS", "7"))
    BACKUP_DIR        = Path(os.environ.get("BACKUP_DIR", "/backups"))
    DB_DIR            = Path(os.environ.get("DB_DIR", "/data"))
    LOG_LEVEL         = os.environ.get("LOG_LEVEL", "INFO")
    # Pre-configured app connections (can be set via env or UI)
    SONARR_HOST       = os.environ.get("SONARR_HOST", "")
    SONARR_PORT       = int(os.environ.get("SONARR_PORT", "8989"))
    SONARR_APIKEY     = os.environ.get("SONARR_APIKEY", "")
    SONARR_URLBASE    = os.environ.get("SONARR_URLBASE", "")
    RADARR_HOST       = os.environ.get("RADARR_HOST", "")
    RADARR_PORT       = int(os.environ.get("RADARR_PORT", "7878"))
    RADARR_APIKEY     = os.environ.get("RADARR_APIKEY", "")
    RADARR_URLBASE    = os.environ.get("RADARR_URLBASE", "")
    LIDARR_HOST       = os.environ.get("LIDARR_HOST", "")
    LIDARR_PORT       = int(os.environ.get("LIDARR_PORT", "8686"))
    LIDARR_APIKEY     = os.environ.get("LIDARR_APIKEY", "")
    LIDARR_URLBASE    = os.environ.get("LIDARR_URLBASE", "")
    SPORTARR_HOST     = os.environ.get("SPORTARR_HOST", "")
    SPORTARR_PORT     = int(os.environ.get("SPORTARR_PORT", "1867"))
    SPORTARR_APIKEY   = os.environ.get("SPORTARR_APIKEY", "")
    SPORTARR_URLBASE  = os.environ.get("SPORTARR_URLBASE", "")

app.config.from_object(Config)
logging.getLogger().setLevel(app.config["LOG_LEVEL"])

APP_DEFAULTS = {
    "sonarr":   {"port": 8989, "dbname": "sonarr.db"},
    "radarr":   {"port": 7878, "dbname": "radarr.db"},
    "lidarr":   {"port": 8686, "dbname": "lidarr.db"},
    "sportarr": {"port": 1867, "dbname": "sportarr.db"},
}

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
# Job state
# ---------------------------------------------------------------------------
class JobState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.running     = False
        self.aborted     = False
        self.start_time  = None
        self.history     = []          # list of log entry dicts
        self.subscribers = []          # list of queue.Queue (one per SSE client)
        self.lock        = threading.Lock()
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
# Starr API helpers
# ---------------------------------------------------------------------------
def _base_url(host, port, urlbase="") -> str:
    ub = (urlbase or "").rstrip("/")
    return f"http://{host}:{port}{ub}"


def _get_status(host, port, apikey, urlbase="", timeout=5):
    try:
        url = f"{_base_url(host, port, urlbase)}/api/v3/system/status"
        r = requests.get(url, headers={"X-Api-Key": apikey}, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _shutdown_app(host, port, apikey, urlbase=""):
    try:
        url = f"{_base_url(host, port, urlbase)}/api/v3/system/shutdown"
        requests.post(url, headers={"X-Api-Key": apikey}, timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Repair steps
# ---------------------------------------------------------------------------
def _step_preflight(cfg) -> str | None:
    """Returns resolved db path or None on failure."""
    emit("PHASE", "── Step 1/5  Preflight ──────────────────────────────────", "phase")
    host, port, apikey, urlbase = cfg["host"], cfg["port"], cfg["apikey"], cfg.get("urlbase", "")
    st = _get_status(host, port, apikey, urlbase)
    if not st:
        emit("ERR", f"Cannot reach {cfg['app']} at {_base_url(host, port, urlbase)}", "err")
        emit("ERR", "Check host / port / apikey settings.", "err")
        return None

    emit("OK",   f"Connected – {cfg['app'].capitalize()} v{st.get('version','?')} on {st.get('osName','?')}", "ok")
    app_data = st.get("appData", "")
    emit("INFO", f"App data dir: {app_data or '(unknown)'}", "info")

    db_path = cfg.get("db_path") or ""
    if not db_path:
        db_path = str(Path(app_data) / APP_DEFAULTS[cfg["app"]]["dbname"])
        emit("WARN", f"db_path not supplied, auto-detected: {db_path}", "warn")

    # Also check the container-mounted /data path
    if not Path(db_path).exists():
        mounted = app.config["DB_DIR"] / APP_DEFAULTS[cfg["app"]]["dbname"]
        if mounted.exists():
            db_path = str(mounted)
            emit("INFO", f"Using mounted path: {db_path}", "info")
        else:
            emit("ERR", f"DB file not found: {db_path}", "err")
            emit("ERR", "Mount the app's config folder to /data in the container.", "err")
            return None

    mb = Path(db_path).stat().st_size / 1_048_576
    emit("OK",   f"DB confirmed: {db_path}  ({mb:.1f} MB)", "ok")
    return db_path


def _step_shutdown(cfg) -> bool:
    emit("PHASE", "── Step 2/5  Shutdown ───────────────────────────────────", "phase")
    if cfg.get("dry_run"):
        emit("DRY", "[DRY] Would POST /api/v3/system/shutdown", "dry"); return True
    if cfg.get("skip_shutdown"):
        emit("WARN", "Skipping shutdown (skip_shutdown=true).", "warn"); return True

    host, port, apikey, urlbase = cfg["host"], cfg["port"], cfg["apikey"], cfg.get("urlbase","")
    emit("INFO", f"Sending shutdown to {cfg['app'].capitalize()}...", "info")
    _shutdown_app(host, port, apikey, urlbase)
    emit("OK",   "Shutdown command sent.", "ok")

    emit("INFO", "Polling until offline (2s intervals, 60s timeout)...", "info")
    deadline = time.time() + 60
    while time.time() < deadline:
        if _job.aborted:
            emit("WARN", "Aborted during shutdown wait.", "warn"); return False
        time.sleep(2)
        if _get_status(host, port, apikey, urlbase, timeout=2) is None:
            emit("OK",   "App is offline. Waiting 3s for file handles to close...", "ok")
            time.sleep(3)
            return True
        emit("INFO", "Still running, waiting...", "info")

    emit("ERR", "App did not stop within 60s.", "err"); return False


def _step_backup(cfg, db_path: str) -> str | None:
    emit("PHASE", "── Step 3/5  Backup ─────────────────────────────────────", "phase")
    if cfg.get("no_backup"):
        emit("WARN", "Backup skipped (no_backup=true).", "warn"); return None

    app.config["BACKUP_DIR"].mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = app.config["BACKUP_DIR"] / f"{cfg['app']}_{ts}.db"

    if cfg.get("dry_run"):
        emit("DRY", f"[DRY] Would copy {db_path} → {dest}", "dry"); return str(dest)

    import shutil
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
    emit("PHASE", "── Step 4/5  SQLite Repairs ─────────────────────────────", "phase")
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
    emit("PHASE", "── Step 5/5  Report ─────────────────────────────────────", "phase")
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
        emit("WARN", f"Restart {cfg['app'].capitalize()} to bring it back online.", "warn")
        emit("SYS",  f"  docker restart {cfg['app']}", "sys")
        emit("SYS",   "  Or restart via your Unraid / NAS app manager.", "sys")

    ok_n  = sum(1 for s, _ in results.values() if s in ("ok","fixed")) if not cfg.get("dry_run") else len(results)
    err_n = sum(1 for s, _ in results.values() if s in ("issues","error")) if not cfg.get("dry_run") else 0
    emit("__DONE__", json.dumps({
        "fixed":   ok_n,
        "errors":  err_n,
        "elapsed": _elapsed(),
        "dry_run": cfg.get("dry_run", False),
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

        if not _step_shutdown(cfg):
            _job.result = {"status": "error", "message": "Shutdown failed or aborted"}
            return

        backup = _step_backup(cfg, db_path)

        results = _step_repair(cfg, db_path)

        _step_report(cfg, backup, results)

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
        emit("SYS", "Job finished. SSE stream remains open.", "sys")


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", config=app.config)


@app.route("/healthz")
def healthz():
    """Docker / k8s liveness probe."""
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()}), 200


@app.route("/readyz")
def readyz():
    """Docker / k8s readiness probe – always ready once Flask is up."""
    return jsonify({"status": "ready"}), 200


@app.route("/api/apps")
def api_apps():
    """Return pre-configured app connections."""
    apps = []
    for name in ("sonarr", "radarr", "lidarr", "sportarr"):
        host = app.config.get(f"{name.upper()}_HOST", "")
        if host:
            apps.append({
                "app":     name,
                "host":    host,
                "port":    app.config.get(f"{name.upper()}_PORT"),
                "apikey":  "***" if app.config.get(f"{name.upper()}_APIKEY") else "",
                "configured": bool(app.config.get(f"{name.upper()}_APIKEY")),
            })
    return jsonify(apps)


@app.route("/api/repair/start", methods=["POST"])
def api_start():
    if _job.running:
        return jsonify({"error": "A repair job is already running."}), 409

    cfg = request.get_json(force=True) or {}
    app_name = cfg.get("app", "").lower()
    if app_name not in APP_DEFAULTS:
        return jsonify({"error": "app must be sonarr, radarr, lidarr, or sportarr"}), 400

    # Merge env-configured API key if caller did not supply one
    env_key = app.config.get(f"{app_name.upper()}_APIKEY", "")
    if not cfg.get("apikey") and env_key:
        cfg["apikey"] = env_key
    if not cfg.get("host"):
        cfg["host"] = app.config.get(f"{app_name.upper()}_HOST") or "localhost"
    if not cfg.get("port"):
        cfg["port"] = app.config.get(f"{app_name.upper()}_PORT") or APP_DEFAULTS[app_name]["port"]

    if not cfg.get("apikey"):
        return jsonify({"error": "apikey is required"}), 400

    # Validate ops list
    ops = cfg.get("ops") or ALL_OPS
    invalid = [o for o in ops if o not in ALL_OPS]
    if invalid:
        return jsonify({"error": f"Unknown ops: {invalid}", "valid": ALL_OPS}), 400
    cfg["ops"] = ops

    _job.reset()
    thread = threading.Thread(target=_repair_worker, args=(cfg,), daemon=True)
    thread.start()

    return jsonify({"status": "started", "app": app_name}), 202


@app.route("/api/repair/stop", methods=["POST"])
def api_stop():
    if not _job.running:
        return jsonify({"error": "No job running."}), 409
    _job.aborted = True
    emit("WARN", "Stop requested by user – aborting after current step.", "warn")
    return jsonify({"status": "aborting"}), 200


@app.route("/api/repair/status")
def api_status():
    return jsonify({
        "running":  _job.running,
        "aborted":  _job.aborted,
        "elapsed":  _elapsed(),
        "lines":    len(_job.history),
        "result":   _job.result,
    })


@app.route("/api/repair/stream")
def api_stream():
    """Server-Sent Events endpoint – streams live log entries."""
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
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/backups")
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8877"))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    log.info("Starting Starr DB Repair on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
