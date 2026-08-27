# 🛠 Starr DB Repair

[![Docker Pulls](https://img.shields.io/docker/pulls/krippler52/starr?style=flat-square&logo=docker)](https://hub.docker.com/r/krippler52/starr)
[![Docker Image Size](https://img.shields.io/docker/image-size/krippler52/starr/latest?style=flat-square)](https://hub.docker.com/r/krippler52/starr)
[![GitHub release](https://img.shields.io/github/v/release/krippler/starr?style=flat-square)](https://github.com/Krippler/Starr/releases)
[![CI](https://github.com/Krippler/Starr/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Krippler/Starr/actions)

**Web UI for diagnosing and repairing the SQLite databases used by Sonarr, Radarr, Lidarr, Sportarr, Readarr, Prowlarr, Whisparr, and Bazarr.**

> ⚠️ **Use at your own risk.** Starr edits live SQLite databases (and always backs them up first). It's been reliable in our own testing, but it's provided **as-is, with no warranty** — the authors accept **no responsibility for any data loss or database damage**. Keep your own backups.

> Safely stops the *arr container, takes a timestamped backup, runs SQLite PRAGMAs on the idle database, streams every log line live to the browser, then brings the app back online — with scheduling, multi-instance support, notifications, restore, and per-instance backup retention.

---

## ✨ Features

**Repair**
- **6 SQLite operations** — integrity check, FK repair, WAL checkpoint, VACUUM, REINDEX, ANALYZE — plus a one-click "Safe" preset
- **Dry-run mode** — preview every step without touching the DB
- **Cancel mid-VACUUM** — Stop calls `Connection.interrupt()`, aborting a long VACUUM/REINDEX in milliseconds
- **Scheduled repairs** — cron-style, per app/instance, with **skip-if-clean** (probes `quick_check` + `foreign_key_check` and skips the run if the DB is already clean)

**Safety**
- **Safe shutdown** — `docker stop` (preferred) or the app's shutdown API, with a stability re-poll so a restart policy can't bring the app back mid-repair
- **Never leaves your app down** — stops are journalled and the restart is guaranteed; if Starr itself is killed mid-repair (OOM, update, reboot) it restarts the container at next boot
- **Disk-space preflight** — refuses a backup, or skips VACUUM, rather than half-writing and filling your disk
- **Auto-backup** before every repair, zstd-compressed by default
- **Restore from backup** — one click: stops the app, snapshots the current DB, writes the backup, starts it again

**Backups**
- **Retention** up to 1 year (or *Forever*), as a global default with per-instance overrides
- **Outcome-flagged files** — `…_clean` / `…_repaired` / `…_aborted` so it's obvious which to keep
- **Bulk-select delete** from the dashboard

**Dashboard**
- **Browser UI** — no SSH required, gated by a single Web Key
- **Live log streaming** over SSE — manual *and* scheduled runs share one log
- **Rearrangeable panels** — drag by the grip to reorder, move between columns, or grow 1–3 columns; **Reset layout** restores the default. Saved per browser.
- **Run history** — powers a last-run pill, a pre-repair estimate ("~2m, based on 4 runs"), and per-instance size/duration trend charts

**Setup**
- **Docker auto-discovery** — one `/appdata` mount + the Docker socket finds each *arr's container, URL, DB path, and bridge IP
- **Multiple instances per app** — e.g. a second Sonarr, each with its own backups, history, schedules, and retention
- **Persisted credentials** — API keys entered in the UI are saved per instance, so reloads and scheduled runs need no env var
- **Notifications** — [Apprise](https://github.com/caronc/apprise) (Discord / Telegram / ntfy / Pushover / Slack / gotify / email / 100+), Signal via [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api), or JSON webhooks, with per-schedule levels (off / error / warning / always)
- **Eight *arr apps** — Sonarr · Radarr · Lidarr · Sportarr · Readarr · Prowlarr · Whisparr · Bazarr, each with the right API version and DB path
- **`linux/amd64` image** on Docker Hub + GHCR, signed with cosign, with an Unraid Community Apps template

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
  krippler52/starr:1.3.7
```

**That's it for the host side.** Open the dashboard, paste each app's API key, click **Save Credentials**, and Starr remembers it for scheduled runs and reloads. URLs / DB paths / container names are auto-discovered from Docker.

**Image tags** — published to both Docker Hub (`krippler52/starr`) and GHCR (`ghcr.io/krippler/starr`):

| Tag | Use |
|---|---|
| `1.3.7` | exact version — recommended pin for production |
| `1.3` / `1` | floating minor / major |
| `latest` | newest **released version** (updated on every version tag) |
| `edge` | tip of `main` — newest merged code, for testing ahead of a release |

> Releases are automatic: merging a release PR publishes the images, tag, and GitHub Release in one CI run. See [PUBLISHING.md](PUBLISHING.md).

---

## 🗂 Volume Mounts

| Container path | Purpose |
|---|---|
| `/appdata` | Host appdata root, mounted **`rw`**. Starr inspects each *arr container, finds its `/config` mount, and walks the relative path inside `/appdata` to locate the DB. |
| `/backups` | Backup output — timestamped `.db.zst` (or `.db` with compression off), plus Starr's own state files (`.starr-*.json`: schedules, history, notify, instances, credential overrides, settings, and stopped-container recovery markers). |
| `/var/run/docker.sock` | Optional but **strongly recommended** — enables auto-discovery and container-managed stop/start. Without it, Starr falls back to the app's HTTP shutdown API. |

> **Why `rw`:** VACUUM, REINDEX, FK repair, and WAL checkpoint all write back to the source `.db`. The backup runs *first*, so the source is only touched after it succeeds.

> **Permissions:** Starr runs as `PUID:PGID` (`99:100` on Unraid, `1000:1000` via compose). The entrypoint chowns `/backups` on startup. `/appdata` is **not** chowned — it belongs to the *arr apps, so `PUID:PGID` must own or share a group with those config dirs.

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
| `BACKUP_ZSTD_LEVEL` | `10` | zstd compression level used when `BACKUP_COMPRESS` is on. |
| `MAX_BACKUP_AGE_DAYS` | `7` | Boot default for backup retention. The dashboard can override globally and per-instance (0–365; `0` = keep forever). |
| `SHUTDOWN_STABILITY_CHECKS` | `5` | After the first offline read, re-poll this many times to make sure the app stays offline (catches a restart-policy bounce). |
| `SHUTDOWN_STABILITY_INTERVAL` | `3` | Seconds between stability re-polls. |
| `STARR_DISABLE_SCHEDULER` | _(unset)_ | Set to `1` to disable the in-process APScheduler (used by the test suite). |
| `APPRISE_TIMEOUT_SECONDS` | `30` | Per-notification timeout for Apprise dispatch. |
| `FLASK_DEBUG` | `false` | Set to `true` to run the dev server in debug mode (`python server.py` only — the container runs gunicorn). |
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

With a restart policy in place (`--restart unless-stopped`, the Unraid default), the app's HTTP shutdown endpoint can't keep it down — Docker restarts it seconds later, mid-repair. Mounting the Docker socket lets Starr stop and start the container directly instead:

```
-v /var/run/docker.sock:/var/run/docker.sock
```

The Unraid template and `docker-compose.yml` include it by default, and auto-discovery resolves the container name — no env var needed. The sequence becomes `docker stop sonarr` → backup → SQLite ops on the idle DB → `docker start sonarr`. Without the socket, Starr falls back to the shutdown API plus stability re-poll.

Check the daemon is reachable:

```bash
docker exec starr python3 -c "import docker; print(docker.from_env().ping())"   # True = ready
```

> **Security:** the socket grants root-equivalent control of the host Docker daemon — the same trade-off as Portainer, Watchtower, or Dockge. Leave it unmounted to disable Docker-managed operation entirely.

---

## 🩺 Troubleshooting

### "Cannot reach _appname_ at http://… " (preflight)
Almost always a URL or network reachability issue:

- **Bridge IP vs published port** — if your *arr container is on Docker's default `bridge` network, the host IP hairpins through NAT, which doesn't always work from another bridge container. Either put Starr + the *arr apps on the **same user-defined network** (then names like `sonarr` resolve), or use the bridge IP that Docker discovery shows in the Connection panel hint.
- **Wrong API version** — Sonarr / Radarr / Sportarr / Whisparr use `/api/v3/…`; Lidarr / Readarr / Prowlarr use `/api/v1/…`; Bazarr uses a versionless `/api/…`. Starr already handles this per app — only relevant if you're forking and adding a new *arr.

### "Could not locate this app's database file" (non-standard DB name)
Some forks/variants name their database differently — e.g. hotio's **Whisparr v2** uses `whisparr2.db` instead of `whisparr.db`. Set the DB name in the **Database path** field on the Connection panel (or the DB-path field when adding an instance): enter just the filename (`whisparr2.db` — resolved next to the auto-detected DB) or a full container path (`/appdata/whisparr/whisparr2.db`). Click **Save Credentials** to persist it for scheduled runs and restore.

### "apikey is required (request body or env)" when a schedule runs
The key wasn't persisted. Enter it, click **Save Credentials**, then **Run now** — it's written to `.starr-instance-overrides.json` so future scheduled runs find it.

### "Not enough free space" / "Skipping VACUUM"
Starr checks free space before writing. A backup needs room for a worst-case uncompressed copy of the DB; VACUUM additionally needs about the database's size again next to it, because it rebuilds into a temporary copy. Free some space (or delete old backups) and re-run — the message reports what was needed versus available.

### Backup "Permission denied"
The entrypoint chowns `/backups` to `PUID:PGID` on every start, so this is rare. If you hit it once, ensure `PUID`/`PGID` match the owner of `/mnt/user/appdata/starr/backups` (or `chown -R PUID:PGID …` once). `/appdata` is **not** chowned — it's owned by the *arr apps.

---

## 🔧 Repair Operations

All six run only against the idle database, after a successful backup — the "Safe" preset in the dashboard selects all of them.

| Operation | Description |
|---|---|
| **Integrity Check** | `PRAGMA integrity_check` — full page-level scan for corruption |
| **Foreign Keys** | `PRAGMA foreign_key_check` — find and remove orphaned FK rows |
| **WAL Checkpoint** | `PRAGMA wal_checkpoint(TRUNCATE)` — flush the write-ahead log into the main file |
| **VACUUM** | Defragment and reclaim free pages (rebuilds via a temporary copy) |
| **REINDEX** | Drop and rebuild every index |
| **ANALYZE** | Refresh query-planner statistics |

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
│   ├── gunicorn.conf.py     # gunicorn config (filters SSE keep-alive log noise)
│   ├── requirements.txt
│   └── templates/
│       └── index.html       # Dashboard web UI (vanilla JS + SSE)
├── templates/
│   ├── unraid.xml           # Unraid Community Apps template
│   └── starr-icon.png       # Icon referenced by the Unraid template
├── tests/
│   └── test_server.py       # pytest suite
├── .github/
│   └── workflows/
│       └── docker-publish.yml   # CI/CD → Docker Hub + GHCR (cosign-signed)
├── Dockerfile
├── entrypoint.sh            # Reconciles PUID/PGID, chowns /backups, drops to non-root
├── docker-compose.yml
├── .env.example
├── ca_profile.xml           # Unraid Community Apps maintainer profile
├── PUBLISHING.md            # Release process + image tagging policy
├── CHANGELOG.md
├── LICENSE
└── README.md
```

---

## 🔐 Security Notes

- The container runs **non-root** — the entrypoint drops to `PUID:PGID` via gosu.
- **Set a real `SECRET_KEY`.** The shipped compose/`.env` value is the insecure-default sentinel (`change-me-in-production`), so an unconfigured install fails *loud* — unauthenticated, warned on every request, with a banner in the dashboard — rather than silently authenticating against a value published in this repo.
- The API-key check is constant-time (`hmac.compare_digest`), so response timing doesn't leak how much of the key matched.
- API keys are masked in the form, stored server-side in `/backups/.starr-instance-overrides.json`, and never echoed by `/api/repair/status` or the SSE stream.
- Mounting `/var/run/docker.sock` is opt-in and grants root-equivalent control of the host Docker daemon.
- Put it behind a reverse proxy with real auth (Authelia, Authentik, nginx basic auth) if it's reachable beyond your LAN.
- Images are **cosign-signed** (keyless / Sigstore) on every release — verify with `cosign verify`.

---

## 📄 License

See [LICENSE](LICENSE).

---

## 🙏 Contributing

Issues and PRs welcome. Please open an issue first for significant changes.
