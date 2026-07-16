"""Notifications for completed repair jobs.

Two delivery paths, both optional:

  1. Apprise — one dependency, 100+ services (Discord, Telegram, ntfy,
     Pushover, Slack, gotify, email, Home Assistant, …). Configured as a
     list of Apprise URLs.
  2. Signal — posts directly to a signal-cli-rest-api server
     (https://github.com/bbernhard/signal-cli-rest-api) via its
     POST /v2/send endpoint. Kept as a first-class path (rather than via
     Apprise's signal:// plugin) so the exact REST contract is explicit.
  3. Webhook — POSTs the full machine-readable result (app, status,
     fixed, errors, elapsed, backup, scheduled, schedule_name, …) as
     JSON to one or more URLs. For wiring Starr into home automation,
     dashboards, or another tool's intake.

Config is persisted as JSON next to the schedules file in BACKUP_DIR and
is editable through the dashboard. The whole subsystem is opt-in: with no
URLs and no Signal config, nothing is ever sent.

Notify levels (lowest → highest verbosity):
    off      never
    error    only failed runs
    warning  failed runs + runs that found/needed repairs
    always   every completed run, including clean/ok
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("starr-repair.notify")

LEVELS = ("off", "error", "warning", "always")

# Map a job result status onto a severity bucket used by should_notify().
#   ok / clean  -> "ok"      (only sent at level=always)
#   warning     -> "warning" (issues found / fixed)
#   error       -> "error"
#   skipped / aborted -> "ok" (operationally a no-op; only at always)
_STATUS_SEVERITY = {
    "ok": "ok", "clean": "ok", "skipped": "ok", "aborted": "ok",
    "warning": "warning",
    "error": "error",
}


def severity(status: str) -> str:
    return _STATUS_SEVERITY.get(status, "error")


def should_notify(level: str, status: str) -> bool:
    sev = severity(status)
    if level == "off":
        return False
    if level == "always":
        return True
    if level == "warning":
        return sev in ("warning", "error")
    if level == "error":
        return sev == "error"
    return False


def format_message(app: str, result: dict, *, scheduled: bool,
                   schedule_name: str | None = None) -> tuple[str, str]:
    """Return (title, body) for a finished run."""
    status = (result or {}).get("status", "unknown")
    pretty = app.capitalize()
    icon = {"ok": "✅", "clean": "✅", "warning": "⚠️", "error": "❌",
            "skipped": "⏭️", "aborted": "🛑"}.get(status, "ℹ️")
    origin = f"scheduled '{schedule_name}'" if scheduled and schedule_name else \
             ("scheduled" if scheduled else "manual")
    title = f"{icon} Starr: {pretty} repair {status}"
    lines = [f"{pretty} repair ({origin}) finished: {status}."]
    if result.get("message"):
        lines.append(result["message"])
    if result.get("fixed") is not None:
        lines.append(f"Operations passed/fixed: {result.get('fixed')}")
    if result.get("errors"):
        lines.append(f"Issues detected: {result.get('errors')}")
    if result.get("elapsed"):
        lines.append(f"Elapsed: {result.get('elapsed')}")
    if result.get("backup"):
        lines.append(f"Backup: {result.get('backup')}")
    return title, "\n".join(lines)


class NotifyConfig:
    """JSON-backed notification settings."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._defaults()
        self.load()

    @staticmethod
    def _defaults() -> dict:
        return {
            "enabled":      False,
            "level":        "error",
            "apprise_urls": [],
            "signal":       {"api_url": "", "number": "", "recipients": []},
            "webhook_urls": [],
        }

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._data = self._defaults()
                return
            try:
                loaded = json.loads(self.path.read_text())
                self._data = {**self._defaults(), **loaded}
                self._data["signal"] = {**self._defaults()["signal"],
                                        **(loaded.get("signal") or {})}
            except Exception:
                log.exception("Failed to read %s; using defaults", self.path)
                self._data = self._defaults()

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self.path)

    def get(self) -> dict:
        with self._lock:
            d = dict(self._data)
            d["signal"] = dict(self._data["signal"])
            return d

    def update(self, payload: dict) -> dict:
        with self._lock:
            cur = self.get()
            if "enabled" in payload:
                cur["enabled"] = bool(payload["enabled"])
            if "level" in payload:
                lvl = str(payload["level"]).lower()
                if lvl not in LEVELS:
                    raise ValueError(f"level must be one of {LEVELS}")
                cur["level"] = lvl
            if "apprise_urls" in payload:
                urls = payload["apprise_urls"]
                if isinstance(urls, str):
                    urls = [u.strip() for u in urls.splitlines()]
                cur["apprise_urls"] = [u.strip() for u in (urls or []) if u.strip()]
            if "webhook_urls" in payload:
                wh = payload["webhook_urls"]
                if isinstance(wh, str):
                    wh = [u.strip() for u in wh.splitlines()]
                cur["webhook_urls"] = [u.strip() for u in (wh or []) if u.strip()]
            if "signal" in payload and isinstance(payload["signal"], dict):
                sig = payload["signal"]
                recips = sig.get("recipients", cur["signal"]["recipients"])
                if isinstance(recips, str):
                    recips = [r.strip() for r in recips.replace(",", "\n").splitlines()]
                cur["signal"] = {
                    "api_url":    (sig.get("api_url", cur["signal"]["api_url"]) or "").strip().rstrip("/"),
                    "number":     (sig.get("number", cur["signal"]["number"]) or "").strip(),
                    "recipients": [r.strip() for r in (recips or []) if r.strip()],
                }
            self._data = cur
            self.save()
            return self.get()


