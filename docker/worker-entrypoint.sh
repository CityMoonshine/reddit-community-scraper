#!/usr/bin/env bash
#
# Two jobs, in order:
#
#   1. As root: make the /data bind mount writable by pwuser, then drop
#      privileges. The mount's ownership comes from the host, so fixing it
#      here beats asking the operator to chown by hand on every VPS.
#   2. As pwuser: bring up a virtual X display and exec the command inside it.
#      Chromium is genuinely headed, it just draws into Xvfb's framebuffer.
#
# Xvfb is started directly rather than via `xvfb-run`. xvfb-run waits for a
# SIGUSR1 readiness signal from Xvfb before running its command, and that
# handshake does not survive being PID 1 in a container: it starts Xvfb and
# then hangs forever without ever launching the command. The symptom is a
# container that looks healthy, logs absolutely nothing, and has no worker
# process inside it.
#
set -euo pipefail

RUN_USER="${RUN_USER:-pwuser}"
XVFB_DISPLAY="${XVFB_DISPLAY:-99}"
XVFB_SCREEN="${XVFB_SCREEN:-1440x900x24}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    chown -R "${RUN_USER}:${RUN_USER}" /data 2>/dev/null || true

    # setpriv changes the uid but does NOT touch the environment, so HOME
    # would still say /root - which is 0700 root-owned. Chromium then fails to
    # write its profile and crashpad dir and dies with SIGTRAP, while the same
    # launch works fine under `docker exec` because that runs as root. Set the
    # target user's environment explicitly before dropping.
    home="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
    export HOME="${home}"
    export USER="${RUN_USER}"
    export LOGNAME="${RUN_USER}"
    export XDG_CACHE_HOME="${home}/.cache"
    export XDG_CONFIG_HOME="${home}/.config"
    mkdir -p "${XDG_CACHE_HOME}" "${XDG_CONFIG_HOME}"
    chown -R "${RUN_USER}:${RUN_USER}" "${home}" 2>/dev/null || true

    uid="$(id -u "${RUN_USER}")"
    gid="$(id -g "${RUN_USER}")"

    # Re-exec this same script as the unprivileged user, which then takes the
    # branch below. Keeping Chromium's sandbox is why we don't just stay root.
    exec setpriv --reuid="${uid}" --regid="${gid}" --init-groups "$0" "$@"
fi

# A hard restart can leave these behind and Xvfb then refuses the display.
rm -f "/tmp/.X${XVFB_DISPLAY}-lock" "/tmp/.X11-unix/X${XVFB_DISPLAY}" 2>/dev/null || true

echo "[entrypoint] starting Xvfb on :${XVFB_DISPLAY} (${XVFB_SCREEN})"
Xvfb ":${XVFB_DISPLAY}" -screen 0 "${XVFB_SCREEN}" -nolisten tcp &
XVFB_PID=$!

# Poll for the socket instead of sleeping a fixed amount: Chromium fails
# confusingly if it starts before the display is accepting connections.
for _ in $(seq 1 100); do
    if [ -e "/tmp/.X11-unix/X${XVFB_DISPLAY}" ]; then
        break
    fi
    if ! kill -0 "${XVFB_PID}" 2>/dev/null; then
        echo "[entrypoint] Xvfb died during startup" >&2
        exit 1
    fi
    sleep 0.1
done

if [ ! -e "/tmp/.X11-unix/X${XVFB_DISPLAY}" ]; then
    echo "[entrypoint] Xvfb never came up on :${XVFB_DISPLAY}" >&2
    exit 1
fi

export DISPLAY=":${XVFB_DISPLAY}"
echo "[entrypoint] Xvfb ready, DISPLAY=${DISPLAY}; exec: $*"

# Tear Xvfb down with us, so a restart doesn't leave an orphan holding :99.
trap 'kill ${XVFB_PID} 2>/dev/null || true' EXIT

exec "$@"
