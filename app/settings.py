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

    def max_backup_age_days(self, env_default: int) -> int:
        """Stored value wins; env default falls in when nothing saved."""
        with self._lock:
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
