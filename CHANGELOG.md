# Changelog

All notable changes are documented here. Releases follow [SemVer](https://semver.org).
Image tags published to Docker Hub (`krippler52/starr`) and GHCR (`ghcr.io/krippler/starr`).

## [Unreleased]

### Changed
- **Added an "as-is, no warranty / use at your own risk" disclaimer** to the top of the README, the Unraid template `<Overview>`, and `ca_profile.xml` — noting the tool has been reliable in testing but the authors accept no responsibility for data loss or database damage, and users should keep their own backups.

## [1.2.4] — 2026-07-05

Patch release: polish for the Database path field shipped in 1.2.3.

### Changed
- **Database path field is now app-aware and more concise** ([#65](https://github.com/Krippler/Starr/pull/65)) — the hint and placeholder reflect the *selected* app's default DB filename (`sonarr.db` on Sonarr, `radarr.db` on Radarr, …) instead of always citing Whisparr; the `whisparr2.db` example now only appears on the Whisparr tab. Copy trimmed to a one-liner, and on wide screens the field flows onto the same row as URL + API Key (wrapping gracefully as the window narrows).

## [1.2.3] — 2026-07-05

### Added
- **Custom database name / path override** ([#62](https://github.com/Krippler/Starr/issues/62)) — a new **Database path** field on the Connection panel (and the add-instance form) lets you point Starr at a non-standard DB name, e.g. hotio's Whisparr v2 uses `whisparr2.db` instead of `whisparr.db`. Accepts a bare filename (resolved next to the auto-detected DB) or a full container path; persists per-instance via **Save Credentials** and is honoured by manual runs, scheduled runs, and restore. New `db_path_override` field on `/api/instances`.

### Changed
- **Unraid Community Applications readiness** — the template (`templates/unraid.xml`) is now ready to submit to [CA](https://ca.unraid.net/):
  - Added a template **`<Icon>`** (`templates/starr-icon.png`, a 256×256 PNG) — CA rejects templates without one.
  - Added `<Beta>False</Beta>`.
  - The **Docker socket mount is now optional** (`Required="false"`) instead of mandatory, with a description that spells out the root-equivalent trade-off and the shutdown-API fallback — CA moderators scrutinise forced `docker.sock` mounts, and the app works without it.
  - `SECRET_KEY` description rewritten to match the app's actual security behaviour (unset ⇒ unauthenticated + insecure banner).

## [1.2.2] — 2026-07-01

Patch release: a dashboard density pass.

### Changed
- **Dashboard density pass** — action buttons now live in the panel bar they belong to instead of a separate row below the panel, matching the "Add Schedule" pattern already used by Scheduled Repairs:
  - **Run Repair** / **Stop** move into the Repair Operations bar (next to the Dry Run / Skip Shutdown toggles); the last-run pill moves there too.
  - **Refresh Backups** moves into the Backups bar (next to "Stored in /backups").
  - **Detect *arr containers** / **Save Credentials** / **Test Connection** move into the Connection bar (next to the connection-status text).
  - The now-empty standalone action row between Repair Operations and Trends is removed.
  - **URL** and **API Key** sit side-by-side on wide viewports instead of stacking full-width (existing `600px` breakpoint still stacks them on narrow screens).

## [1.2.1] — 2026-07-01

Patch release: a shipped-defaults security fix, plus the release-automation
work that lets this very release publish itself.

### Security
- **Shipped `SECRET_KEY` defaults now match the app's "insecure default" sentinel** — `docker-compose.yml` and `.env.example` previously defaulted to `change-me` / `change-me-to-a-random-string`, which are *different* strings from the one `server.py` checks for (`change-me-in-production`). That meant an out-of-the-box `docker compose up` with no `.env` edits was silently **authenticating every request against a value published in this repo**, with no warning and no "insecure" banner in the dashboard (both only fire when the key equals the exact sentinel). Both files now default to the sentinel, so an unset key is loud and visible instead of quietly insecure.
- **API-key comparison is now constant-time** (`hmac.compare_digest`) instead of `!=`, closing a minor timing side-channel in `require_api_key`.

### Changed
- **Releases are now fully automatic** — merging a release PR (one that flips `CHANGELOG.md`'s `[Unreleased]` section to `[X.Y.Z]`) is enough. CI detects the version flip, publishes the version pins (`X.Y.Z` / `X.Y` / `X`), moves **`latest`** to that release, auto-creates the `vX.Y.Z` git tag, and creates the matching GitHub Release — all in the same workflow run. Manually pushing a `v*.*.*` tag still works (useful for re-running the release pipeline). (`.github/workflows/docker-publish.yml`)

### Upgrade note
If your `.env` (or compose override) still has `SECRET_KEY` unset or set to the
old shipped default (`change-me` / `change-me-to-a-random-string`), set it to
a real random value now — e.g. `openssl rand -hex 32`. Those old values are
**not** treated as "insecure default" by the app, so requests against them
were being silently authenticated.

## [1.2.0] — 2026-06-24

UX rework — the dashboard is much calmer at rest, with secondary panels
collapsed by default and controls grouped where they're actually used.
Plus a release-automation rework so `latest` finally means "newest
release" and every tag auto-creates a GitHub Release.

### Added
- **`edge` image tag** (#47) — every push to `main` publishes
  `krippler52/starr:edge` and `ghcr.io/krippler/starr:edge`, so testing
  the tip of `main` ahead of a release no longer means building locally.

### Changed
- **`latest` tag now tracks the newest released version, not every commit**
  (#47) — only `v*.*.*` tag pushes move `latest`. Merges to `main` update
  `edge` instead. Each version tag also **auto-creates a GitHub Release**
  with notes pulled from this changelog. (`.github/workflows/docker-publish.yml`)
- **Dashboard de-clutter** (#48) — Trends, Backups, Schedules, and
  Notifications panels are collapsible (collapsed by default, state saved
  per browser). The 1→6 phase indicator only renders during a repair. The
  shutdown warning collapses to a single muted line at rest and only blows
  up to the loud orange treatment when Skip Shutdown is checked or no
  container was discovered. Lazy-load: collapsed sections fetch on first
  expand instead of at unlock.
- **Repair Operations panel** (#51) — collapsible, moved to sit directly
  above the Run Repair button so "pick your ops" lives next to "run". The
  Dry Run + Skip Shutdown toggles stay in the panel header for one-click
  access; a small `"3 selected"` chip in the title shows current state at
  a glance.
- **Backup retention controls** (#49) consolidated into a single **Retention**
  card at the top of the Backups panel. Two clearly-labelled columns:
  *Default for all instances* and *This instance: <name>* — with plain-English
  source captions (`Saved here` / `From MAX_BACKUP_AGE_DAYS env var`;
  `Using default (X days)` / `Overrides the default`). No more split between
  panel header and a vague "current instance" row.
- **Lock button** (#50) moved out of the Connection panel's action row up to
  the header next to the status badge, where session controls belong.

### Fixed
- **Last-run pill and trend charts** now correctly scope to the selected
  instance instead of bleeding across named extras of the same app (#52).
  Switching tabs (Sonarr → Radarr → …) reliably updates the pill; the
  default tab no longer shows runs that actually came from a named extra
  (e.g. `sonarr-4k`).

## [1.1.2] — 2026-06-24

Adjustable backup retention — globally and per instance — plus a comprehensive
docs and UI-label sweep.

### Added
- **Adjustable backup retention from the dashboard** (#43) — picker in the Backups panel header with `7 / 14 / 30 / 60 / 90 / 180 / 365 / Forever`. New endpoints `GET` / `PUT /api/settings`. `MAX_BACKUP_AGE_DAYS` env var remains the boot fallback.
- **Per-instance backup retention** (#44) — each instance can override the global retention. A daily-backed Sonarr can keep 14 days while a weekly Sonarr-4K keeps a year, without one prune window chopping the other's files. New endpoint `PUT /api/instances/<id>/retention` (`null` clears the override). `/api/instances` payload now includes `retention_days` (override) and `retention_effective_days` (what would actually apply).

### Changed
- **README rewritten** (#45) to reflect everything shipped since the `/data/<app>` era — single `/appdata` mount + Docker auto-discovery, multi-instance, run history, trends, restore, mid-VACUUM cancel, notifications, retention, Save Credentials. Complete API reference grouped by area.
- **UI labels and tooltips** (#45) tightened around the instance model, retention inheritance, and the Save Credentials affordance.
- **Unraid template overview** (#45) updated with the full current feature set.

## [1.1.1] — 2026-06-22

Patch release fixing credential handling for scheduled repairs.

### Fixed
- **API keys typed in the dashboard now persist and reach scheduled runs**
  (#40) — previously the API Key field was form-only state, so a schedule that
  fired with no `*_APIKEY` env var set failed with `apikey is required`. The UI
  now has a **Save Credentials** button that persists the URL + API Key per
  instance to `.starr-instance-overrides.json`; both manual and scheduled runs
  pick them up. New endpoint: `PUT /api/instances/<id>/credentials`.
- **Default-instance schedules now read the saved override** (#41) — scheduled
  runs targeting the env/discovery default carry an empty `instance_id`, and the
  override lookup was skipped for them. It now falls back to the app name (the
  default instance's id), so `Run now` succeeds after saving credentials.
- **Schedule rows surface the failure reason** (#40) — when a schedule's last
  status is `error`, the actual message is shown under the row instead of just
  the word "error".

## [1.1.0] — 2026-06-21

A large feature drop centred on **multiple instances per app** plus a new
**run-history layer** that powers a last-run pill, pre-repair time estimate, and
DB-size / repair-duration trend charts. Fully backwards-compatible: existing
single-instance installs see no behaviour change without action.

### Added
- **Multiple instances per app** (#36, #37) — manage more than one of the same
  *arr (e.g. a second Sonarr at a different URL). Each app keeps its env /
  Docker-discovery "default" instance; extras are added/edited/deleted from the
  new instance selector under the app tabs. Backups, schedules, history, and
  restore are all per-instance.
  - New endpoints: `GET/POST /api/instances`, `PUT/DELETE /api/instances/<id>`.
- **Run history store** (#32) — every completed repair is recorded to
  `.starr-history.json` in `BACKUP_DIR` (rolling cap of 500). Drives:
  - **Last-run pill** in the action row (latest result + how long ago).
  - **Pre-repair time estimate** ("~2m, based on 4 runs"), computed from real
    past runs (excludes skip-if-clean / errored / dry-run records).
  - New endpoints: `GET /api/history`, `GET /api/history/estimate`.
- **Trend charts** (#34) — two per-app/per-instance inline-SVG sparklines:
  repair duration and database size over the last 30 runs.
- **Instance-scoped history & trends** (#38) — named extras (e.g. `sonarr-4k`)
  get their own pill, estimate, and charts; the default falls back to per-app
  so pre-upgrade records still surface. `?instance=` query support added to
  history endpoints.
- **Webhook on completion** (#33) — fires a JSON POST to a configurable URL
  alongside the existing Apprise + Signal notifications.

### Changed
- **Stop now actually cancels a mid-VACUUM / REINDEX** (#35) — the active SQLite
  connection is published on the job state and `api_stop` calls
  `Connection.interrupt()` from the request thread; verified to abort a real
  783 MB VACUUM in ~9 ms. The cancelled op is recorded as `aborted` and its
  backup is renamed `…_aborted.db[.zst]` instead of the previous misleading
  `…_clean`. `api_stop` response includes `{"interrupted": bool}`.

### Fixed
- **Scheduler accepts the newer *arr apps** (#33) — `VALID_APPS` had only
  Sonarr / Radarr / Lidarr / Sportarr; schedules can now also be created for
  Readarr, Prowlarr, Whisparr, and Bazarr.

### Notes
- `.starr-instances.json` is created on demand alongside the existing
  `.starr-schedules.json`, `.starr-notify.json`, and `.starr-history.json` in
  `BACKUP_DIR` — no new mount points.
- Records written by 1.0.x have no `instance` field; the history filter treats
  them as belonging to the default instance so the upgrade is seamless.

## [1.0.4]

Previous tagged release. See git history.
