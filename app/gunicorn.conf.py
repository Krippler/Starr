"""Gunicorn configuration.

Sole purpose: keep the access log readable by dropping the container's own
health probes. Docker's HEALTHCHECK hits ``GET /healthz`` every 30s (and any
readiness probe hits ``/readyz``); those internal 200s would otherwise flood
the log a couple of times a minute. Every real request is still logged, and
the health check itself is unaffected — this only filters logging, not the
response.
"""

import logging


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


def post_fork(server, worker):
    # Applied per worker (after fork) so the filter is attached to the logger
    # that actually emits the access records.
    logging.getLogger("gunicorn.access").addFilter(_HealthProbeFilter())
