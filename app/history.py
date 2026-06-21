"""Persistent run history for Starr repair jobs.

Every completed repair (manual or scheduled) appends one record to a hidden
JSON file inside BACKUP_DIR — same "no new mount" trick the scheduler and
notify config use. The history powers three UI features:

  * the "last run" pill on the dashboard / schedule rows,
  * a pre-repair time estimate (median duration of comparable past runs),
  * a small DB-size / duration trend chart.

We keep a rolling cap (newest MAX_RECORDS) so the file can't grow without
bound on a long-lived install.
"""

import json
import logging
import statistics
import threading
import time
from pathlib import Path

log = logging.getLogger("starr-repair.history")

MAX_RECORDS = 500


class HistoryStore:
    """JSON-backed append-only run log. Thread-safe; newest entries last."""

    def __init__(self, path: Path, cap: int = MAX_RECORDS):
        self.path = Path(path)
        self.cap = cap
        self._lock = threading.RLock()
        self._items: list[dict] = []
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._items = []
                return
            try:
                data = json.loads(self.path.read_text())
                self._items = data if isinstance(data, list) else []
            except Exception:
                log.exception("Failed to read %s; starting empty", self.path)
                self._items = []

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._items, indent=2))
            tmp.replace(self.path)

    def record(self, entry: dict) -> dict:
        """Append a run record. Fills in `ts` if absent. Best-effort: a
        write failure is logged, never raised (history is non-critical)."""
        rec = dict(entry)
        rec.setdefault("ts", time.time())
        try:
            with self._lock:
                self._items.append(rec)
                if len(self._items) > self.cap:
                    self._items = self._items[-self.cap:]
            self.save()
        except Exception:
            log.exception("Failed to record run history entry")
        return rec

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._items]

    def recent(self, app: str | None = None, instance: str | None = None,
               limit: int = 50) -> list[dict]:
        """Newest-first list, optionally filtered to one app or one instance.

        For pre-multi-instance records (no `instance` field), the bare app
        name is treated as their implicit instance label so the default
        instance's history still shows past runs from before the upgrade.
        """
        with self._lock:
            items = list(self._items)
        if instance:
            instance = instance.lower()
            items = [
                e for e in items
                if (e.get("instance") or (e.get("app") or "")).lower() == instance
            ]
        elif app:
            app = app.lower()
            items = [e for e in items if (e.get("app") or "").lower() == app]
        items.reverse()
        return [dict(e) for e in items[:limit]]

    def estimate(self, app: str | None = None, instance: str | None = None,
                 sample: int = 10) -> dict:
        """Predict how long a repair will take from past real runs.

        Considers only completed, non-dry-run repairs that actually did work
        (status ok/warning) — skip-if-clean and errored runs aren't
        representative. When `instance` is given the median is per-instance
        (with the same legacy-records fallback as recent()); otherwise it
        falls back to the per-app median. Returns sample size so the UI can
        phrase confidence ("~2m, based on 4 runs")."""
        with self._lock:
            items = list(self._items)
        if instance:
            instance_l = instance.lower()
            def match(e):
                return (e.get("instance") or (e.get("app") or "")).lower() == instance_l
        else:
            app_l = (app or "").lower()
            def match(e):
                return (e.get("app") or "").lower() == app_l
        durations = [
            e["duration_s"]
            for e in items
            if match(e)
            and not e.get("dry_run")
            and e.get("status") in ("ok", "warning")
            and isinstance(e.get("duration_s"), (int, float))
            and e["duration_s"] > 0
        ]
        durations = durations[-sample:]
        out = {"seconds": None, "samples": 0}
        if instance:
            out["instance"] = instance
        if app:
            out["app"] = app.lower()
        if durations:
            out["seconds"] = round(statistics.median(durations), 1)
            out["samples"] = len(durations)
        return out

    def last(self, app: str | None = None,
             instance: str | None = None) -> dict | None:
        """Most recent record (optionally for one app or instance), or None."""
        recent = self.recent(app=app, instance=instance, limit=1)
        return recent[0] if recent else None
