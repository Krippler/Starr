# 🛠 Starr DB Repair

[![Docker Pulls](https://img.shields.io/docker/pulls/Krippler52/Starr?style=flat-square&logo=docker)](https://hub.docker.com/r/Krippler52/Starr)
[![Docker Image Size](https://img.shields.io/docker/image-size/Krippler52/Starr/latest?style=flat-square)](https://hub.docker.com/r/Krippler52/Starr)
[![GitHub release](https://img.shields.io/github/v/release/Krippler/Starr?style=flat-square)](https://github.com/Krippler/Starr/releases)
[![CI](https://github.com/Krippler/Starr/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Krippler/Starr/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)

**Web UI tool for diagnosing and repairing Sonarr, Radarr, and Lidarr SQLite databases.**

> Safely shuts down your app, creates a timestamped backup, runs SQLite PRAGMAs on the idle database, streams every log line live to the browser, then reminds you to restart.

![Dashboard screenshot](docs/screenshot.png)

---

## ✨ Features

- **Browser dashboard** — no SSH required
- **Live log streaming** via Server-Sent Events (SSE)
- **Safe shutdown sequence** — calls `/api/v3/system/shutdown` and polls until confirmed offline before touching the DB
- **Auto-backup** before every repair, with configurable retention (default 7 days)
- **6 SQLite operations**: integrity check, FK repair, WAL checkpoint, VACUUM, REINDEX, ANALYZE
- **Dry-run mode** — preview every step without making changes
- **Supports all three Starr apps** — Sonarr · Radarr · Lidarr
- **Multi-arch Docker image** — `linux/amd64` + `linux/arm64` (Unraid, Synology, RPi)
- **Unraid Community Apps template** included

---

## 🚀 Quick Start

### Docker Compose (recommended)

```bash
git clone https://github.com/Krippler/Starr.git
cd Starr
cp .env.example .env       # edit with your paths & API keys
docker compose up -d
```

Open **http://localhost:8877**

### Docker CLI

```bash
docker run -d \
  --name Starr \
  --restart unless-stopped \
  -p 8877:8877 \
  -v /mnt/user/appdata/sonarr:/data/sonarr \
  -v /mnt/user/appdata/radarr:/data/radarr \
  -v /mnt/user/appdata/lidarr:/data/lidarr \
  -v /mnt/user/appdata/Starr/backups:/backups \
  -e SONARR_HOST=sonarr \
  -e SONARR_APIKEY=your-api-key \
  Krippler52/Starr:latest
```

---

## 🗂 Volume Mounts

| Container path | Purpose |
|---|---|
| `/data/sonarr` | Sonarr config directory (must contain `sonarr.db`) |
| `/data/radarr` | Radarr config directory (must contain `radarr.db`) |
| `/data/lidarr` | Lidarr config directory (must contain `lidarr.db`) |
| `/backups`     | Backup output — timestamped `.db` copies stored here |

> **Mount mode:** `rw` is required so the container can read the DB and write the backup.  
> The original `.db` is never modified until the app is shut down.

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8877` | Web UI listen port |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG` `INFO` `WARNING` `ERROR` |
| `MAX_BACKUP_AGE_DAYS` | `7` | Days to keep backups before auto-pruning |
| `BACKUP_DIR` | `/backups` | Backup directory inside the container |
| `SONARR_HOST` | _(blank)_ | Sonarr hostname or IP |
| `SONARR_PORT` | `8989` | Sonarr HTTP port |
| `SONARR_APIKEY` | _(blank)_ | Sonarr API key _(masked in template)_ |
| `SONARR_URLBASE` | _(blank)_ | Sonarr URL base, e.g. `/sonarr` |
| `RADARR_HOST` | _(blank)_ | Radarr hostname or IP |
| `RADARR_PORT` | `7878` | Radarr HTTP port |
| `RADARR_APIKEY` | _(blank)_ | Radarr API key |
| `RADARR_URLBASE` | _(blank)_ | Radarr URL base |
| `LIDARR_HOST` | _(blank)_ | Lidarr hostname or IP |
| `LIDARR_PORT` | `8686` | Lidarr HTTP port |
| `LIDARR_APIKEY` | _(blank)_ | Lidarr API key |
| `LIDARR_URLBASE` | _(blank)_ | Lidarr URL base |

All connection settings can also be entered directly in the web UI — env vars just pre-fill the fields.

---

## 🔧 Repair Operations

| Operation | Safe? | Description |
|---|---|---|
| **Integrity Check** | ✅ | `PRAGMA integrity_check` — full page-level scan for corruption |
| **Foreign Keys** | ✅ | `PRAGMA foreign_key_check` — find and remove orphaned FK rows |
| **WAL Checkpoint** | ✅ | `PRAGMA wal_checkpoint(TRUNCATE)` — flush write-ahead log to main file |
| **VACUUM** | ✅ | Defragments the database and reclaims free pages |
| **REINDEX** | ✅ | Drops and rebuilds every index from scratch |
| **ANALYZE** | ✅ | Updates query-planner statistics |

---

## 🔄 Repair Sequence

```
1. Preflight   →  Connect to app API, verify DB file exists
2. Shutdown    →  POST /api/v3/system/shutdown, poll until offline
3. Backup      →  Copy .db → /backups/appname_YYYYMMDD_HHMMSS.db
4. SQLite ops  →  Run selected PRAGMAs on the idle file
5. Report      →  Summary + restart reminder
```

After the repair completes you **must restart your app manually**:

```bash
docker restart sonarr     # or radarr / lidarr
# Unraid: Apps → sonarr → Start
# systemd: systemctl restart sonarr
```

---

## 🐋 Unraid Setup

1. Open **Apps** in the Unraid UI
2. Search for **Starr DB Repair**
3. Click Install — the template pre-fills all paths and fields
4. Set your API keys in the template form _(they are masked)_
5. Click **Apply**

Or manually add the template URL in Apps → Settings:
```
https://raw.githubusercontent.com/Krippler/Starr/main/templates/unraid.xml
```

---

## 🌐 API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Dashboard web UI |
| `GET` | `/healthz` | Liveness probe `{"status":"ok"}` |
| `GET` | `/readyz` | Readiness probe |
| `GET` | `/api/apps` | Env-configured app connections |
| `POST` | `/api/repair/start` | Start a repair job (JSON body) |
| `POST` | `/api/repair/stop` | Abort the running job |
| `GET` | `/api/repair/status` | Current job state |
| `GET` | `/api/repair/stream` | SSE live log stream |
| `GET` | `/api/backups` | List backup files |

### `POST /api/repair/start` body

```json
{
  "app":           "sonarr",
  "host":          "localhost",
  "port":          8989,
  "apikey":        "YOUR_API_KEY",
  "urlbase":       "",
  "db_path":       "",
  "ops":           ["integrity","foreign_keys","wal_checkpoint","vacuum","reindex","analyze"],
  "dry_run":       false,
  "skip_shutdown": false
}
```

---

## 🏗 Development

```bash
# Clone and set up
git clone https://github.com/Krippler/Starr.git
cd Starr
python3 -m venv .venv && source .venv/bin/activate
pip install -r app/requirements.txt

# Run in dev mode
cd app
FLASK_DEBUG=true python server.py

# Build Docker image locally
docker build -t Starr:dev .
docker run -p 8877:8877 Starr:dev
```

---

## 📦 Project Layout

```
Starr/
├── app/
│   ├── server.py            # Flask backend (REST + SSE)
│   ├── requirements.txt
│   └── templates/
│       └── index.html       # Dashboard web UI
├── docker/                  # Extra Docker helpers
├── templates/
│   └── unraid.xml           # Unraid Community Apps template
├── docs/
│   └── screenshot.png
├── tests/
├── .github/
│   └── workflows/
│       └── docker-publish.yml   # CI/CD → Docker Hub + GHCR
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## 🔐 Security Notes

- The container runs as **non-root** (UID 1000)
- API keys set via env vars are **never logged or exposed** in the web UI
- The web UI has **no authentication** by default — place it behind a reverse proxy with auth if exposed beyond your LAN (Authelia, Authentik, nginx basic auth)
- The Docker image is scanned with **Trivy** on every release

---

## 📄 License

MIT — see [LICENSE](LICENSE)

---

## 🙏 Contributing

Issues and PRs welcome. Please open an issue first for significant changes.
