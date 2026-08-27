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
