#!/usr/bin/env bash
#
# Two jobs, in order:
#
#   1. As root: make the /data bind mount writable by pwuser, then drop
#      privileges. The mount's ownership comes from the host, so fixing it
#      here beats asking the operator to chown by hand on every VPS.
#   2. As pwuser: start a virtual X display and run the command inside it.
#      Chromium is genuinely headed, it just draws into Xvfb's framebuffer.
#
set -euo pipefail

RUN_USER="${RUN_USER:-pwuser}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    chown -R "${RUN_USER}:${RUN_USER}" /data 2>/dev/null || true

    uid="$(id -u "${RUN_USER}")"
    gid="$(id -g "${RUN_USER}")"

    # Re-exec this same script as the unprivileged user, which then takes the
    # branch below. Keeping the sandbox is why we don't just stay root.
    exec setpriv --reuid="${uid}" --regid="${gid}" --init-groups "$0" "$@"
fi

# --auto-servernum picks a free display number, so a restarted container never
# collides with a stale lock file on the mount.
exec xvfb-run --auto-servernum --server-args="-screen 0 1440x900x24" "$@"
