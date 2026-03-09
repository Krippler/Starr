# ── Stage 1: build deps ───────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
COPY app/requirements.txt .

RUN pip install --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Starr" \
      org.opencontainers.image.description="Web UI for repairing Sonarr, Radarr, and Lidarr SQLite databases" \
      org.opencontainers.image.url="https://github.com/Krippler/starr-db-repair" \
      org.opencontainers.image.source="https://github.com/Krippler/starr-db-repair" \
      org.opencontainers.image.licenses="MIT" \
      maintainer="jasoncatcher@gmail.com"

# Non-root user for security
RUN groupadd -r starr && useradd -r -g starr -u 1000 starr

# Runtime directories
RUN mkdir -p /app /data /backups /config \
 && chown -R starr:starr /app /data /backups /config

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY --chown=starr:starr app/ /app/

WORKDIR /app

# Switch to non-root
USER starr

# Expose web UI port
EXPOSE 8877

# Volume declarations
VOLUME ["/data", "/backups"]

# Health check — hits the liveness endpoint every 30s
HEALTHCHECK \
  --interval=30s \
  --timeout=5s \
  --start-period=10s \
  --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8877/healthz')" \
   || exit 1

# Environment defaults
ENV PORT=8877 \
    LOG_LEVEL=INFO \
    BACKUP_DIR=/backups \
    DB_DIR=/data \
    MAX_BACKUP_AGE_DAYS=7 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Launch with gunicorn (4 sync workers, threaded for SSE)
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8877", \
     "--workers", "1", \
     "--threads", "8", \
     "--timeout", "300", \
     "--keep-alive", "65", \
     "--log-level", "info", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "server:app"]
