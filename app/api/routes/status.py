"""System status - is anything actually working, and what is it doing?

The single most useful thing here is worker liveness. The worker has no network
surface, so if its container is down the only symptom is that queued runs never
start. Without this endpoint that looks exactly like "nothing scheduled yet".
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from app.config import QUEUE_POLL_SECONDS, SCRAPE_BACKEND, SWEEP_INTERVAL_MINUTES
from app.db import connection_scope

router = APIRouter(prefix='/api')

# Miss this many polls and we call it down. Three gives enough slack for a slow
# write or a container restart without flapping.
MISSED_POLLS_BEFORE_DOWN = 3


def parse_ts(value):
    if not value:
        return None
    try:
        text = str(value).replace(' ', 'T')
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def humanise(seconds):
    """'12s' / '4m' / '3h' - a raw second count stops being readable fast."""
    if seconds < 90:
        return f'{int(seconds)}s'
    if seconds < 5400:
        return f'{int(seconds / 60)}m'
    if seconds < 172800:
        return f'{int(seconds / 3600)}h'
    return f'{int(seconds / 86400)}d'


def worker_health(row):
    """Turn a heartbeat timestamp into something the dashboard can act on."""
    if row is None or not row['last_heartbeat_at']:
        return {
            'state': 'never started',
            'online': False,
            'seconds_since_heartbeat': None,
            'message': 'The worker has never checked in. Is the worker '
                       'container running? Try: docker compose logs worker',
        }

    beat = parse_ts(row['last_heartbeat_at'])
    now = datetime.now(timezone.utc)
    age = (now - beat).total_seconds() if beat else None

    tolerance = QUEUE_POLL_SECONDS * MISSED_POLLS_BEFORE_DOWN
    online = age is not None and age <= tolerance

    if row['state'] == 'stopped':
        message = 'The worker shut down cleanly. Sweeps will not run until it restarts.'
    elif not online:
        message = (f'No heartbeat for {humanise(age)} (expected every '
                   f'{QUEUE_POLL_SECONDS}s). Sweeps will not run. '
                   f'Try: docker compose logs worker')
    elif row['state'] == 'sweeping':
        message = row['activity'] or 'sweeping'
    else:
        message = row['activity'] or 'idle'

    return {
        'state': row['state'],
        'online': online,
        'seconds_since_heartbeat': int(age) if age is not None else None,
        'message': message,
    }


@router.get('/status')
def status():
    with connection_scope() as connection:
        worker = connection.execute(
            'SELECT * FROM WorkerStatus WHERE id = 1;'
        ).fetchone()

        last_run = connection.execute(
            '''
            SELECT r.*, c.name AS only_community_name
            FROM MonitorRuns r
            LEFT JOIN Communities c ON c.id = r.only_community_id
            WHERE r.status NOT IN ('queued', 'running')
            ORDER BY r.id DESC LIMIT 1;
            '''
        ).fetchone()

        active = connection.execute(
            """
            SELECT r.*, c.name AS only_community_name
            FROM MonitorRuns r
            LEFT JOIN Communities c ON c.id = r.only_community_id
            WHERE r.status IN ('queued', 'running')
            ORDER BY r.id LIMIT 1;
            """
        ).fetchone()

        totals = connection.execute(
            '''
            SELECT
                (SELECT COUNT(*) FROM Posts) AS posts,
                (SELECT COUNT(*) FROM Communities) AS communities,
                (SELECT COUNT(*) FROM Communities WHERE monitor_enabled = 1) AS monitored,
                (SELECT COUNT(*) FROM Posts
                  WHERE first_seen_at >= datetime('now', '-1 day')) AS new_24h,
                (SELECT COUNT(*) FROM MonitorRuns WHERE status = 'failed') AS failed_runs;
            '''
        ).fetchone()

        # Communities that produced nothing on their most recent attempt. This
        # is the "something is quietly broken" list.
        failing = connection.execute(
            '''
            SELECT i.community_name, i.status, i.error, i.run_id
            FROM MonitorRunItems i
            WHERE i.id IN (
                SELECT MAX(id) FROM MonitorRunItems
                WHERE community_id IS NOT NULL
                GROUP BY community_id
            ) AND i.status != 'ok'
            ORDER BY i.community_name;
            '''
        ).fetchall()

    health = worker_health(worker)

    # Anything that should make the dashboard shout rather than whisper.
    alerts = []

    if not health['online']:
        alerts.append({'level': 'error', 'text': health['message']})

    if last_run and last_run['status'] == 'failed':
        alerts.append({
            'level': 'error',
            'text': f"Last sweep (#{last_run['id']}) failed: "
                    f"{last_run['error'] or 'no detail recorded'}",
        })

    for row in failing:
        if row['status'] == 'blocked':
            alerts.append({
                'level': 'error',
                'text': f"r/{row['community_name']}: Reddit served a block page. "
                        f"On a VPS this is usually the datacenter IP, not the "
                        f"browser - try SCRAPE_BACKEND=api.",
            })
        else:
            alerts.append({
                'level': 'warn',
                'text': f"r/{row['community_name']}: last sweep returned "
                        f"{row['status']}"
                        + (f" - {row['error']}" if row['error'] else ''),
            })

    return {
        'worker': {
            **health,
            'backend': worker['backend'] if worker else None,
            'current_run_id': worker['current_run_id'] if worker else None,
            'current_community': worker['current_community'] if worker else None,
            'next_sweep_at': worker['next_sweep_at'] if worker else None,
            'started_at': worker['started_at'] if worker else None,
            'last_error': worker['last_error'] if worker else None,
        },
        'active_run': dict(active) if active else None,
        'last_run': dict(last_run) if last_run else None,
        'totals': dict(totals),
        'alerts': alerts,
        'config': {
            'sweep_interval_minutes': SWEEP_INTERVAL_MINUTES,
            'queue_poll_seconds': QUEUE_POLL_SECONDS,
            'default_backend': SCRAPE_BACKEND,
        },
    }
