# Shared by both entrypoints. Sourced, not executed.
#
# The api and worker containers write the same SQLite file on the same bind
# mount, as two different users. SQLite in WAL mode does not just write the
# database file - it creates portal.db-wal and portal.db-shm alongside it, so
# BOTH containers need write permission on the directory and on whichever
# sidecar files the other one created first.
#
# Earlier this was handled by each entrypoint chowning /data to its own user.
# That is a race: the two images have different uids (api's `portal` is 1000,
# the Playwright image's `pwuser` is 1001), so whichever container started last
# won and the other lost write access with
# "sqlite3.OperationalError: attempt to write a readonly database".
#
# Instead: both service users belong to APP_GID, /data is group-owned by it and
# setgid so new files inherit the group, and umask 0002 makes them
# group-writable. Ownership no longer matters, so nobody has to win.
#
# Why this never showed up locally: Docker Desktop on WSL2 mounts bind volumes
# 0777 and ignores ownership entirely, so every permission bug here is
# invisible until it runs on a real Linux bind mount.

APP_GID="${APP_GID:-1000}"
DATA_DIR="${DATA_DIR:-/data}"

# New files (including the WAL sidecars) become group-writable.
umask 0002

prepare_data_dir() {
    mkdir -p "${DATA_DIR}"

    # Group, not owner. Both users are in APP_GID, so neither has to own it.
    chgrp -R "${APP_GID}" "${DATA_DIR}" 2>/dev/null || true

    # 2775: setgid, so files created here inherit APP_GID no matter which
    # container creates them.
    chmod 2775 "${DATA_DIR}" 2>/dev/null || true

    # Existing database and sidecars predate the setgid bit.
    find "${DATA_DIR}" -maxdepth 1 -type f -name 'portal.db*' \
        -exec chmod 0664 {} + 2>/dev/null || true
}

# Prove we can actually write, and say exactly what to do if not. A silent
# permission failure here surfaces much later as a confusing SQLite error.
assert_data_writable() {
    probe="${DATA_DIR}/.write-probe.$$"

    # Subshell: the shell reports a failed redirection itself, before the
    # command's own 2>/dev/null can suppress it.
    if ( : > "${probe}" ) 2>/dev/null; then
        rm -f "${probe}"
        return 0
    fi

    cat >&2 <<BANNER

======================================================================
CANNOT WRITE TO ${DATA_DIR}

  Running as: $(id)
  Directory : $(ls -ld "${DATA_DIR}" 2>/dev/null || echo 'missing')

  This container needs write access to the SQLite database and to the
  directory holding it - WAL mode creates portal.db-wal and
  portal.db-shm next to it.

  On the host, from the repo directory:

      sudo chgrp -R ${APP_GID} ./data
      sudo chmod -R g+w ./data
      sudo chmod g+s ./data

  If Docker is running rootless, the container cannot change ownership
  itself, so that host-side fix is required.
======================================================================

BANNER
    return 1
}
