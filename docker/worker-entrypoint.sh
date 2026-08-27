#!/usr/bin/env bash
#
# Three jobs, in order:
#
#   1. As root: make the /data bind mount usable by the service user. See
#      shared-data.sh for why this is a shared *group*, not an ownership grab.
#   2. Drop privileges to pwuser, taking a correct HOME with us - setpriv
#      changes the uid but not the environment, and Chromium dies with SIGTRAP
#      if HOME still points at root-owned /root.
#   3. Bring up a virtual X display and exec the command inside it.
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

# shellcheck source=/dev/null
. /usr/local/lib/shared-data.sh

RUN_USER="${RUN_USER:-pwuser}"
XVFB_DISPLAY="${XVFB_DISPLAY:-99}"
XVFB_SCREEN="${XVFB_SCREEN:-1440x900x24}"

if [ "$(id -u)" = "0" ]; then
    prepare_data_dir

    # setpriv changes the uid but does NOT touch the environment, so HOME
    # would still say /root - which is 0700 root-owned. Chromium then fails to
    # write its profile and crashpad dir and dies with SIGTRAP, while the same
    # launch works fine under `docker exec` because that runs as root.
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

    # --init-groups is what picks up the shared APP_GID membership granted in
    # the Dockerfile; without it the supplementary group is dropped here and
    # /data goes read-only again.
    exec setpriv --reuid="${uid}" --regid="${gid}" --init-groups "$0" "$@"
fi

assert_data_writable || exit 1

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
echo "[entrypoint] Xvfb ready, DISPLAY=${DISPLAY}; running as $(id -un) $(id -G)"

# Tear Xvfb down with us, so a restart doesn't leave an orphan holding :99.
trap 'kill ${XVFB_PID} 2>/dev/null || true' EXIT

exec "$@"
