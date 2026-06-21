"""Persistent cron-style scheduler for Starr repair jobs.

Schedules are stored as a hidden JSON file inside BACKUP_DIR (so we don't
need a new mount point on existing installs). Each schedule defines an
app, the ops to run, a cron expression, and behaviour flags
(skip_if_clean, dry_run, container_name, …).

The scheduler runs in-process via APScheduler's BackgroundScheduler.
gunicorn launches a single worker (see Dockerfile CMD), so one scheduler
per container is the right model. We deliberately do NOT use APScheduler's
SQLite jobstore — we keep our own JSON so users can hand-edit it.

A scheduled run that fires while another job is in progress is skipped
with a SYS log line; queueing is intentionally not supported.
"""

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("starr-repair.schedules")

VALID_OPS = ("integrity", "foreign_keys", "wal_checkpoint",
             "vacuum", "reindex", "analyze")
VALID_APPS = ("sonarr", "radarr", "lidarr", "sportarr")


def _parse_cron(expr: str) -> CronTrigger:
    """Accept standard 5-field cron (minute hour dom month dow). Raises
    ValueError with a friendly message on bad input."""
    parts = (expr or "").strip().split()
    if len(parts) != 5:
        raise ValueError("cron must have 5 fields: 'minute hour day month weekday'")
    try:
        return CronTrigger.from_crontab(expr.strip())
    except Exception as e:
        raise ValueError(f"invalid cron expression: {e}") from e


class ScheduleStore:
    """JSON-backed schedule store. Thread-safe for the small operations we do."""

    def __init__(self, path: Path):
        self.path = Path(path)
        # RLock so add/update/delete can hold the lock and then call save()
        # without self-deadlocking.
        self._lock = threading.RLock()
        self._items: list[dict] = []
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._items = []
                return
            try:
                self._items = json.loads(self.path.read_text())
            except Exception:
                log.exception("Failed to read %s; starting with empty list", self.path)
                self._items = []

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._items, indent=2))
            tmp.replace(self.path)

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(s) for s in self._items]

    def get(self, sid: str) -> dict | None:
        with self._lock:
            for s in self._items:
                if s["id"] == sid:
                    return dict(s)
        return None

    def add(self, payload: dict) -> dict:
        sched = self._normalise(payload, new=True)
        with self._lock:
            self._items.append(sched)
        self.save()
        return dict(sched)

    def update(self, sid: str, payload: dict) -> dict | None:
        with self._lock:
            for i, s in enumerate(self._items):
                if s["id"] == sid:
                    merged = {**s, **payload, "id": sid}
                    merged = self._normalise(merged, new=False)
                    self._items[i] = merged
                    self.save()
                    return dict(merged)
        return None

    def delete(self, sid: str) -> bool:
        with self._lock:
            n = len(self._items)
            self._items = [s for s in self._items if s["id"] != sid]
            if len(self._items) != n:
                self.save()
                return True
        return False

    def record_run(self, sid: str, summary: dict) -> None:
        """Stamp the schedule with the result of its most recent run."""
        with self._lock:
            for s in self._items:
                if s["id"] == sid:
                    s["last_run"] = summary
                    s["last_status"] = summary.get("status", "unknown")
                    break
            else:
                return
        self.save()

    # ── Validation ───────────────────────────────────────────────────────
    @staticmethod
    def _normalise(payload: dict, *, new: bool) -> dict:
        app_name = (payload.get("app") or "").lower()
        if app_name not in VALID_APPS:
            raise ValueError(f"app must be one of {VALID_APPS}")
        ops = payload.get("ops") or []
        if not isinstance(ops, list) or not ops:
            raise ValueError("ops must be a non-empty list")
        bad = [o for o in ops if o not in VALID_OPS]
        if bad:
            raise ValueError(f"unknown ops: {bad}")
        cron = (payload.get("cron") or "").strip()
        _parse_cron(cron)   # validates
        name = (payload.get("name") or "").strip() or f"{app_name} schedule"
        return {
            "id":             payload.get("id") or uuid.uuid4().hex[:12],
            "name":           name,
            "app":            app_name,
            "ops":            ops,
            "cron":           cron,
            "enabled":        bool(payload.get("enabled", True)),
            "skip_if_clean":  bool(payload.get("skip_if_clean", True)),
            "dry_run":        bool(payload.get("dry_run", False)),
            # Per-schedule notify level: inherit | off | error | warning | always
            "notify":         (payload.get("notify") or "inherit").lower(),
            "container_name": (payload.get("container_name") or "").strip(),
            "last_run":       payload.get("last_run"),
            "last_status":    payload.get("last_status"),
            "created_at":     payload.get("created_at") or time.time() if new else payload.get("created_at"),
        }


class ScheduleRunner:
    """Wires a ScheduleStore to APScheduler and a `run_one` callback that
    actually performs the repair (the existing _repair_worker)."""

    def __init__(self, store: ScheduleStore,
                 run_one: Callable[[dict], dict],
                 is_busy: Callable[[], bool]):
        self.store   = store
        self.run_one = run_one      # called as run_one(cfg) -> result dict
        self.is_busy = is_busy
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._scheduler.start()
        self.reload()

    # ── Lifecycle ────────────────────────────────────────────────────────
    def reload(self) -> None:
        """Drop all jobs and re-add from the current store."""
        for j in list(self._scheduler.get_jobs()):
            j.remove()
        for sched in self.store.all():
            if sched.get("enabled"):
                try:
                    self._schedule_job(sched)
                except Exception as e:
                    log.warning("Skipping schedule %s: %s", sched.get("id"), e)

    def _schedule_job(self, sched: dict) -> None:
        trigger = _parse_cron(sched["cron"])
        self._scheduler.add_job(
            self._fire, trigger, args=[sched["id"]],
            id=sched["id"], replace_existing=True, max_instances=1,
            misfire_grace_time=300,
        )

    def next_run_for(self, sid: str) -> str | None:
        job = self._scheduler.get_job(sid)
        return job.next_run_time.isoformat() if job and job.next_run_time else None

    # ── Job execution ────────────────────────────────────────────────────
    def _fire(self, sid: str) -> None:
        sched = self.store.get(sid)
        if not sched or not sched.get("enabled"):
            return
        if self.is_busy():
            log.info("Schedule %s skipped: another job is in progress.", sid)
            self.store.record_run(sid, {
                "status": "skipped",
                "reason": "another job in progress",
                "ts":     time.time(),
            })
            return
        cfg = {
            "app":             sched["app"],
            "ops":             list(sched["ops"]),
            "container_name":  sched.get("container_name") or "",
            "dry_run":         bool(sched.get("dry_run")),
            "skip_if_clean":   bool(sched.get("skip_if_clean")),
            "notify":          sched.get("notify") or "inherit",
            "_scheduled":      True,
            "_schedule_id":    sid,
            "_schedule_name":  sched.get("name"),
        }
        try:
            result = self.run_one(cfg)
        except Exception as e:
            log.exception("Schedule %s crashed", sid)
            result = {"status": "error", "message": str(e)}
        result.setdefault("ts", time.time())
        self.store.record_run(sid, result)

    def run_now(self, sid: str) -> bool:
        """Fire a schedule immediately (off-cron)."""
        sched = self.store.get(sid)
        if not sched:
            return False
        threading.Thread(target=self._fire, args=(sid,), daemon=True).start()
        return True
