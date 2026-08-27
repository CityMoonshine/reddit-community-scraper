"""SQLite access shared by the api and worker containers.

Two processes on one database file is the whole reason this module exists.
Every connection turns on WAL so the API's reads don't block the worker's
writes, and sets a busy timeout so a sweep landing mid-request waits its turn
instead of raising 'database is locked'.
"""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from app.config import DB_PATH

# Long enough to ride out a sweep's write burst, short enough that a genuine
# deadlock still surfaces rather than hanging a request forever.
BUSY_TIMEOUT_MS = 10000


def get_connection():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row

    # WAL is a property of the file, not the connection, so this is a no-op
    # after the first time. Cheap enough to assert on every connect.
    connection.execute('PRAGMA journal_mode=WAL;')
    connection.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS};')
    connection.execute('PRAGMA foreign_keys=ON;')

    return connection


@contextmanager
def connection_scope():
    """Commit on success, roll back on error, always close.

    sqlite3's own `with connection:` manages the transaction but leaves the
    connection open - fine for a script, a slow leak in a long-lived API.
    """
    connection = get_connection()

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def table_exists(connection, name):
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (name,)
    ).fetchone()
    return row is not None


def wait_for_schema(tables=('Users', 'Communities', 'Posts', 'MonitorRuns',
                            'WorkerStatus'),
                    timeout=60, interval=1.0):
    """Block until the API container has created the schema.

    Both services start at once under compose; the API owns schema creation and
    the worker just needs to not race it.
    """
    deadline = time.monotonic() + timeout

    while True:
        try:
            connection = get_connection()
            try:
                if all(table_exists(connection, name) for name in tables):
                    return True
            finally:
                connection.close()
        except sqlite3.Error:
            pass

        if time.monotonic() >= deadline:
            return False

        print('waiting for schema...', flush=True)
        time.sleep(interval)
