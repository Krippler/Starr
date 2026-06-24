# 🛠 Starr DB Repair

[![Docker Pulls](https://img.shields.io/docker/pulls/krippler52/starr?style=flat-square&logo=docker)](https://hub.docker.com/r/krippler52/starr)
[![Docker Image Size](https://img.shields.io/docker/image-size/krippler52/starr/latest?style=flat-square)](https://hub.docker.com/r/krippler52/starr)
[![GitHub release](https://img.shields.io/github/v/release/krippler/starr?style=flat-square)](https://github.com/Krippler/Starr/releases)
[![CI](https://github.com/Krippler/Starr/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Krippler/Starr/actions)

**Web UI tool for diagnosing and repairing Sonarr, Radarr, Sportarr, and Lidarr SQLite databases.**

> Safely shuts down your app, creates a timestamped backup, runs SQLite PRAGMAs on the idle database, streams every log line live to the browser, then reminds you to restart.

---

## ✨ Features

- **Browser dashboard** — no SSH required
- **Live log streaming** via Server-Sent Events (SSE)
- **Safe shutdown sequence** — calls `/api/v3/system/shutdown` and polls until confirmed offline before touching the DB
- **Auto-backup** before every repair, with retention adjustable from the dashboard up to 1 year (or *Forever*)
- **6 SQLite operations**: integrity check, FK repair, WAL checkpoint, VACUUM, REINDEX, ANALYZE
- **Dry-run mode** — preview every step without making changes
- **Run history** — last-run pill, pre-repair time estimate, per-app DB-size / duration trend charts, and a persistent run log
- **Multiple instances per app** — manage more than one of the same *arr (e.g. a second Sonarr) with per-instance backups and schedules
- **Supports four *arr apps** — Sonarr · Radarr · Lidarr · Sportarr
- **Docker image** — `linux/amd64` (Unraid, Synology, most x86 NAS), published to Docker Hub + GHCR and signed with cosign
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
  --name starr \
  --restart unless-stopped \
  -p 8877:8877 \
  -v /mnt/user/appdata/sonarr:/data/sonarr \
  -v /mnt/user/appdata/radarr:/data/radarr \
  -v /mnt/user/appdata/lidarr:/data/lidarr \
  -v /mnt/user/appdata/starr/backups:/backups \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e SONARR_HOST=sonarr \
  -e SONARR_APIKEY=your-api-key \
  -e SONARR_CONTAINER=sonarr \
  -e SECRET_KEY=your-secret-here \
  krippler52/starr:1.0.4
```

> The `docker.sock` mount + `SONARR_CONTAINER` enable [container-managed shutdown](#-container-managed-shutdown-recommended-for-docker--unraid). Omit both to use the app's shutdown API instead.

**Image tags** — published to both Docker Hub (`krippler52/starr`) and GHCR (`ghcr.io/krippler/starr`):

| Tag | Use |
|---|---|
| `1.1.1` | exact version — recommended pin for production |
| `1.1` / `1` | floating minor / major |
| `latest` | newest release |

---

## 🗂 Volume Mounts

| Container path | Purpose |
|---|---|
| `/data/sonarr`   | Sonarr config directory (must contain `sonarr.db`) |
| `/data/radarr`   | Radarr config directory (must contain `radarr.db`) |
| `/data/lidarr`   | Lidarr config directory (must contain `lidarr.db`) |
| `/data/sportarr` | Sportarr config directory (must contain `sportarr.db`) |
| `/backups`       | Backup output — timestamped `.db` copies stored here |

> **Mount mode:** `rw` is **required** — the repair operations (VACUUM, REINDEX, foreign-key cleanup, WAL checkpoint) write directly to the source `.db`.  
> The original `.db` is never touched until the app has been shut down and a timestamped backup has been written to `/backups`. If the backup write fails (e.g. permission error on `/backups`), the repair aborts before touching the source DB.

> **Mount layout:** mount each app's config directory at `/data/<app>` (e.g. `/data/sonarr`). Starr auto-detects the database at `/data/<app>/<app>.db`, so you normally don't need to set a DB path by hand.

> **Permissions:** Starr runs as `PUID:PGID` (default `99:100` on Unraid, `1000:1000` via compose). On startup the entrypoint chowns `/backups` to that UID so backups always work. The `/data/<app>` mounts are **not** chowned (they belong to the *arr apps), so `PUID:PGID` must match — or share a group with — the owner of those config dirs.

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PUID` | `99` (Unraid) / `1000` (compose) | UID the container runs as. Must match the owner of your *arr config dirs so VACUUM/REINDEX can write the source DB. |
| `PGID` | `100` (Unraid) / `1000` (compose) | GID the container runs as. |
| `PORT` | `8877` | Web UI listen port |
| `SECRET_KEY` | _(required)_ | Web UI access key — set this to protect the dashboard |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG` `INFO` `WARNING` `ERROR` |
| `MAX_BACKUP_AGE_DAYS` | `7` | Days to keep backups before auto-pruning. Boot default; the dashboard can override (0–365; 0 = keep forever). |
| `BACKUP_DIR` | `/backups` | Backup directory inside the container |
| `SONARR_HOST` | _(blank)_ | Sonarr hostname or IP |
| `SONARR_PORT` | `8989` | Sonarr HTTP port |
| `SONARR_APIKEY` | _(blank)_ | Sonarr API key _(masked in template)_ |
| `SONARR_URLBASE` | _(blank)_ | Sonarr URL base, e.g. `/sonarr` |
| `SONARR_CONTAINER` | `sonarr` | Sonarr container name — when set + the Docker socket is mounted, Starr stops/starts the container directly (see [Container-managed shutdown](#-container-managed-shutdown-recommended-for-docker--unraid)) |
| `RADARR_HOST` | _(blank)_ | Radarr hostname or IP |
| `RADARR_PORT` | `7878` | Radarr HTTP port |
| `RADARR_APIKEY` | _(blank)_ | Radarr API key |
| `RADARR_URLBASE` | _(blank)_ | Radarr URL base |
| `RADARR_CONTAINER` | `radarr` | Radarr container name (container-managed shutdown) |
| `LIDARR_HOST` | _(blank)_ | Lidarr hostname or IP |
| `LIDARR_PORT` | `8686` | Lidarr HTTP port |
| `LIDARR_APIKEY` | _(blank)_ | Lidarr API key |
| `LIDARR_URLBASE` | _(blank)_ | Lidarr URL base |
| `LIDARR_CONTAINER` | `lidarr` | Lidarr container name (container-managed shutdown) |
| `SPORTARR_HOST`   | _(blank)_ | Sportarr hostname or IP |
| `SPORTARR_PORT`   | `1867`    | Sportarr HTTP port |
| `SPORTARR_APIKEY` | _(blank)_ | Sportarr API key |
| `SPORTARR_URLBASE`| _(blank)_ | Sportarr URL base |
| `SPORTARR_CONTAINER` | `sportarr` | Sportarr container name (container-managed shutdown) |

All connection settings can also be entered directly in the web UI — env vars just pre-fill the fields.

---

## 🐳 Container-managed shutdown (recommended for Docker / Unraid)

On any host that runs the *arr app with a **restart policy** (`--restart unless-stopped`, the Unraid default), the app's own `/api/v3/system/shutdown` endpoint can't keep it down — Docker restarts the container seconds later, while Starr is mid-repair. Starr detects this and refuses to repair a database the app may reopen.

The reliable fix is to let Starr **stop and start the app's container directly**:

1. **Mount the Docker socket** into the Starr container — `-v /var/run/docker.sock:/var/run/docker.sock` (the Unraid template and `docker-compose.yml` include this by default).
2. **Set the container name** for each app you use — `SONARR_CONTAINER=sonarr`, `RADARR_CONTAINER=radarr`, etc. (defaults match the conventional container names). The value must exactly match the container name shown by `docker ps --format '{{.Names}}'`.

With both in place, the repair sequence becomes: `docker stop sonarr` → backup → SQLite ops on the idle DB → `docker start sonarr`. No restart-policy race, and no need to enable **Skip shutdown**.

If the socket isn't mounted or the container name is unset, Starr falls back to the app's shutdown API (with a stability check). Verify Starr can reach the daemon:

```bash
docker exec starr python3 -c "import docker; print(docker.from_env().ping())"   # True = ready
```

> **Security:** mounting `/var/run/docker.sock` grants the Starr container root-equivalent control of the host Docker daemon (the same tradeoff as Portainer / Watchtower / Dockge). It's entirely optional — leave the socket unmounted and the `*_CONTAINER` vars blank to disable.

---

## 🩺 Troubleshooting

### "Cannot reach _appname_ at http://… " (preflight)

This usually means Starr connected fine but the URL wasn't right. Two common causes:

- **Bridge IP vs published port (Docker / Unraid).** If your *arr container is on Docker's default `bridge` network, the host IP (e.g. `192.168.1.x:8989`) hairpins back through NAT — and that doesn't always work from another bridge container like Starr. Use the *arr's **container bridge IP** instead:
  ```bash
  docker inspect sonarr --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
  # → 172.17.0.x — use this as SONARR_HOST
  ```
  More durable option: put Starr and the *arr apps on the **same user-defined Docker network**, then use container names — `SONARR_HOST=sonarr`. Names only resolve on user-defined networks, not on the default bridge.
- **Wrong API version.** Sonarr / Radarr / Sportarr all speak `/api/v3/…`, but **Lidarr (and Readarr) speak `/api/v1/…`**. Starr handles this automatically per app; if you've forked the code and added a new *arr, register its API version in `APP_DEFAULTS`.

### `True` from `docker exec starr python3 -c "import docker; …ping()"` but no container-managed shutdown
The Docker socket is reachable, so it's the env var. Confirm `SONARR_CONTAINER` is set on the **Starr** container (not the *arr container), and that the value exactly matches `docker ps --format '{{.Names}}'` (capitalization matters). On a fresh dashboard load you should see `container` listed in the SYS line:
```
SONARR config loaded from environment: host, port, container, apikey
```
If `container` is missing, the env var didn't make it into Starr.

### Backup "Permission denied"
The entrypoint chowns `/backups` to `PUID:PGID` on every start, so this is rare. If you hit it once, set `PUID`/`PGID` to match the owner of `/mnt/user/appdata/starr/backups` (or `chown -R PUID:PGID …` once). Note that `/data/<app>` mounts are **not** chowned — they're owned by the *arr apps.

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
5. Report      →  Summary of operations + backup location
6. Restart     →  Wait for the app to come back online, then confirm
```

In **step 6** Starr polls the app's status endpoint and waits for it to come
back online — this relies on your container restart policy
(`--restart unless-stopped`) bringing the app back up automatically. If the app
doesn't return within 3 minutes, restart it yourself:

```bash
docker restart sonarr     # or radarr / lidarr / sportarr
# Unraid: Apps → sonarr → Start
# systemd: systemctl restart sonarr
```

---

## 🐋 Unraid Setup

1. Open **Apps** in the Unraid UI
2. Search for **Starr DB Repair**
3. Click Install — the template pre-fills all path mappings and fields
4. Set a **SECRET_KEY** _(required)_ and your API keys _(masked)_ in the form
5. Adjust the `/data/<app>` paths to match your appdata layout
6. Click **Apply**

Or manually add the template URL in Apps → Settings:
```
https://raw.githubusercontent.com/Krippler/Starr/main/templates/unraid.xml
```

---

## 🌐 API Reference

| Method | Endpoint | Auth required | Description |
|---|---|---|---|
| `GET` | `/` | No | Dashboard web UI |
| `GET` | `/healthz` | No | Liveness probe `{"status":"ok"}` |
| `GET` | `/readyz` | No | Readiness probe |
| `GET` | `/api/apps` | Yes | Env-configured app connections |
| `POST` | `/api/repair/start` | Yes | Start a repair job (JSON body) |
| `POST` | `/api/repair/stop` | Yes | Abort the running job |
| `GET` | `/api/repair/status` | Yes | Current job state |
| `GET` | `/api/repair/stream` | Yes | SSE live log stream |
| `GET` | `/api/backups` | Yes | List backup files |
| `GET` | `/api/history` | Yes | Recent run records (`?instance=` or `?app=`, `?limit=`) |
| `GET` | `/api/history/estimate` | Yes | Median duration of past runs (`?instance=` or `?app=`) |
| `GET` | `/api/instances` | Yes | List instances (env/discovery defaults + extras) |
| `POST` | `/api/instances` | Yes | Add a named extra instance of an app |
| `PUT`/`DELETE` | `/api/instances/<id>` | Yes | Edit / remove an extra instance |

All protected endpoints require an `X-Api-Key` header matching your `SECRET_KEY`.  
The SSE stream accepts `?api_key=` as a query parameter instead (browsers cannot set headers on `EventSource`).

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
docker build -t starr:dev .
docker run -p 8877:8877 starr:dev
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
├── templates/
│   └── unraid.xml           # Unraid Community Apps template
├── tests/
│   └── test_server.py       # pytest suite
├── .github/
│   └── workflows/
│       └── docker-publish.yml   # CI/CD → Docker Hub + GHCR (cosign-signed)
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── PUBLISHING.md
├── LICENSE
└── README.md
```

---

## 🔐 Security Notes

- The container runs as **non-root** (UID 1000)
- API keys set via env vars are **never logged or exposed** in the web UI
- The web UI is protected by `SECRET_KEY` — set this in your `.env` file (or the Unraid form, where it is required). If left unset the dashboard runs **unauthenticated** and logs a warning on every request, so always set it on a shared network
- Place behind a reverse proxy with additional auth (Authelia, Authentik, nginx basic auth) if exposed beyond your LAN
- Published images are **signed with cosign** (keyless / Sigstore) on every release — verify with `cosign verify`

---

## 📄 License



---

## 🙏 Contributing

Issues and PRs welcome. Please open an issue first for significant changes.
