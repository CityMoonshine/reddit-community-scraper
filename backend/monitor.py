"""Scheduled sweeps of the monitored communities.

The dashboard at /monitor decides *what* gets watched; this decides *when* and
does the fetching. Every sweep writes a MonitorRuns row plus one
MonitorRunItems row per community, so a subreddit that goes private shows up as
a failed item instead of vanishing into an aggregate.

    python monitor.py --once                 # one sweep, then exit
    python monitor.py --loop                 # hourly, on the hour, until Ctrl-C
    python monitor.py --loop --interval 15   # every 15 minutes instead
    python monitor.py --once --backend api   # OAuth instead of the browser

Backends:

    browser  (default)  Playwright, no credentials, but needs a real desktop
                        session - see the warning under --loop below.
    api                 OAuth; needs .env credentials but runs truly headless.

IMPORTANT for scheduling: the browser backend drives a *headed* Chromium, because
Reddit serves headless Chromium a block page. Windows Task Scheduler jobs set to
"run whether user is logged on or not" have no interactive desktop, so the
browser backend will fail there. Either keep `--loop` running in a logged-in
session, or schedule `--backend api`.
"""

import argparse
import sqlite3
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / 'portal.db')

# A run still marked 'running' after this long is assumed dead - the process was
# killed, the machine slept, Chromium hung. Reaped on the next sweep so the
# dashboard doesn't show a phantom run in progress forever.
STALE_RUN_MINUTES = 45

DEFAULT_INTERVAL_MINUTES = 60


def get_connection():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ------------------------------------------------------------------ run state

def reap_stale_runs(connection):
    """Mark abandoned 'running' rows as failed before starting a new sweep."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_RUN_MINUTES)).isoformat()

    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE MonitorRuns
        SET status = 'failed',
            finished_at = ?,
            error = 'abandoned - process died or machine slept'
        WHERE status = 'running' AND started_at < ?;
        """,
        (utcnow(), cutoff),
    )
    connection.commit()

    return cursor.rowcount


def run_in_progress(connection):
    """A live sweep, if one is already going. Two browsers fighting is no good."""
    reap_stale_runs(connection)

    cursor = connection.cursor()
    cursor.execute(
        "SELECT * FROM MonitorRuns WHERE status = 'running' ORDER BY id DESC LIMIT 1;"
    )
    return cursor.fetchone()


def start_run(connection, trigger, backend):
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO MonitorRuns (trigger, backend, started_at, status)
        VALUES (?, ?, ?, 'running');
        """,
        (trigger, backend, utcnow()),
    )
    connection.commit()
    return cursor.lastrowid


def finish_run(connection, run_id, status, checked, new, refreshed, error=None):
    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE MonitorRuns
        SET finished_at = ?, status = ?, communities_checked = ?,
            posts_new = ?, posts_refreshed = ?, error = ?
        WHERE id = ?;
        """,
        (utcnow(), status, checked, new, refreshed, error, run_id),
    )
    connection.commit()


def record_item(connection, run_id, community_id, name, status,
                new=0, refreshed=0, error=None):
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO MonitorRunItems (
            run_id, community_id, community_name, status,
            posts_new, posts_refreshed, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (run_id, community_id, name, status, new, refreshed, error),
    )
    connection.commit()


