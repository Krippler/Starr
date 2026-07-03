# 🛠 Starr DB Repair

[![Docker Pulls](https://img.shields.io/docker/pulls/krippler52/starr?style=flat-square&logo=docker)](https://hub.docker.com/r/krippler52/starr)
[![Docker Image Size](https://img.shields.io/docker/image-size/krippler52/starr/latest?style=flat-square)](https://hub.docker.com/r/krippler52/starr)
[![GitHub release](https://img.shields.io/github/v/release/krippler/starr?style=flat-square)](https://github.com/Krippler/Starr/releases)
[![CI](https://github.com/Krippler/Starr/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Krippler/Starr/actions)

**Web UI for diagnosing and repairing the SQLite databases used by Sonarr, Radarr, Lidarr, Sportarr, Readarr, Prowlarr, Whisparr, and Bazarr.**

> Safely stops the *arr container, takes a timestamped backup, runs SQLite PRAGMAs on the idle database, streams every log line live to the browser, then brings the app back online — with scheduling, multi-instance support, notifications, restore, and per-instance backup retention.

---

## ✨ Features

- **Browser dashboard** — no SSH required, with a single Web Key gate
- **Live log streaming** via Server-Sent Events (SSE) — manual *and* scheduled runs stream into the same log
- **Safe shutdown** — `docker stop` (preferred) or the app's shutdown API, with stability re-poll so a restart policy can't bring it back mid-repair
- **6 SQLite operations** — integrity check, FK repair, WAL checkpoint, VACUUM, REINDEX, ANALYZE — with a one-click "Safe" preset
- **Dry-run mode** — preview every step without touching the DB
- **Cancel mid-VACUUM** — Stop calls `Connection.interrupt()` so a long VACUUM / REINDEX aborts in milliseconds, not minutes
- **Auto-backup** before every repair, **zstd-compressed** by default
- **Backup retention** adjustable from the dashboard up to 1 year (or *Forever*) — **global default + per-instance overrides**, so a daily-backed Sonarr can keep 14 days while a weekly Sonarr-4K keeps a year
- **Restore from backup** — one-click restore puts a chosen backup back over the live DB (stops → snapshots current → writes → starts)
- **Outcome-flagged backups** — files are renamed `…_clean.db.zst` / `…_repaired.db.zst` / `…_aborted.db.zst` so it's obvious which to keep
- **Bulk-select delete** — checkbox in each backup row + a "Delete selected" action
- **Scheduled repairs** — cron-style, per app/instance, with **skip-if-clean** (probes `quick_check` + `foreign_key_check` and skips the whole run if the DB is already clean)
- **Multiple instances per app** — manage more than one of the same *arr (e.g. a second Sonarr at a different URL); each instance has its own backups, history, schedules, and retention
- **Run history** — every completed repair is recorded; powers a **last-run pill**, a **pre-repair time estimate** ("~2m, based on 4 runs"), and **per-instance DB-size / repair-duration trend charts**
- **Notifications** on completion — **Apprise** (Discord / Telegram / ntfy / Pushover / Slack / gotify / email / 100+ services), **Signal** via [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api), and **plain JSON webhooks**, with per-schedule level overrides (off / error / warning+ / always)
- **Persisted credentials** — typed an API key in the UI? It's saved per instance, so reloads and scheduled runs use it without an env var
- **Docker auto-discovery** — one `/appdata` mount + the Docker socket → Starr finds each *arr's container, URL, DB path, and bridge IP automatically; UI shows the host-perspective URL but talks to the bridge IP internally
- **Eight *arr apps supported** — Sonarr · Radarr · Lidarr · Sportarr · Readarr · Prowlarr · Whisparr · Bazarr (correct API versions and DB paths per app)
- **Docker image** — `linux/amd64`, published to Docker Hub + GHCR, signed with cosign
- **Unraid Community Apps template** included

---

## 🚀 Quick Start

### Docker Compose (recommended)

```bash
git clone https://github.com/Krippler/Starr.git
cd Starr
cp .env.example .env       # set SECRET_KEY + your API keys
docker compose up -d
```

Open **http://localhost:8877** and enter your `SECRET_KEY` as the Web Key.

### Docker CLI

```bash
docker run -d \
  --name starr \
  --restart unless-stopped \
  -p 8877:8877 \
  -e SECRET_KEY=your-strong-secret \
  -e PUID=99 -e PGID=100 \
  -v /mnt/user/appdata:/appdata:rw \
  -v /mnt/user/appdata/starr/backups:/backups \
  -v /var/run/docker.sock:/var/run/docker.sock \
  krippler52/starr:1.2.2
```

**That's it for the host side.** Open the dashboard, paste each app's API key, click **Save Credentials**, and Starr remembers it for scheduled runs and reloads. URLs / DB paths / container names are auto-discovered from Docker.

**Image tags** — published to both Docker Hub (`krippler52/starr`) and GHCR (`ghcr.io/krippler/starr`):

| Tag | Use |
|---|---|
| `1.2.2` | exact version — recommended pin for production |
| `1.2` / `1` | floating minor / major |
| `latest` | newest **released version** (updated on every version tag) |
| `edge` | tip of `main` — newest merged code, for testing ahead of a release |

> **Releases are fully automatic.** Merging a release PR (one that flips `CHANGELOG.md`'s `[Unreleased]` section to `[X.Y.Z]`) is enough — CI publishes the version pins (`X.Y.Z` / `X.Y` / `X`), moves **`latest`** to that release, auto-creates the `vX.Y.Z` git tag, and creates the matching **GitHub Release** with notes from the [CHANGELOG](CHANGELOG.md). Plain merges to `main` (no version flip) only update `edge`. Pushing a `v*.*.*` tag manually still works the same — useful for re-running the release pipeline.

---

## 🗂 Volume Mounts

| Container path | Purpose |
|---|---|
| `/appdata` | Host appdata root. Starr inspects each *arr container, finds its `/config` mount, and walks the relative path inside `/appdata` to locate the DB. One mount replaces the old per-app mounts. |
| `/backups` | Backup output — timestamped `.db.zst` (or `.db` if compression is off). Also stores hidden settings files: `.starr-schedules.json`, `.starr-history.json`, `.starr-notify.json`, `.starr-instances.json`, `.starr-instance-overrides.json`, `.starr-settings.json`. |
| `/var/run/docker.sock` | (Optional, **strongly recommended**) Docker socket — enables auto-discovery and container-managed stop/start. Without it, Starr falls back to the app's HTTP shutdown API. |

> **Mount mode:** `/appdata` must be `rw` — VACUUM, REINDEX, FK repair, and WAL checkpoint write back to the source `.db`. The pre-repair backup happens *first*, so the source DB is only ever touched after a successful backup.

> **Permissions:** Starr runs as `PUID:PGID` (default `99:100` on Unraid, `1000:1000` via compose). The entrypoint chowns `/backups` on startup so backups always work. `/appdata` is **not** chowned (it belongs to the *arr apps) — `PUID:PGID` must own or share a group with those config dirs so VACUUM/REINDEX can write.

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PUID` | `99` (Unraid) / `1000` (compose) | UID the container runs as. Must own — or share a group with — your *arr config dirs. |
| `PGID` | `100` (Unraid) / `1000` (compose) | GID the container runs as. |
| `PORT` | `8877` | Web UI listen port. |
| `SECRET_KEY` | _(required)_ | Web UI access key. Leave it at the shipped default (`change-me-in-production`, which `docker-compose.yml` and `.env.example` both use) and the dashboard runs **unauthenticated** — logging a warning on every request and showing an "insecure" banner in the UI. Set a strong random value (e.g. `openssl rand -hex 32`) to enforce the Web Key gate. |
| `LOG_LEVEL` | `INFO` | `DEBUG` `INFO` `WARNING` `ERROR`. |
| `APPDATA_DIR` | `/appdata` | Container path of the host appdata root (rarely needs changing). |
| `BACKUP_DIR` | `/backups` | Backup output directory inside the container. |
| `BACKUP_COMPRESS` | `true` | Stream-compress backups to `.db.zst`. Set to `false` for plain `.db`. |
| `MAX_BACKUP_AGE_DAYS` | `7` | Boot default for backup retention. The dashboard can override globally and per-instance (0–365; `0` = keep forever). |
| `SHUTDOWN_STABILITY_CHECKS` | `5` | After the first offline read, re-poll this many times to make sure the app stays offline (catches a restart-policy bounce). |
| `SHUTDOWN_STABILITY_INTERVAL` | `3` | Seconds between stability re-polls. |
| `STARR_DISABLE_SCHEDULER` | _(unset)_ | Set to `1` to disable the in-process APScheduler (used by the test suite). |
| `<APP>_APIKEY` | _(blank)_ | API key for an app — `SONARR_APIKEY`, `RADARR_APIKEY`, `LIDARR_APIKEY`, `SPORTARR_APIKEY`, `READARR_APIKEY`, `PROWLARR_APIKEY`, `WHISPARR_APIKEY`, `BAZARR_APIKEY`. The UI also has a **Save Credentials** button that persists API keys per instance without needing an env var. |
| `<APP>_URL` | _(blank)_ | Optional URL override per app — `SONARR_URL`, `RADARR_URL`, etc. Format: `http://host:port[/urlbase]`. Only set when Docker discovery can't find the container or you want to point at a specific instance. |
| `CORS_ORIGINS` | `http://localhost:8877` | CORS allowlist for the Web UI API. |

All connection settings can also be entered directly in the dashboard (URL + API Key in the Connection panel) and persisted with **Save Credentials** — env vars are just the boot defaults.

---

## 🧩 Multi-instance (more than one of the same *arr)

Each app has a **default instance** synthesized from env / Docker discovery (id = the app name, e.g. `sonarr`). To manage extras (e.g. a second Sonarr for 4K), click **+ Add instance** under the app tabs and fill in name + URL + API key. The id of an extra is always hyphenated (e.g. `sonarr-4k`) so it can never collide with a default.

Backups, history, schedules, restore, and **retention** are all keyed by instance id, so the two Sonarrs are kept fully separate:

| Object | Per default instance | Per named extra |
|---|---|---|
| Backups | `sonarr_<ts>.db.zst` | `sonarr-4k_<ts>.db.zst` |
| History / trends / estimate | Per `sonarr` | Per `sonarr-4k` |
| Schedules | `instance_id` field on the schedule | same |
| Retention override | dashboard / API | dashboard / API |

---

## 🐳 Container-managed shutdown (recommended for Docker / Unraid)

On any host with a restart policy (`--restart unless-stopped`, the Unraid default), the app's HTTP shutdown endpoint can't keep it down — Docker restarts the container seconds later, while Starr is mid-repair. The reliable fix is to let Starr **stop and start the container directly** via the Docker socket:

1. **Mount the Docker socket** — `-v /var/run/docker.sock:/var/run/docker.sock` (the Unraid template and `docker-compose.yml` include this by default).
2. **Auto-discovery handles the container name** — no env var needed.

The repair sequence becomes `docker stop sonarr` → backup → SQLite ops on the idle DB → `docker start sonarr`. If the socket isn't mounted, Starr falls back to the app's shutdown API plus stability re-poll. Verify the daemon is reachable:

```bash
docker exec starr python3 -c "import docker; print(docker.from_env().ping())"   # True = ready
```

> **Security:** mounting `/var/run/docker.sock` grants the Starr container root-equivalent control of the host Docker daemon (the same tradeoff as Portainer / Watchtower / Dockge). Leave it unmounted to disable Docker-managed operation entirely.

---

## 🩺 Troubleshooting

### "Cannot reach _appname_ at http://… " (preflight)
Almost always a URL or network reachability issue:

- **Bridge IP vs published port** — if your *arr container is on Docker's default `bridge` network, the host IP hairpins through NAT, which doesn't always work from another bridge container. Either put Starr + the *arr apps on the **same user-defined network** (then names like `sonarr` resolve), or use the bridge IP that Docker discovery shows in the Connection panel hint.
- **Wrong API version** — Sonarr / Radarr / Sportarr / Whisparr use `/api/v3/…`; Lidarr / Readarr / Prowlarr use `/api/v1/…`; Bazarr uses a versionless `/api/…`. Starr already handles this per app — only relevant if you're forking and adding a new *arr.

### "apikey is required (request body or env)" when a schedule runs
The dashboard's API Key field was form-only state before v1.1.1. Fix: enter the key, click **Save Credentials**, then **Run now** on the schedule — it'll be persisted to `.starr-instance-overrides.json` so future scheduled runs find it.

### Stop didn't kill a long VACUUM in older versions
Pre-v1.1.0 Stop only set an abort flag checked between ops. From v1.1.0 onward Stop calls `Connection.interrupt()` and aborts the in-flight statement in milliseconds.

### Backup "Permission denied"
The entrypoint chowns `/backups` to `PUID:PGID` on every start, so this is rare. If you hit it once, ensure `PUID`/`PGID` match the owner of `/mnt/user/appdata/starr/backups` (or `chown -R PUID:PGID …` once). `/appdata` is **not** chowned — it's owned by the *arr apps.

---

## 🔧 Repair Operations

| Operation | Safe? | Description |
|---|---|---|
| **Integrity Check** | ✅ | `PRAGMA integrity_check` — full page-level scan for corruption |
| **Foreign Keys** | ✅ | `PRAGMA foreign_key_check` — find and remove orphaned FK rows |
| **WAL Checkpoint** | ✅ | `PRAGMA wal_checkpoint(TRUNCATE)` — flush write-ahead log into the main file |
| **VACUUM** | ✅ | Defragments the database and reclaims free pages |
| **REINDEX** | ✅ | Drops and rebuilds every index from scratch |
| **ANALYZE** | ✅ | Updates query-planner statistics |

---

## 🔄 Repair Sequence

```
1. Preflight   →  Reach the app API, locate the DB file, optional clean-probe
2. Shutdown    →  docker stop <container>  (or the app's shutdown API + re-poll)
3. Backup      →  Stream-copy DB to /backups/<instance>_YYYYMMDD_HHMMSS.db.zst
4. SQLite ops  →  Run selected PRAGMAs on the idle file
5. Report      →  Summary of operations + outcome-flag the backup filename
6. Restart     →  docker start <container>, wait for the app to come back online
```

If **Skip-if-clean** is enabled (scheduled runs default to it), step 1 also probes the live DB read-only with `quick_check` + `foreign_key_check`; if both pass, the run skips entirely — no shutdown, no backup, no mutations.

---

## 🐋 Unraid Setup

1. Open **Apps** in the Unraid UI
2. Search for **Starr DB Repair**
3. Click Install — the template pre-fills `/appdata`, `/backups`, and the Docker socket
4. Set a **SECRET_KEY** _(required)_ and paste your API keys _(masked)_
5. Click **Apply**, open the WebUI, and (optionally) click **Save Credentials** on each app to persist the keys without leaving them in env vars

Or manually add the template URL in Apps → Settings:
```
https://raw.githubusercontent.com/Krippler/Starr/main/templates/unraid.xml
```

---

## 🌐 API Reference

All protected endpoints require an `X-Api-Key` header matching your `SECRET_KEY`. The SSE stream accepts `?api_key=` as a query parameter instead (browsers cannot set headers on `EventSource`).

### Health / dashboard
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/` | No | Dashboard web UI |
| `GET` | `/healthz` | No | Liveness probe `{"status":"ok"}` |
| `GET` | `/readyz` | No | Readiness probe |
| `GET` | `/api/config` | No | Public UI config — reports whether `SECRET_KEY` is still the insecure default (drives the dashboard's security banner). No secrets returned. |

### Repair lifecycle
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `POST` | `/api/repair/start` | Yes | Start a repair job (JSON body) |
| `POST` | `/api/repair/stop` | Yes | Abort the running job (interrupts in-flight SQLite ops) |
| `GET` | `/api/repair/status` | Yes | Current job state |
| `GET` | `/api/repair/stream` | Yes | Server-Sent Events live log |

### Backups
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/api/backups` | Yes | List backup files (with `result`/`compressed` flags) |
| `DELETE` | `/api/backups/<name>` | Yes | Delete a single backup |
| `POST` | `/api/backups/delete` | Yes | Bulk delete (`{"names": [...]}`) |
| `POST` | `/api/backups/<name>/restore` | Yes | Restore a backup over the live DB |

### Instances
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/api/instances` | Yes | List instances (env/discovery defaults + extras), with retention picture |
| `POST` | `/api/instances` | Yes | Add a named extra instance of an app |
| `PUT` / `DELETE` | `/api/instances/<id>` | Yes | Edit / remove an extra instance |
| `PUT` | `/api/instances/<id>/credentials` | Yes | Persist URL + API key for this instance |
| `PUT` | `/api/instances/<id>/retention` | Yes | Per-instance backup retention (`null` to clear) |

### Discovery
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `POST` | `/api/discover` | Yes | Rescan Docker for *arr containers |
| `GET` | `/api/apps` | Yes | Legacy: one row per app (env/discovery defaults only) — kept for back-compat |

### Schedules
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/api/schedules` | Yes | List schedules |
| `POST` | `/api/schedules` | Yes | Create a schedule |
| `PUT` / `DELETE` | `/api/schedules/<id>` | Yes | Edit / delete a schedule |
| `POST` | `/api/schedules/<id>/run-now` | Yes | Fire the schedule immediately |

### History
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/api/history` | Yes | Recent run records (`?instance=` or `?app=`, `?limit=`) |
| `GET` | `/api/history/estimate` | Yes | Median duration of past runs (`?instance=` or `?app=`) |

### Notifications
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` / `PUT` | `/api/notify` | Yes | Read / save notification config (Apprise + Signal + Webhooks) |
| `POST` | `/api/notify/test` | Yes | Send a test notification with the current config |

### Settings (global)
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` / `PUT` | `/api/settings` | Yes | Global settings (currently just `max_backup_age_days`) |

### `POST /api/repair/start` body

```json
{
  "app":           "sonarr",
  "instance_id":   "sonarr",
  "url":           "http://sonarr:8989",
  "apikey":        "YOUR_API_KEY",
  "ops":           ["integrity","foreign_keys","wal_checkpoint","vacuum","reindex","analyze"],
  "dry_run":       false,
  "skip_shutdown": false
}
```

Most fields are optional — `instance_id` alone is enough if the instance's connection has been saved.

---

## 🏗 Development

```bash
git clone https://github.com/Krippler/Starr.git
cd Starr
python3 -m venv .venv && source .venv/bin/activate
pip install -r app/requirements.txt

# Run in dev mode
cd app
FLASK_DEBUG=true python server.py

# Run the test suite
STARR_DISABLE_SCHEDULER=1 pytest -q

# Build the Docker image locally
docker build -t starr:dev .
docker run -p 8877:8877 -e SECRET_KEY=dev starr:dev
```

---

## 📦 Project Layout

```
Starr/
├── app/
│   ├── server.py            # Flask backend (REST + SSE) — repair lifecycle, history, instances
│   ├── schedules.py         # APScheduler-backed cron scheduler
│   ├── history.py           # Persistent run history
│   ├── instances.py         # Per-app instance store + credential overrides
│   ├── notify.py            # Apprise / Signal / webhook dispatch
│   ├── settings.py          # UI-adjustable settings (backup retention)
│   ├── discovery.py         # Docker auto-discovery of *arr containers
│   ├── requirements.txt
│   └── templates/
│       └── index.html       # Dashboard web UI (vanilla JS + SSE)
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
├── CHANGELOG.md
├── LICENSE
└── README.md
```

---

## 🔐 Security Notes

- The container runs as **non-root** — `PUID:PGID` (entrypoint drops via gosu).
- The Web UI is protected by `SECRET_KEY`. The shipped compose/`.env` defaults are the exact insecure-default sentinel (`change-me-in-production`), so an unconfigured install fails **loud** — unauthenticated, with a warning logged on every request and an "insecure" banner in the dashboard — rather than silently authenticating against a value published in this repo. Always set a strong random value on a shared network.
- The API-key check uses a constant-time comparison (`hmac.compare_digest`), so it doesn't leak how many leading characters of the key matched via response timing.
- API keys saved via the UI's **Save Credentials** button are persisted server-side to `/backups/.starr-instance-overrides.json` and are masked in the form.
- API keys are never echoed in the response body for `/api/repair/status` or the SSE stream.
- Place behind a reverse proxy with extra auth (Authelia, Authentik, nginx basic auth) if exposed beyond your LAN.
- Mounting `/var/run/docker.sock` is **opt-in** but is root-equivalent control of the host Docker daemon — leave it unmounted to disable Docker-managed operation.
- Published images are **signed with cosign** (keyless / Sigstore) on every release — verify with `cosign verify`.

---

## 📄 License

See [LICENSE](LICENSE).

---

## 🙏 Contributing

Issues and PRs welcome. Please open an issue first for significant changes.
