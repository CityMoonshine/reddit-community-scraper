"""The sweep engine and its scheduler. Runs in its own container.

Because the api and worker are separate images, the API cannot spawn a browser
to service a "Run sweep now" click - it has no Chromium and no Xvfb. So the
queue lives in the database:

    api    inserts a MonitorRuns row with status='queued'
    worker polls for queued rows every QUEUE_POLL_SECONDS and claims one

The hourly cron job goes through the same queue, so there is exactly one code
path that executes a sweep, and the "only one sweep at a time" guard has only
one place to hold.

    python -m app.worker.monitor --loop     # scheduler (what the container runs)
    python -m app.worker.monitor --once     # one sweep now, then exit
"""

import argparse
import sys
import traceback
from datetime import datetime, timedelta, timezone

from app.config import (QUEUE_POLL_SECONDS, SCRAPE_BACKEND, STALE_RUN_MINUTES,
                        SWEEP_INTERVAL_MINUTES)
from app.db import connection_scope, wait_for_schema
from app.ingest.store import upsert_community, upsert_posts
from app.worker import status


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ------------------------------------------------------------------ run state

def reap_stale_runs(connection):
    """Mark abandoned 'running' rows failed.

    A run still in progress after STALE_RUN_MINUTES means the process was
    killed, the container restarted, or Chromium hung. Without this the
    one-sweep-at-a-time guard would wedge permanently.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_RUN_MINUTES)).isoformat()

    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE MonitorRuns
        SET status = 'failed',
            finished_at = ?,
            error = 'abandoned - worker died or container restarted'
        WHERE status = 'running' AND started_at < ?;
        """,
        (utcnow(), cutoff),
    )

    return cursor.rowcount


def enqueue_run(trigger, backend, only_community_id=None):
    """Queue a sweep. Returns the run id, or None if one is already pending.

    Coalescing matters for the cron trigger: if a sweep overruns its slot we
    want the next tick skipped, not a backlog of identical runs.
    """
    with connection_scope() as connection:
        cursor = connection.cursor()

        pending = cursor.execute(
            "SELECT id FROM MonitorRuns WHERE status IN ('queued', 'running') LIMIT 1;"
        ).fetchone()

        if pending:
            return None

        cursor.execute(
            """
            INSERT INTO MonitorRuns (trigger, backend, queued_at, status,
                                     only_community_id)
            VALUES (?, ?, ?, 'queued', ?);
            """,
            (trigger, backend, utcnow(), only_community_id),
        )

        return cursor.lastrowid


def claim_run():
    """Atomically take the oldest queued run. Returns the row, or None."""
    with connection_scope() as connection:
        reap_stale_runs(connection)

        cursor = connection.cursor()

        if cursor.execute(
            "SELECT id FROM MonitorRuns WHERE status = 'running' LIMIT 1;"
        ).fetchone():
            return None

        # The status check in the WHERE clause is what makes this a claim
        # rather than a read-then-write race.
        cursor.execute(
            """
            UPDATE MonitorRuns
            SET status = 'running', started_at = ?
            WHERE id = (
                SELECT id FROM MonitorRuns WHERE status = 'queued'
                ORDER BY id LIMIT 1
            ) AND status = 'queued';
            """,
            (utcnow(),),
        )

        if not cursor.rowcount:
            return None

        return cursor.execute(
            "SELECT * FROM MonitorRuns WHERE status = 'running' ORDER BY id DESC LIMIT 1;"
        ).fetchone()


def finish_run(run_id, outcome, checked, new, refreshed, error=None):
    with connection_scope() as connection:
        connection.execute(
            """
            UPDATE MonitorRuns
            SET finished_at = ?, status = ?, communities_checked = ?,
                posts_new = ?, posts_refreshed = ?, error = ?
            WHERE id = ?;
            """,
            (utcnow(), outcome, checked, new, refreshed, error, run_id),
        )


