#!/bin/sh
# Starr DB Repair container entrypoint.
#
# Runs as root just long enough to:
#   1. Reconcile the `starr` user's UID/GID with PUID/PGID (Unraid uses
#      99:100 (nobody:users) by convention; LSIO containers do the same).
#      Defaulting here matches what most *arr containers run as on Unraid,
#      so /data mounts the user already configured Just Work.
#   2. Make /backups writable by that UID — this is Starr's own output dir,
#      so it's safe to chown unconditionally.
#
# We deliberately do NOT chown /data — those are the *arr apps' own config
# directories, owned by their UIDs. Changing ownership would break the
# upstream app. The user is expected to set PUID/PGID to match.
#
# Finally, drop privileges and exec the real command as `starr`.

set -e

PUID=${PUID:-99}
PGID=${PGID:-100}

# Reconcile UID/GID. -o lets us assign non-unique IDs which is fine inside a
# container (no other accounts exist).
if [ "$(id -u starr)" != "$PUID" ] || [ "$(id -g starr)" != "$PGID" ]; then
    groupmod -o -g "$PGID" starr
    usermod  -o -u "$PUID" starr
fi

# Ensure /backups is writable by the runtime user. Silently ignored if the
# path doesn't exist (e.g. someone replaced the BACKUP_DIR env).
if [ -d /backups ]; then
    chown -R starr:starr /backups || true
fi

# Helpful one-line summary in container logs so users can spot UID mismatches.
echo "[entrypoint] running as starr (uid=$(id -u starr) gid=$(id -g starr))"

exec gosu starr "$@"