# ── Senders ───────────────────────────────────────────────────────────────────
# Apprise's notify() takes no timeout and many of its plugins fall back to
# requests with timeout=None, so a black-holed target would block forever.
# Since notifications run inline on the repair worker's finally block, an
# unbounded send would wedge that thread (and its fd) permanently. Cap it.
APPRISE_TIMEOUT = int(os.environ.get("APPRISE_TIMEOUT_SECONDS", "30"))


def _run_bounded(fn, timeout: float) -> tuple[Any, bool]:
    """Run fn() on a daemon thread and wait up to `timeout` seconds.
    Returns (result_or_None, timed_out). On timeout we stop waiting and let
    the worker proceed — the orphaned daemon thread can't block process exit
    and is reaped when its own socket timeout finally fires."""
    box: dict[str, Any] = {}
    def _target():
        try:
            box["result"] = fn()
        except BaseException as e:   # noqa: BLE001 — never let it escape the thread
            box["error"] = e
    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, True
    if "error" in box:
        raise box["error"]
    return box.get("result"), False


def _send_apprise(urls: list[str], title: str, body: str) -> tuple[int, list[str]]:
    """Returns (count_sent, errors)."""
    if not urls:
        return 0, []
    try:
        import apprise
    except ImportError:
        return 0, ["apprise not installed"]
    ap = apprise.Apprise()
    added = 0
    errors = []
    for u in urls:
        if ap.add(u):
            added += 1
        else:
            errors.append(f"invalid apprise url: {u.split('://',1)[0]}://…")
    if added:
        try:
            ok, timed_out = _run_bounded(lambda: ap.notify(title=title, body=body), APPRISE_TIMEOUT)
        except BaseException as e:   # noqa: BLE001 — a plugin blew up; report, don't crash
            ok, timed_out = False, False
            errors.append(f"apprise notify error: {e}")
        if timed_out:
            errors.append(f"apprise notify timed out after {APPRISE_TIMEOUT}s")
        elif not ok:
            errors.append("apprise notify failed for one or more targets")
    return added, errors