def record_item(run_id, community_id, name, item_status, new=0, refreshed=0, error=None):
    with connection_scope() as connection:
        connection.execute(
            """
            INSERT INTO MonitorRunItems (
                run_id, community_id, community_name, status,
                posts_new, posts_refreshed, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (run_id, community_id, name, item_status, new, refreshed, error),
        )


def communities_for_run(only_community_id=None):
    """What this run should sweep.

    A manual single-community trigger deliberately ignores monitor_enabled: if
    you explicitly asked for this one, you want it fetched, paused or not.
    """
    with connection_scope() as connection:
        if only_community_id:
            return connection.execute(
                """
                SELECT id, name, monitor_sort, monitor_limit
                FROM Communities WHERE id = ?;
                """,
                (only_community_id,),
            ).fetchall()

        return connection.execute(
            """
            SELECT id, name, monitor_sort, monitor_limit
            FROM Communities
            WHERE monitor_enabled = 1
            ORDER BY name;
            """
        ).fetchall()


def store_result(run_id, community_row_data, posts):
    """One short write transaction per community.

    Deliberately not one transaction for the whole sweep: a browser sweep can
    run for many minutes, and holding a write lock that long would block every
    API read behind it even with WAL.
    """
    with connection_scope() as connection:
        community_id = upsert_community(connection, community_row_data)
        new, refreshed = upsert_posts(connection, community_id, posts, run_id)
        connection.execute(
            'UPDATE Communities SET last_checked_at = ? WHERE id = ?;',
            (utcnow(), community_id),
        )

    return community_id, new, refreshed


# -------------------------------------------------------------------- sweeps

def sweep_browser(run_id, communities):
    """One Chromium window for the whole sweep, reused across communities."""
    from playwright.sync_api import sync_playwright

    from app.ingest.browser import (SUBREDDIT_PAUSE, BlockedError,
                                    browse_subreddit, community_row,
                                    launch_browser, snooze)

    total_new = total_refreshed = checked = 0

    with sync_playwright() as playwright:
        browser, context = launch_browser(playwright)
        page = context.new_page()

        try:
            for index, community in enumerate(communities):
                name = community['name']

                if index:
                    snooze(SUBREDDIT_PAUSE)

                print(f"\nr/{name} ({community['monitor_sort']})", flush=True)
                status.sweeping(run_id, name, f'opening r/{name}')

                try:
                    meta, posts = browse_subreddit(
                        page, name,
                        community['monitor_sort'] or 'new',
                        community['monitor_limit'] or 50,
                        on_progress=status.activity,
                    )
                except BlockedError:
                    # Blocked is about the IP, not this subreddit - every
                    # remaining fetch would hit the same wall.
                    record_item(run_id, community['id'], name, 'blocked',
                                error='Reddit block page')
                    raise
                except Exception as exc:
                    print(f'  r/{name}: {exc}', flush=True)
                    record_item(run_id, community['id'], name, 'error',
                                error=str(exc)[:400])
                    continue

                if meta is None or not posts:
                    record_item(run_id, community['id'], name, 'no_data',
                                error='private, empty, or nothing rendered')
                    continue

                community_id, new, refreshed = store_result(
                    run_id, community_row(meta), posts
                )
                record_item(run_id, community_id, name, 'ok', new, refreshed)

                total_new += new
                total_refreshed += refreshed
                checked += 1

                print(f'  {new} new, {refreshed} refreshed', flush=True)
        finally:
            browser.close()

    return checked, total_new, total_refreshed


def sweep_api(run_id, communities):
    """OAuth path. Needs credentials but survives having no display."""
    from app.ingest.reddit_api import (RedditAuthError, build_client,
                                       fetch_community, fetch_posts)

    total_new = total_refreshed = checked = 0
    client = build_client()

    try:
        for community in communities:
            name = community['name']
            print(f"\nr/{name} ({community['monitor_sort']})", flush=True)
            status.sweeping(run_id, name, f'fetching r/{name} via the OAuth API')

            try:
                meta = fetch_community(client, name)

                if meta is None:
                    record_item(run_id, community['id'], name, 'no_data',
                                error='not readable')
                    continue

                posts = fetch_posts(
                    client, name,
                    community['monitor_sort'] or 'new',
                    community['monitor_limit'] or 50,
                )
            except RedditAuthError:
                raise
            except Exception as exc:
                record_item(run_id, community['id'], name, 'error', error=str(exc)[:400])
                continue

            community_id, new, refreshed = store_result(run_id, meta, posts)
            record_item(run_id, community_id, name, 'ok', new, refreshed)

            total_new += new
            total_refreshed += refreshed
            checked += 1

            print(f'  {new} new, {refreshed} refreshed', flush=True)
    finally:
        client.close()

    return checked, total_new, total_refreshed


def execute_run(run):
    """Do the work for an already-claimed run row."""
    run_id = run['id']
    backend = run['backend'] or SCRAPE_BACKEND

    # sqlite3.Row has no .get(), and the column is absent on rows written
    # before the migration added it.
    only_id = run['only_community_id'] if 'only_community_id' in run.keys() else None

    communities = communities_for_run(only_id)

    if not communities:
        reason = 'community not found' if only_id else 'nothing is being monitored'
        finish_run(run_id, 'ok', 0, 0, 0, error=reason)
        status.idle(last_error=reason)
        print(f'Run {run_id}: {reason}.', flush=True)
        return

    scope = f"r/{communities[0]['name']}" if only_id else f'{len(communities)} communities'
    print(f'Run {run_id}: {scope} via {backend}', flush=True)

    # NB: never name a local `status` in this function - it would shadow the
    # imported status module for the whole body, including the calls above it.
    status.sweeping(run_id, None, f'starting {scope} via {backend}')

    try:
        sweep = sweep_browser if backend == 'browser' else sweep_api
        checked, new, refreshed = sweep(run_id, communities)
    except Exception as exc:
        traceback.print_exc()
        finish_run(run_id, 'failed', 0, 0, 0, str(exc)[:400])
        status.idle(last_error=str(exc)[:400])
        return

    outcome = 'ok' if checked == len(communities) else 'partial'
    finish_run(run_id, outcome, checked, new, refreshed)
    status.idle(last_error=None if outcome == 'ok' else
                f'{len(communities) - checked} of {len(communities)} communities failed')

    print(f'\nRun {run_id} {outcome}: {checked}/{len(communities)} checked, '
          f'{new} new, {refreshed} refreshed', flush=True)


def process_queue():
    """Claim and execute one pending run, if there is one. The poll job."""
    run = claim_run()

    if run is None:
        return None

    execute_run(run)
    return run['id']


def resolve_community(name):
    """Look up a community by name for the CLI's --community flag."""
    with connection_scope() as connection:
        row = connection.execute(
            'SELECT id, name FROM Communities WHERE name = ? COLLATE NOCASE;',
            (name.strip().lstrip('r/').strip('/'),),
        ).fetchone()

    return row


def run_once(backend=None, community=None):
    """Queue a sweep and run it immediately, for CLI use."""
    backend = backend or SCRAPE_BACKEND
    only_id = None

    if community:
        row = resolve_community(community)

        if row is None:
            print(f'No community named {community!r}. Add it on the dashboard '
                  f'first, or check the spelling.', flush=True)
            return None

        only_id = row['id']

    run_id = enqueue_run('manual', backend, only_id)

    if run_id is None:
        print('A sweep is already queued or running.', flush=True)
        return None

    return process_queue()


# ------------------------------------------------------------------ schedule

def loop(interval_minutes, backend):
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BlockingScheduler(timezone='UTC')

    if interval_minutes == 60:
        trigger = CronTrigger(minute=0)
        description = 'every hour, on the hour'
    else:
        trigger = IntervalTrigger(minutes=interval_minutes)
        description = f'every {interval_minutes} minutes'

    # The cron job only enqueues; the poll job below is what executes. Keeps
    # scheduled and manual sweeps on one path.
    scheduler.add_job(
        enqueue_run,
        trigger=trigger,
        kwargs={'trigger': 'cron', 'backend': backend},
        id='enqueue_sweep',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    def next_sweep_at():
        job = scheduler.get_job('enqueue_sweep')
        return job.next_run_time.isoformat() if job and job.next_run_time else None

    def tick():
        # Heartbeat first: if the sweep below runs long, the dashboard still
        # sees a recent beat from before it started, and the sweep itself
        # keeps the row fresh through status.sweeping/activity.
        status.heartbeat(next_sweep_at())
        process_queue()

    scheduler.add_job(
        tick,
        trigger=IntervalTrigger(seconds=QUEUE_POLL_SECONDS),
        id='process_queue',
        # A sweep runs far longer than the poll interval; without this
        # APScheduler would try to start a second one on the next tick.
        max_instances=1,
        coalesce=True,
    )

    status.started(backend)

    print(f'Worker up. Sweeps {description}, backend={backend}. '
          f'Queue polled every {QUEUE_POLL_SECONDS}s.', flush=True)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print('\nWorker stopped.', flush=True)


def main():
    parser = argparse.ArgumentParser(description='Sweep monitored communities.')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--loop', action='store_true')
    parser.add_argument('--interval', type=int, default=SWEEP_INTERVAL_MINUTES)
    parser.add_argument('--backend', default=SCRAPE_BACKEND, choices=('browser', 'api'))
    parser.add_argument('--community', default=None,
                        help='Sweep only this subreddit (implies --once). '
                             'Runs even if the community is paused.')
    args = parser.parse_args()

    if args.community and not args.loop:
        args.once = True

    if not args.once and not args.loop:
        parser.error('Pass --once or --loop')

    if not wait_for_schema():
        print('Schema never appeared - is the api container running?', file=sys.stderr)
        return 1

    if args.loop:
        loop(args.interval, args.backend)
    else:
        run_once(args.backend, args.community)

    return 0


if __name__ == '__main__':
    sys.exit(main())