def monitored_communities(connection):
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT id, name, display_name, monitor_sort, monitor_limit
        FROM Communities
        WHERE monitor_enabled = 1
        ORDER BY name;
        """
    )
    return cursor.fetchall()


def mark_checked(connection, community_id):
    cursor = connection.cursor()
    cursor.execute(
        'UPDATE Communities SET last_checked_at = ? WHERE id = ?;',
        (utcnow(), community_id),
    )
    connection.commit()


# -------------------------------------------------------------------- sweeps

def sweep_browser(connection, run_id, communities):
    """One Chromium window for the whole sweep, reused across communities."""
    from playwright.sync_api import sync_playwright

    from browse_ingest import SUBREDDIT_PAUSE, browse_subreddit, snooze
    from reddit_ingest import upsert_community, upsert_posts

    total_new = total_refreshed = checked = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={'width': 1440, 'height': 900},
            locale='en-US',
        )
        page = context.new_page()

        try:
            for index, community in enumerate(communities):
                name = community['name']

                if index:
                    snooze(SUBREDDIT_PAUSE)

                print(f"\nr/{name} ({community['monitor_sort']})")

                try:
                    meta, posts = browse_subreddit(
                        page, name,
                        community['monitor_sort'] or 'new',
                        community['monitor_limit'] or 50,
                        'week',
                    )
                except Exception as exc:
                    print(f"  r/{name}: {exc}")
                    record_item(connection, run_id, community['id'], name,
                                'error', error=str(exc)[:400])
                    continue

                if meta is None or not posts:
                    record_item(connection, run_id, community['id'], name,
                                'no_data', error='blocked, private, or empty listing')
                    continue

                community_row = {
                    'name': meta['name'],
                    'display_name': meta.get('display_name'),
                    'title': meta.get('title'),
                    'public_description': meta.get('public_description'),
                    'subscribers': meta.get('subscribers'),
                    'active_users': meta.get('active_users'),
                    'subreddit_type': None,
                    'over18': 0,
                    'created_utc': None,
                    'url': f"https://www.reddit.com/r/{meta['name']}/",
                }

                community_id = upsert_community(connection, community_row)
                new, refreshed = upsert_posts(connection, community_id, posts, run_id)

                mark_checked(connection, community_id)
                record_item(connection, run_id, community_id, name, 'ok', new, refreshed)

                total_new += new
                total_refreshed += refreshed
                checked += 1

                print(f"  {new} new, {refreshed} refreshed")
        finally:
            browser.close()

    return checked, total_new, total_refreshed


def sweep_api(connection, run_id, communities):
    """OAuth path. Slower to set up, but survives having no desktop."""
    from reddit_ingest import (RedditAuthError, build_client, fetch_community,
                               fetch_posts, upsert_community, upsert_posts)

    total_new = total_refreshed = checked = 0
    client = build_client()

    try:
        for community in communities:
            name = community['name']
            print(f"\nr/{name} ({community['monitor_sort']})")

            try:
                meta = fetch_community(client, name)

                if meta is None:
                    record_item(connection, run_id, community['id'], name,
                                'no_data', error='not readable')
                    continue

                posts = fetch_posts(
                    client, name,
                    community['monitor_sort'] or 'new',
                    community['monitor_limit'] or 50,
                    'week',
                )
            except RedditAuthError:
                raise
            except Exception as exc:
                record_item(connection, run_id, community['id'], name,
                            'error', error=str(exc)[:400])
                continue

            community_id = upsert_community(connection, meta)
            new, refreshed = upsert_posts(connection, community_id, posts, run_id)

            mark_checked(connection, community_id)
            record_item(connection, run_id, community_id, name, 'ok', new, refreshed)

            total_new += new
            total_refreshed += refreshed
            checked += 1

            print(f"  {new} new, {refreshed} refreshed")
    finally:
        client.close()

    return checked, total_new, total_refreshed


def run_sweep(trigger='manual', backend='browser'):
    """One full pass over the monitored communities. Returns the run id."""
    connection = get_connection()

    try:
        active = run_in_progress(connection)

        if active:
            print(f"Run {active['id']} is already in progress (started "
                  f"{active['started_at']}). Skipping.")
            return None

        communities = monitored_communities(connection)

        if not communities:
            print("No communities are being monitored. Add some at /monitor.")
            return None

        run_id = start_run(connection, trigger, backend)
        print(f"Run {run_id}: {len(communities)} communit"
              f"{'y' if len(communities) == 1 else 'ies'} via {backend}")

        try:
            sweep = sweep_browser if backend == 'browser' else sweep_api
            checked, new, refreshed = sweep(connection, run_id, communities)
        except Exception as exc:
            traceback.print_exc()
            finish_run(connection, run_id, 'failed', 0, 0, 0, str(exc)[:400])
            return run_id

        status = 'ok' if checked == len(communities) else 'partial'
        finish_run(connection, run_id, status, checked, new, refreshed)

        print(f"\nRun {run_id} {status}: {checked}/{len(communities)} checked, "
              f"{new} new post(s), {refreshed} refreshed")

        return run_id
    finally:
        connection.close()


# ------------------------------------------------------------------ schedule

def loop(interval_minutes, backend):
    """Hourly on the hour by default, via APScheduler's cron trigger."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BlockingScheduler()

    if interval_minutes == 60:
        trigger = CronTrigger(minute=0)
        description = 'every hour, on the hour'
    else:
        trigger = IntervalTrigger(minutes=interval_minutes)
        description = f'every {interval_minutes} minutes'

    scheduler.add_job(
        run_sweep,
        trigger=trigger,
        kwargs={'trigger': 'cron', 'backend': backend},
        # If a sweep overruns its slot, skip the missed one rather than
        # queueing a second browser behind it.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        id='reddit_sweep',
    )

    print(f"Scheduled: {description}, backend={backend}. Ctrl-C to stop.")

    if backend == 'browser':
        print("A Chromium window will open on each sweep - leave this session "
              "logged in.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nScheduler stopped.")


def main():
    parser = argparse.ArgumentParser(
        description='Sweep the monitored communities for new posts.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--once', action='store_true', help='Run one sweep and exit')
    parser.add_argument('--loop', action='store_true', help='Run on a schedule')
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL_MINUTES,
                        help='Minutes between sweeps in --loop (default 60)')
    parser.add_argument('--backend', default='browser', choices=('browser', 'api'))
    parser.add_argument('--trigger', default=None,
                        help=argparse.SUPPRESS)  # set by the dashboard's Run now
    args = parser.parse_args()

    if not args.once and not args.loop:
        parser.error('Pass --once or --loop')

    if not Path(DB_PATH).exists():
        parser.error(f'No database at {DB_PATH}. Run: python createDb.py')

    if args.loop:
        loop(args.interval, args.backend)
        return 0

    run_sweep(trigger=args.trigger or 'manual', backend=args.backend)
    return 0


if __name__ == '__main__':
    sys.exit(main())
