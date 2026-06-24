"""User-adjustable Starr settings persisted to BACKUP_DIR.

Same "no new mount" pattern as schedules / notify / instances / history.
Right now this stores only `max_backup_age_days`; future per-install knobs
go here too rather than each getting its own file.

The env var (MAX_BACKUP_AGE_DAYS) remains the boot default; values saved
here override it without needing a container restart.
"""

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("starr-repair.settings")

# Hard caps so a UI typo can't disable pruning by accident or set a value
# the dashboard's dropdown wouldn't recognise.
MAX_RETENTION_DAYS = 365   # one year
MIN_RETENTION_DAYS = 0     # 0 == keep forever


class SettingsStore:
    """JSON-backed key/value settings. Thread-safe; small surface."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._items: dict = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._items = {}
                return
            try:
                data = json.loads(self.path.read_text())
                self._items = data if isinstance(data, dict) else {}
            except Exception:
                log.exception("Failed to read %s; starting empty", self.path)
                self._items = {}

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._items, indent=2))
            tmp.replace(self.path)

    def get(self) -> dict:
        with self._lock:
            return dict(self._items)

    def update(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        with self._lock:
            if "max_backup_age_days" in payload:
                self._items["max_backup_age_days"] = _coerce_retention(
                    payload["max_backup_age_days"])
            self.save()
            return dict(self._items)

    # ── Per-instance retention ───────────────────────────────────────────
    # Each instance (the env/discovery default's id == app name, or a named
    # extra's "<app>-<slug>") can carry its own retention. Resolution at
    # prune time is: instance → global → env-var fallback. We persist these
    # under a nested "instance_retention" map rather than separate keys so
    # the JSON stays tidy.

    def instance_retention_all(self) -> dict:
        with self._lock:
            return dict(self._items.get("instance_retention", {}))

    def set_instance_retention(self, iid: str, days) -> dict:
        """Save / clear retention for a single instance. Passing days=None
        removes the override so the global / env value applies again."""
        iid = (iid or "").strip().lower()
        if not iid:
            raise ValueError("instance id required")
        with self._lock:
            ret = dict(self._items.get("instance_retention", {}))
            if days is None or days == "":
                ret.pop(iid, None)
            else:
                ret[iid] = _coerce_retention(days)
            if ret:
                self._items["instance_retention"] = ret
            else:
                self._items.pop("instance_retention", None)
            self.save()
            return dict(self._items.get("instance_retention", {}))

    def max_backup_age_days(self, env_default: int, instance: str | None = None) -> int:
        """Resolved retention for a backup. Per-instance override wins, then
        the saved global, then the boot env default. Cleared / unset values
        cleanly fall through to the next level."""
        iid = (instance or "").strip().lower()
        with self._lock:
            ret = self._items.get("instance_retention") or {}
            if iid and isinstance(ret.get(iid), int):
                return ret[iid]
            v = self._items.get("max_backup_age_days")
        return v if isinstance(v, int) else int(env_default)


def _coerce_retention(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError) as e:
        raise ValueError("max_backup_age_days must be an integer") from e
    if n < MIN_RETENTION_DAYS or n > MAX_RETENTION_DAYS:
        raise ValueError(
            f"max_backup_age_days must be between {MIN_RETENTION_DAYS} "
            f"({MIN_RETENTION_DAYS} = keep forever) and {MAX_RETENTION_DAYS}"
        )
    return n
