# Changelog

All notable changes are documented here. Releases follow [SemVer](https://semver.org).
Image tags published to Docker Hub (`krippler52/starr`) and GHCR (`ghcr.io/krippler/starr`).

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
