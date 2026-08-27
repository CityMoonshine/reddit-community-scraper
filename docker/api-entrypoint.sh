#!/usr/bin/env bash
#
# The ./data bind mount arrives owned by whoever owns it on the host - usually
# root on a fresh VPS. The api runs as uid 1000, so without this it cannot
# create or write portal.db, init_db() raises at import, uvicorn exits, and
# nginx reports 502 with nothing useful in its own log.
#
set -euo pipefail

RUN_USER="${RUN_USER:-portal}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    chown -R "${RUN_USER}:${RUN_USER}" /data 2>/dev/null || true

    # setpriv leaves the environment alone, so HOME would still be /root.
    home="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
    export HOME="${home}"
    export USER="${RUN_USER}"

    uid="$(id -u "${RUN_USER}")"
    gid="$(id -g "${RUN_USER}")"

    exec setpriv --reuid="${uid}" --regid="${gid}" --init-groups "$0" "$@"
fi

exec "$@"
