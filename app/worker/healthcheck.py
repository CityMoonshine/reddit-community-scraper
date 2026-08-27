"""Container healthcheck for the worker.

The worker's failure mode is not crashing - it is hanging. A wedged entrypoint
or a stuck Chromium leaves the container reporting "Up" forever while nothing
runs, which is exactly what made the xvfb-run-as-PID-1 hang so hard to spot:
no crash, no restart, no logs, healthy-looking `docker compose ps`.

So health here means "the heartbeat is recent", not "the process exists".

    exit 0 - heartbeat is fresh
    exit 1 - stale, missing, or unreadable
"""

import sys
from datetime import datetime, timezone

from app.config import QUEUE_POLL_SECONDS
from app.db import connection_scope

# Generous: a sweep keeps the heartbeat fresh through status updates, so this
# only trips when the worker is genuinely not looping.
STALE_AFTER = max(90, QUEUE_POLL_SECONDS * 6)


def main():
    try:
        with connection_scope() as connection:
            row = connection.execute(
                'SELECT last_heartbeat_at, state FROM WorkerStatus WHERE id = 1;'
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        print(f'unhealthy: cannot read status: {exc}')
        return 1

    if row is None or not row['last_heartbeat_at']:
        print('unhealthy: no heartbeat recorded yet')
        return 1

    try:
        beat = datetime.fromisoformat(str(row['last_heartbeat_at']).replace(' ', 'T'))
        if beat.tzinfo is None:
            beat = beat.replace(tzinfo=timezone.utc)
    except ValueError:
        print(f'unhealthy: unparseable heartbeat {row["last_heartbeat_at"]!r}')
        return 1

    age = (datetime.now(timezone.utc) - beat).total_seconds()

    if age > STALE_AFTER:
        print(f'unhealthy: last heartbeat {int(age)}s ago (limit {STALE_AFTER}s)')
        return 1

    print(f'healthy: {row["state"]}, heartbeat {int(age)}s ago')
    return 0


if __name__ == '__main__':
    sys.exit(main())
