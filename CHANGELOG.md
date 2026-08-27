# Changelog

All notable changes are documented here. Releases follow [SemVer](https://semver.org).
Images are published to Docker Hub (`krippler52/starr`) and GHCR (`ghcr.io/krippler/starr`).

## [1.3.7] — 2026-08-26

### Fixed
- **An interrupted run could leave your *arr container stopped** — the restart step was only reached on explicit success/guard paths, so an unexpected error after shutdown left the app down with nothing to bring it back. Both the repair and restore workers now restart from a `finally`, guarded so the normal path can't double-start.
- **Starr dying mid-repair left the app stopped with no recovery** — every `docker stop` is now journalled to `/backups/.starr-stopped.json` and cleared on restart. At boot, Starr starts anything a previous process stopped but never restarted (OOM kill, container update, host reboot). Containers already running are left alone, and the marker is kept for a later retry if the Docker socket isn't reachable.

### Added
- **Disk-space preflight** — there were no free-space checks at all, so a backup could half-write and VACUUM could fail mid-rebuild; on Unraid that usually means filling the cache pool that also holds `docker.img` and appdata. Backups now refuse to start without room for a worst-case (incompressible) copy plus margin, and VACUUM is skipped with a clear message when its transient full copy wouldn't fit — the remaining operations still run.

## [1.3.6] — 2026-07-17

### Fixed
- **The rightmost dashboard column couldn't be dragged** — cards there couldn't move to a new column or the first column until a third column was seeded from the left. Native HTML5 drag-and-drop was the cause: adding the ＋ rail on `dragstart` reflowed the columns and broke the browser's drag hit-testing. Rearranging now uses pointer events with a floating clone and coordinate-based routing, so every column behaves identically.

## [1.3.5] — 2026-07-17

### Fixed
- **Dropping into an adjacent column or the new-column rail was unreliable** — a per-column `dragover` handler meant you had to land the cursor precisely inside a thin target, and the gaps between columns were dead zones. Drag routing now uses one dashboard-level handler that tiles the full width into column bands.

## [1.3.4] — 2026-07-17

### Fixed
- **Couldn't drag a card into a shorter column** — columns were only as tall as their content, so the empty space beneath a short column rejected drops. Columns now stretch to full dashboard height, and the ＋ drop-rail was widened (48→60px).

## [1.3.3] — 2026-07-17

### Added
- **Grow/shrink dashboard columns by dragging** — while dragging, empty columns show as a dashed **＋ drop-rail**, so you can pull a card into empty space to spin a column back up (1 → 2 → 3, capped by screen width). Previously the only way back was **Reset layout**.

## [1.3.2] — 2026-07-17

### Fixed
- **Empty dashboard columns left dead space** — an emptied column still reserved its share of the width. Empty columns now collapse so the populated ones fill the page.

## [1.3.1] — 2026-07-17

### Fixed
- **Panel titles were pushed to the centre of their headers** — the 1.3.0 drag grip became a third flex child under `justify-content: space-between`. It's now absolutely positioned in a reserved gutter, out of the flex flow.

## [1.3.0] — 2026-07-16

### Added
- **Rearrangeable dashboard** — the main panels are drag-to-rearrange cards. Hover a header for the grip, then reorder or move between columns; wide screens flow into 2–3 columns (narrow/mobile unchanged), each width remembering its own arrangement in `localStorage`. **Reset layout** restores the default. Live run status stays in a fixed strip above the grid so it never moves mid-repair. Frontend only — no API changes.

## [1.2.10] — 2026-07-16

### Fixed
- **Docker client socket/fd leak** — every Docker operation opened a docker-py client without closing it, leaking a `requests` session and a socket each time. Since discovery runs on every scheduled-repair preflight, these accumulated toward the process's open-file limit. All clients are now closed after use.
- **Leaked SQLite connection on an unexpected repair error** — a non-`sqlite3` error escaping the op loop abandoned an open connection still holding a WAL/exclusive lock on the live database. Cleanup now runs in a `finally`.
- **Notifications could hang the repair worker forever** — Apprise's `notify()` takes no timeout and many plugins fall back to `requests` without one, so a black-holed target blocked the worker indefinitely. Sends are now bounded by `APPRISE_TIMEOUT_SECONDS` (default 30s).
- **Stale credential overrides on instance delete** — deleting a named instance left its saved apikey/url/db_path override on disk. `delete()` now removes it.

## [1.2.9] — 2026-07-13

Supersedes 1.2.8, whose tag was cut moments before this fix merged — the published `1.2.8`/`latest` images were built from 1.2.7 code and lacked the fix below.

### Fixed
- **Scheduled repairs self-heal a stale container IP** — if a container was recreated, Docker reassigned its bridge IP and scheduled runs failed preflight until you clicked **Detect**. Preflight now detects a first-try connection miss, forces a fresh scan, and retries once at the current address. Explicit `url` / `*_URL` overrides are never rescanned over. Discovery's Docker timeout also rose from 10s to 30s so a busy daemon doesn't strand the cache.

## [1.2.7] — 2026-07-13

Supersedes 1.2.6, whose tag was created from an incomplete commit and shipped only the first fix below.

### Fixed
- **`docker stop` read-timeouts no longer abort a repair** — docker-py sets a stop's read timeout to `client_timeout + stop_grace` (was 10 + 30 = 40s), so a busy daemon surfaced as `docker stop failed: … Read timed out` even while it *was* stopping the container. The client timeout is now 30s (60s of headroom), and a stop read-timeout is treated as "maybe still stopping": Starr polls for up to 60s and proceeds once the app is actually offline. Genuine stop errors still fail fast.
- **Repairs re-scan Docker for the container's current bridge IP** — the discovery cache was only refreshed at startup and via **Detect**, so a recreated container left repairs hitting the old address. Manual, scheduled, and restore paths now rescan before resolving the connection.

## [1.2.5] — 2026-07-09

### Changed
- **Added an "as-is, no warranty" disclaimer** to the README, the Unraid template `<Overview>`, and `ca_profile.xml`.
- **Health-check probes no longer flood the access log** — a `gunicorn.conf.py` filter drops `/healthz` and `/readyz` access-log lines; every real request is still logged.

## [1.2.4] — 2026-07-05

### Changed
- **Database path field is app-aware** ([#65](https://github.com/Krippler/Starr/pull/65)) — the hint and placeholder now reflect the selected app's default DB filename instead of always citing Whisparr, and the field shares a row with URL + API Key on wide screens.

## [1.2.3] — 2026-07-05

### Added
- **Custom database name / path override** ([#62](https://github.com/Krippler/Starr/issues/62)) — a **Database path** field for non-standard DB names (e.g. hotio's Whisparr v2 uses `whisparr2.db`). Accepts a bare filename or a full container path, persists per instance, and is honoured by manual runs, scheduled runs, and restore. Adds `db_path_override` to `/api/instances`.

### Changed
- **Unraid Community Applications readiness** — added a template `<Icon>` (CA rejects templates without one) and `<Beta>False</Beta>`; made the Docker socket mount optional (`Required="false"`) with the root-equivalent trade-off spelled out; rewrote the `SECRET_KEY` description to match actual behaviour.

## [1.2.2] — 2026-07-01

### Changed
- **Dashboard density pass** — action buttons moved into the panel bar they belong to: **Run Repair**/**Stop** and the last-run pill into Repair Operations, **Refresh Backups** into Backups, and **Detect**/**Save Credentials**/**Test Connection** into Connection. The standalone action row is gone, and URL + API Key sit side-by-side on wide viewports.

## [1.2.1] — 2026-07-01

### Security
- **Shipped `SECRET_KEY` defaults now match the insecure-default sentinel** — `docker-compose.yml` and `.env.example` defaulted to `change-me` / `change-me-to-a-random-string`, which are *different* strings from the one `server.py` checks for. An out-of-the-box `docker compose up` was therefore silently authenticating every request against a value published in this repo, with no warning and no banner. Both files now ship the sentinel, so an unset key is loud and visible.
- **API-key comparison is constant-time** (`hmac.compare_digest`), closing a timing side-channel.

### Changed
- **Releases are now fully automatic** — merging a release PR publishes the version pins, moves `latest`, creates the `vX.Y.Z` tag, and creates the GitHub Release in one workflow run. Manual tag pushes still work.

### Upgrade note
If your `.env` still has `SECRET_KEY` unset or set to an old shipped default (`change-me` / `change-me-to-a-random-string`), set a real random value now — e.g. `openssl rand -hex 32`. Those old values are **not** treated as insecure defaults, so requests against them were being silently authenticated.

## [1.2.0] — 2026-06-24

UX rework — a calmer dashboard at rest, plus release automation so `latest` means "newest release".

### Added
- **`edge` image tag** (#47) — every push to `main` publishes `krippler52/starr:edge` and `ghcr.io/krippler/starr:edge` for testing ahead of a release.

### Changed
- **`latest` tracks the newest released version, not every commit** (#47) — only `v*.*.*` tag pushes move it; merges to `main` update `edge`. Each version tag also auto-creates a GitHub Release from this changelog.
- **Dashboard de-clutter** (#48) — Trends, Backups, Schedules, and Notifications are collapsible (collapsed by default, state saved per browser). The phase indicator only renders during a repair, and the shutdown warning stays a muted line unless it matters. Collapsed sections fetch on first expand.
- **Repair Operations panel** (#51) — collapsible and moved directly above Run Repair, with Dry Run + Skip Shutdown in the header and a `"3 selected"` chip.
- **Backup retention controls** (#49) consolidated into one **Retention** card with two columns — *Default for all instances* and *This instance* — and plain-English source captions.
- **Lock button** (#50) moved to the Connection header, next to the status badge.

### Fixed
- **Last-run pill and trend charts scope to the selected instance** (#52) instead of bleeding across named extras of the same app.

## [1.1.2] — 2026-06-24

### Added
- **Adjustable backup retention from the dashboard** (#43) — `7 / 14 / 30 / 60 / 90 / 180 / 365 / Forever`, via new `GET`/`PUT /api/settings`. `MAX_BACKUP_AGE_DAYS` remains the boot fallback.
- **Per-instance backup retention** (#44) — each instance can override the global value, so one prune window can't chop another's files. New `PUT /api/instances/<id>/retention` (`null` clears). `/api/instances` now returns `retention_days` and `retention_effective_days`.

### Changed
- **README rewritten** (#45) for the single `/appdata` mount + auto-discovery era, with a complete API reference.
- **UI labels and tooltips** (#45) tightened around instances, retention inheritance, and Save Credentials.

## [1.1.1] — 2026-06-22

### Fixed
- **API keys typed in the dashboard now persist and reach scheduled runs** (#40) — the API Key field was form-only state, so a schedule firing without a `*_APIKEY` env var failed with `apikey is required`. **Save Credentials** now persists URL + API Key per instance to `.starr-instance-overrides.json`. New `PUT /api/instances/<id>/credentials`.
- **Default-instance schedules read the saved override** (#41) — those runs carry an empty `instance_id`, which skipped the override lookup; it now falls back to the app name.
- **Schedule rows surface the failure reason** (#40) instead of just the word "error".

## [1.1.0] — 2026-06-21

Multiple instances per app, plus a run-history layer powering the last-run pill, time estimate, and trend charts. Fully backwards-compatible.

### Added
- **Multiple instances per app** (#36, #37) — manage a second Sonarr etc. Each app keeps its env/discovery "default"; extras are managed from the instance selector. Backups, schedules, history, and restore are all per-instance. New `GET/POST /api/instances`, `PUT/DELETE /api/instances/<id>`.
- **Run history store** (#32) — every completed repair is recorded to `.starr-history.json` (rolling cap of 500), driving the last-run pill and a pre-repair estimate ("~2m, based on 4 runs") computed from real past runs. New `GET /api/history`, `GET /api/history/estimate`.
- **Trend charts** (#34) — inline-SVG sparklines for repair duration and database size over the last 30 runs.
- **Instance-scoped history & trends** (#38) — named extras get their own pill, estimate, and charts; defaults fall back to per-app so pre-upgrade records still surface.
- **Webhook on completion** (#33) — a JSON POST alongside the existing Apprise + Signal notifications.

### Changed
- **Stop actually cancels a mid-VACUUM / REINDEX** (#35) — the active connection is published on the job state and `api_stop` calls `Connection.interrupt()`; verified to abort a real 783 MB VACUUM in ~9 ms. The cancelled op is recorded as `aborted` and its backup renamed `…_aborted.db[.zst]` instead of the misleading `…_clean`.

### Fixed
- **Scheduler accepts the newer *arr apps** (#33) — schedules can now also be created for Readarr, Prowlarr, Whisparr, and Bazarr.

### Notes
- `.starr-instances.json` is created on demand alongside the other state files in `BACKUP_DIR` — no new mounts.
- Records written by 1.0.x have no `instance` field and are treated as belonging to the default instance.

## [1.0.4]

Previous tagged release. See git history.
