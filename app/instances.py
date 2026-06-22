"""Per-app instance store for Starr.

The original model assumed exactly one of each *arr app, configured via
env vars (SONARR_URL / SONARR_APIKEY …) and/or Docker discovery. That
single, env/discovery-derived connection is the app's "default instance".

This store adds *additional* named instances of the same app type — e.g. a
second Sonarr at a different URL ("sonarr-4k", "sonarr-anime"). Defaults are
NOT stored here (they stay env/discovery-driven so existing installs keep
working untouched); only user-added extras live in this JSON file.

Instance id rules (important — backup filenames are prefixed with the id and
the app *type* is later inferred from that prefix):
  * the default instance's id is the bare app name ("sonarr");
  * every stored extra's id is "<app>-<slug>" — it always contains a hyphen,
    so it can never collide with a bare app-name default, and the app type is
    recoverable as the part before the first hyphen.
"""

import json
import logging
import re
import threading
import uuid
from pathlib import Path

log = logging.getLogger("starr-repair.instances")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s


class InstanceStore:
    """JSON-backed store of user-added extra instances. Thread-safe."""

    def __init__(self, path: Path, valid_apps):
        self.path = Path(path)
        # Per-instance credential overrides (apikey / url / container_name /
        # db_path) that the user typed into the dashboard. Lets the synthesized
        # env-derived default instance pick up a UI-entered apikey so it
        # survives reloads and is available to scheduled runs.
        self.overrides_path = self.path.with_name(".starr-instance-overrides.json")
        self.valid_apps = tuple(valid_apps)
        self._lock = threading.RLock()
        self._items: list[dict] = []
        self._overrides: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text())
                    self._items = data if isinstance(data, list) else []
                except Exception:
                    log.exception("Failed to read %s; starting empty", self.path)
                    self._items = []
            else:
                self._items = []
            if self.overrides_path.exists():
                try:
                    data = json.loads(self.overrides_path.read_text())
                    self._overrides = data if isinstance(data, dict) else {}
                except Exception:
                    log.exception("Failed to read %s; starting empty",
                                  self.overrides_path)
                    self._overrides = {}
            else:
                self._overrides = {}

    def _save_overrides(self) -> None:
        with self._lock:
            tmp = self.overrides_path.with_suffix(self.overrides_path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._overrides, indent=2))
            tmp.replace(self.overrides_path)

    def get_override(self, iid: str) -> dict:
        """Per-instance UI-entered credentials. Empty dict if none."""
        with self._lock:
            return dict(self._overrides.get((iid or "").lower(), {}))

    def set_override(self, iid: str, fields: dict) -> dict:
        """Persist apikey/url/container_name/db_path overrides for an instance
        (default or extra). Only known keys are stored; blanks are stripped so
        clearing a field falls back to env/discovery rather than masking it
        with an empty string."""
        iid = (iid or "").lower()
        if not iid:
            raise ValueError("instance id required")
        clean = {
            k: (fields.get(k) or "").strip()
            for k in ("apikey", "url", "container_name", "db_path")
            if k in fields
        }
        clean = {k: v for k, v in clean.items() if v}
        with self._lock:
            if clean:
                self._overrides[iid] = clean
            else:
                self._overrides.pop(iid, None)
        self._save_overrides()
        return dict(self._overrides.get(iid, {}))

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._items, indent=2))
            tmp.replace(self.path)

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(s) for s in self._items]

    def get(self, iid: str) -> dict | None:
        with self._lock:
            for s in self._items:
                if s["id"] == iid:
                    return dict(s)
        return None

    def add(self, payload: dict) -> dict:
        inst = self._normalise(payload, existing_id=None)
        with self._lock:
            inst["id"] = self._unique_id(inst["app"], payload.get("name") or inst["app"])
            self._items.append(inst)
        self.save()
        return dict(inst)

    def update(self, iid: str, payload: dict) -> dict | None:
        with self._lock:
            for i, s in enumerate(self._items):
                if s["id"] == iid:
                    merged = {**s, **payload, "id": iid, "app": s["app"]}
                    merged = self._normalise(merged, existing_id=iid)
                    self._items[i] = merged
                    self.save()
                    return dict(merged)
        return None

    def delete(self, iid: str) -> bool:
        with self._lock:
            n = len(self._items)
            self._items = [s for s in self._items if s["id"] != iid]
            if len(self._items) != n:
                self.save()
                return True
        return False

    def app_for(self, iid: str) -> str | None:
        """Map an instance id to its app type. Used to interpret a backup
        filename prefix back into a known app."""
        iid = (iid or "").lower()
        if iid in self.valid_apps:
            return iid
        inst = self.get(iid)
        if inst:
            return inst["app"]
        head = iid.split("-", 1)[0]
        return head if head in self.valid_apps else None

    # ── internals ─────────────────────────────────────────────────────────
    def _unique_id(self, app_name: str, name: str) -> str:
        base = f"{app_name}-{slugify(name) or uuid.uuid4().hex[:6]}"
        existing = {s["id"] for s in self._items}
        if base not in existing and base != app_name:
            return base
        for n in range(2, 1000):
            cand = f"{base}-{n}"
            if cand not in existing:
                return cand
        return f"{app_name}-{uuid.uuid4().hex[:8]}"

    def _normalise(self, payload: dict, *, existing_id: str | None) -> dict:
        app_name = (payload.get("app") or "").lower()
        if app_name not in self.valid_apps:
            raise ValueError(f"app must be one of {self.valid_apps}")
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        url = (payload.get("url") or "").strip()
        if not url:
            raise ValueError("url is required")
        return {
            "id":             existing_id or "",
            "app":            app_name,
            "name":           name,
            "url":            url,
            "apikey":         (payload.get("apikey") or "").strip(),
            "container_name": (payload.get("container_name") or "").strip(),
            "db_path":        (payload.get("db_path") or "").strip(),
        }