def _send_signal(sig: dict, message: str) -> tuple[int, list[str]]:
    """POST to signal-cli-rest-api /v2/send. Returns (count_sent, errors)."""
    api_url    = (sig.get("api_url") or "").strip().rstrip("/")
    number     = (sig.get("number") or "").strip()
    recipients = [r for r in (sig.get("recipients") or []) if r]
    if not (api_url and number and recipients):
        return 0, []
    try:
        r = requests.post(
            f"{api_url}/v2/send",
            json={"message": message, "number": number, "recipients": recipients},
            timeout=15,
        )
        if r.status_code in (200, 201):
            return len(recipients), []
        return 0, [f"signal-cli-rest-api HTTP {r.status_code}: {r.text[:200]}"]
    except requests.RequestException as e:
        return 0, [f"signal send failed: {e}"]


def _send_webhook(urls: list[str], payload: dict) -> tuple[int, list[str]]:
    """POST a JSON payload to each configured URL. Returns (count_sent, errors)."""
    if not urls:
        return 0, []
    sent, errors = 0, []
    for url in urls:
        try:
            r = requests.post(url, json=payload, timeout=10)
            if 200 <= r.status_code < 300:
                sent += 1
            else:
                errors.append(f"webhook {url} HTTP {r.status_code}")
        except requests.RequestException as e:
            errors.append(f"webhook {url} failed: {e}")
    return sent, errors


def dispatch(config: dict, title: str, body: str,
             webhook_payload: dict | None = None) -> dict:
    """Send a notification through every configured channel. Never raises.
    Returns a summary dict (sent count + errors) for the /test endpoint."""
    sent, errors = 0, []
    # NOTE: catch BaseException, not just Exception — a flaky Apprise plugin can
    # raise a Rust pyo3 PanicException (subclass of BaseException) on import.
    # Notifications must never crash the request thread or a repair job.
    try:
        n, errs = _send_apprise(config.get("apprise_urls") or [], title, body)
        sent += n
        errors += errs
    except BaseException as e:        # noqa: BLE001 — deliberate, see note above
        errors.append(f"apprise error: {e}")
    try:
        # Signal gets the title prepended since it has no separate subject.
        n, errs = _send_signal(config.get("signal") or {}, f"{title}\n\n{body}")
        sent += n
        errors += errs
    except BaseException as e:        # noqa: BLE001
        errors.append(f"signal error: {e}")
    try:
        # Webhooks get a structured JSON payload (or, for the test endpoint,
        # a synthetic one) — useful for integrating with home automation, etc.
        wh_payload = webhook_payload or {"event": "test", "title": title, "body": body}
        n, errs = _send_webhook(config.get("webhook_urls") or [], wh_payload)
        sent += n
        errors += errs
    except BaseException as e:        # noqa: BLE001
        errors.append(f"webhook error: {e}")
    return {"sent": sent, "errors": errors}


def maybe_notify(config_store: "NotifyConfig", app: str, result: dict, *,
                 level_override: str | None = None, scheduled: bool = False,
                 schedule_name: str | None = None) -> None:
    """Evaluate config + result and fire a notification if warranted.
    Swallows all errors — notifications must never break a repair."""
    try:
        cfg = config_store.get()
        if not cfg.get("enabled"):
            return
        level = level_override or cfg.get("level", "error")
        if level == "inherit":
            level = cfg.get("level", "error")
        status = (result or {}).get("status", "error")
        if not should_notify(level, status):
            return
        title, body = format_message(app, result, scheduled=scheduled,
                                     schedule_name=schedule_name)
        webhook_payload = {
            "event":         "repair_complete",
            "app":           app,
            "status":        status,
            "fixed":         result.get("fixed"),
            "errors":        result.get("errors"),
            "elapsed":       result.get("elapsed"),
            "backup":        result.get("backup"),
            "message":       result.get("message"),
            "scheduled":     scheduled,
            "schedule_name": schedule_name,
            "title":         title,
            "body":          body,
        }
        summary = dispatch(cfg, title, body, webhook_payload=webhook_payload)
        if summary["errors"]:
            log.warning("Notification errors: %s", summary["errors"])
        else:
            log.info("Notification sent (%d target[s])", summary["sent"])
    except Exception:
        log.exception("maybe_notify failed (ignored)")
