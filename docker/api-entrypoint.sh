#!/usr/bin/env bash
#
# Prepare the shared /data bind mount, then drop to the service user. See
# shared-data.sh for why this uses a shared group rather than chowning: the
# api and worker images have different uids, and an ownership grab by one
# leaves the other with a read-only database.
#
set -euo pipefail

# shellcheck source=/dev/null
. /usr/local/lib/shared-data.sh

RUN_USER="${RUN_USER:-portal}"

if [ "$(id -u)" = "0" ]; then
    prepare_data_dir

    # setpriv leaves the environment alone, so HOME would still be /root.
    home="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
    export HOME="${home}"
    export USER="${RUN_USER}"

    uid="$(id -u "${RUN_USER}")"
    gid="$(id -g "${RUN_USER}")"

    exec setpriv --reuid="${uid}" --regid="${gid}" --init-groups "$0" "$@"
fi

assert_data_writable || exit 1

echo "[entrypoint] running as $(id -un) $(id -G)"

exec "$@"
