"""The worker's liveness and activity, written to a single database row.

The worker has no network surface at all - it talks to the API only through
SQLite - so this table is the only way the dashboard can answer "is the
scraper alive, and what is it doing right now?".

Written by the worker, read by the API. Deliberately best-effort: a failure to
record status must never take down a sweep that is otherwise working, so every
write here swallows its own errors.
"""

from datetime import datetime, timezone

from app.db import connection_scope


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write(**fields):
    """Update the single status row. Never raises."""
    if not fields:
        return

    fields['last_heartbeat_at'] = utcnow()

    assignments = ', '.join(f'{key} = ?' for key in fields)
    values = list(fields.values())

    try:
        with connection_scope() as connection:
            connection.execute(
                f'UPDATE WorkerStatus SET {assignments} WHERE id = 1;', values
            )
    except Exception as exc:  # noqa: BLE001 - status must not break the sweep
        print(f'[status] could not record status: {exc}', flush=True)


def started(backend, next_sweep_at=None):
    _write(state='idle', backend=backend, started_at=utcnow(),
           current_run_id=None, current_community=None,
           activity='waiting for the next sweep', next_sweep_at=next_sweep_at,
           last_error=None)


def heartbeat(next_sweep_at=None):
    """Called on every queue poll. Staleness is what the API reads as 'down'."""
    if next_sweep_at is not None:
        _write(next_sweep_at=next_sweep_at)
    else:
        _touch()


def _touch():
    try:
        with connection_scope() as connection:
            connection.execute(
                'UPDATE WorkerStatus SET last_heartbeat_at = ? WHERE id = 1;',
                (utcnow(),),
            )
    except Exception as exc:  # noqa: BLE001
        print(f'[status] heartbeat failed: {exc}', flush=True)


def sweeping(run_id, community=None, activity=None):
    _write(state='sweeping', current_run_id=run_id,
           current_community=community, activity=activity)


def activity(text):
    """Progress within the community currently being fetched."""
    _write(activity=text)


def idle(next_sweep_at=None, last_error=None):
    fields = {
        'state': 'idle',
        'current_run_id': None,
        'current_community': None,
        'activity': 'waiting for the next sweep',
        'last_error': last_error,
    }

    if next_sweep_at is not None:
        fields['next_sweep_at'] = next_sweep_at

    _write(**fields)


def read():
    """The current row, or None. Used by the API."""
    with connection_scope() as connection:
        return connection.execute('SELECT * FROM WorkerStatus WHERE id = 1;').fetchone()


def self_check():
    """Prove a status write actually lands, and explain it loudly if not.

    Every write in this module swallows its own errors so a status problem can
    never kill a working sweep. The cost of that is a silent failure mode: the
    worker runs fine, the dashboard says "offline" forever, and nothing
    connects the two. This is called once at startup to make that case obvious
    in the logs.
    """
    try:
        with connection_scope() as connection:
            row = connection.execute(
                'SELECT last_heartbeat_at FROM WorkerStatus WHERE id = 1;'
            ).fetchone()

            if row is None:
                connection.execute(
                    "INSERT OR IGNORE INTO WorkerStatus (id, state) VALUES (1, 'starting');"
                )

        before = utcnow()
        _touch()

        with connection_scope() as connection:
            after = connection.execute(
                'SELECT last_heartbeat_at FROM WorkerStatus WHERE id = 1;'
            ).fetchone()

        if after is None or not after['last_heartbeat_at']:
            raise RuntimeError('the heartbeat write did not land')

        print(f'[status] heartbeat write verified at {before}', flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print('', flush=True)
        print('=' * 70, flush=True)
        print('WORKER CANNOT RECORD ITS STATUS', flush=True)
        print(f'  {type(exc).__name__}: {exc}', flush=True)
        print('', flush=True)
        print('  Sweeps may still run, but the dashboard will show this worker', flush=True)
        print('  as OFFLINE no matter what it is doing.', flush=True)
        print('', flush=True)
        print('  Most likely: the api container is running older code and has', flush=True)
        print('  not created the WorkerStatus table. Rebuild both together:', flush=True)
        print('      docker compose up -d --build', flush=True)
        print('  Failing that, check /data is writable by uid 1000.', flush=True)
        print('=' * 70, flush=True)
        print('', flush=True)
        return False
