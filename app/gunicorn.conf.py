"""Gunicorn configuration — two access-log filters, nothing else.

1. Drop the container's own health probes. Docker's HEALTHCHECK hits
   ``GET /healthz`` every 30s (and any readiness probe hits ``/readyz``); those
   internal 200s would otherwise flood the log a couple of times a minute.
2. Redact the Web Key. The dashboard's live log stream (EventSource, which
   can't send headers) passes it as ``?api_key=…``, and gunicorn logs the full
   request line — so the key would otherwise land in the container log, where
   anyone with access to Unraid's log viewer or a diagnostics bundle reads it.

Every real request is still logged, and responses are unaffected — this only
changes what the log says.
"""

import logging
import re

_API_KEY_RE = re.compile(r"(api_key=)[^&\s]*", re.IGNORECASE)


class _HealthProbeFilter(logging.Filter):
    """Drop gunicorn access-log lines for the health / readiness endpoints."""

    _QUIET = {"/healthz", "/readyz"}

    def filter(self, record):
        # Gunicorn passes the access-log "atoms" dict as record.args; the "U"
        # atom is the URL path without any query string — an exact, log-format-
        # independent match. Fall back to a substring check if it's absent.
        atoms = record.args
        if isinstance(atoms, dict):
            return atoms.get("U") not in self._QUIET
        msg = record.getMessage()
        return not (" /healthz " in msg or " /readyz " in msg)


class _RedactApiKeyFilter(logging.Filter):
    """Replace the value of any api_key= query parameter with "REDACTED"."""

    def filter(self, record):
        atoms = record.args
        if isinstance(atoms, dict):
            # The message is formatted lazily from these atoms: "r" is the full
            # request line, "q" the query string, "f" the referer.
            record.args = {k: _API_KEY_RE.sub(r"\1REDACTED", v) if k in ("r", "q", "f")
                           and isinstance(v, str) else v
                           for k, v in atoms.items()}
        elif isinstance(record.msg, str):
            record.msg = _API_KEY_RE.sub(r"\1REDACTED", record.msg)
        return True


def post_fork(server, worker):
    # Applied per worker (after fork) so the filters are attached to the logger
    # that actually emits the access records.
    access = logging.getLogger("gunicorn.access")
    access.addFilter(_HealthProbeFilter())
    access.addFilter(_RedactApiKeyFilter())
