"""Pull real subreddit and post data from Reddit into the portal database.

Reddit's anonymous JSON endpoints (www.reddit.com/r/x/hot.json) are gated now -
they answer 403, and old.reddit.com bounces to a login wall. The supported way
in is the OAuth API, so that is what this uses.

Setup, once:

    1. https://www.reddit.com/prefs/apps -> "create another app..."
    2. Pick type "script", redirect uri http://localhost:8000 (unused).
    3. Copy the id under the app name (client id) and the secret.
    4. Put them in backend/.env  (see .env.example)

Then:

    python reddit_ingest.py --subreddits python,programming,dataisbeautiful
    python reddit_ingest.py --subreddits python --sort top --time-filter year --limit 300

Re-running is an upsert: posts keep their row and refresh score / comment
count, so you can ingest the same subreddit repeatedly and watch the numbers
move rather than accumulating duplicates.
"""

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / 'portal.db')

load_dotenv(BASE_DIR / '.env')

TOKEN_URL = 'https://www.reddit.com/api/v1/access_token'
API_BASE = 'https://oauth.reddit.com'

# Reddit asks for a descriptive UA in the platform:appid:version (by /u/user)
# form and rate-limits generic ones harder. Override via REDDIT_USER_AGENT.
DEFAULT_USER_AGENT = 'python:scraping-defense-lab:v0.1 (by /u/unknown)'

# OAuth clients get 100 requests/minute averaged over 10 minutes. Pacing at one
# request per 1.2s keeps us at ~50/min - half the budget, no bursts to explain.
MIN_REQUEST_INTERVAL = 1.2

# Back off early rather than riding the limit to zero.
RATELIMIT_FLOOR = 5

# Reddit caps listing pages at 100 items regardless of what you ask for.
PAGE_SIZE = 100

# Self-post bodies can be enormous; the portal only ever renders a preview.
SELFTEXT_CAP = 2000

SORTS = ('hot', 'new', 'top', 'rising', 'controversial')
TIME_FILTERS = ('hour', 'day', 'week', 'month', 'year', 'all')


class RedditAuthError(RuntimeError):
    pass


class RedditClient:
    """Minimal OAuth read client. Paces itself and respects Retry-After."""

    def __init__(self, client_id, client_secret, user_agent,
                 username=None, password=None):
        if not client_id or not client_secret:
            raise RedditAuthError(
                "Missing credentials. Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET "
                "in backend/.env - see .env.example, or run with --help for setup steps."
            )

        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent
        self.username = username
        self.password = password

        self._token = None
        self._token_expires_at = 0.0
        self._last_request_at = 0.0

        self._http = httpx.Client(timeout=30.0, headers={'User-Agent': user_agent})

    # -------------------------------------------------------------- auth

    def _authenticate(self):
        if self.username and self.password:
            # Script apps can act as their own account, which gets a slightly
            # roomier rate limit and can read quarantined subs the user opted into.
            data = {
                'grant_type': 'password',
                'username': self.username,
                'password': self.password,
            }
            mode = f'password ({self.username})'
        else:
            data = {'grant_type': 'client_credentials'}
            mode = 'app-only'

        response = self._http.post(
            TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            data=data,
        )

        if response.status_code == 401:
            raise RedditAuthError(
                "Reddit rejected the credentials (401). Check that REDDIT_CLIENT_ID is "
                "the short string under the app name - not the app name itself - and "
                "that the app type is 'script'."
            )

        response.raise_for_status()
        payload = response.json()

        if 'access_token' not in payload:
            raise RedditAuthError(f"No access_token in token response: {payload}")

        self._token = payload['access_token']
        # Renew a minute early so a long ingest never trips over the boundary.
        self._token_expires_at = time.monotonic() + payload.get('expires_in', 3600) - 60

        print(f"  authenticated ({mode}), token valid ~{payload.get('expires_in', 3600)}s")

    def _ensure_token(self):
        if self._token is None or time.monotonic() >= self._token_expires_at:
            self._authenticate()

    # ------------------------------------------------------------ requests

    def _pace(self):
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_at = time.monotonic()

    def _respect_ratelimit(self, response):
        """Reddit reports the remaining budget on every response. Use it."""
        remaining = response.headers.get('x-ratelimit-remaining')
        reset = response.headers.get('x-ratelimit-reset')

        if remaining is None or reset is None:
            return

        try:
            remaining = float(remaining)
            reset = float(reset)
        except ValueError:
            return

        if remaining <= RATELIMIT_FLOOR and reset > 0:
            print(f"  rate limit nearly spent ({remaining:.0f} left), sleeping {reset:.0f}s")
            time.sleep(reset + 1)

    def get(self, path, params=None, attempt=1):
        self._ensure_token()
        self._pace()

        response = self._http.get(
            f'{API_BASE}{path}',
            params=params,
            headers={'Authorization': f'bearer {self._token}'},
        )

        if response.status_code == 401 and attempt == 1:
            # Token died early (revoked, or clock drift). One clean retry.
            self._token = None
            return self.get(path, params, attempt + 1)

        if response.status_code == 429:
            wait = float(response.headers.get('retry-after', 60))
            print(f"  429 from Reddit, backing off {wait:.0f}s")
            time.sleep(wait + 1)
            if attempt <= 3:
                return self.get(path, params, attempt + 1)

        if response.status_code >= 500 and attempt <= 3:
            wait = 2 ** attempt
            print(f"  {response.status_code} from Reddit, retrying in {wait}s")
            time.sleep(wait)
            return self.get(path, params, attempt + 1)

        self._respect_ratelimit(response)
        return response

    def close(self):
        self._http.close()


# ------------------------------------------------------------------ fetching

def fetch_community(client, subreddit):
    """/r/{sub}/about -> the Communities row, or None if we can't read it."""
    response = client.get(f'/r/{subreddit}/about', params={'raw_json': 1})

    if response.status_code == 404:
        print(f"  r/{subreddit}: not found (or banned)")
        return None

    if response.status_code == 403:
        print(f"  r/{subreddit}: private / quarantined - no read access")
        return None

    if response.status_code != 200:
        print(f"  r/{subreddit}: unexpected {response.status_code}")
        return None

    data = response.json().get('data', {})

    # User profiles ('u_someone') come back from /about too but have no posts
    # listing worth ingesting here.
    if data.get('subreddit_type') == 'user':
        print(f"  r/{subreddit}: user profile, skipping")
        return None

    return {
        'name': data.get('display_name', subreddit),
        'display_name': data.get('display_name_prefixed'),
        'title': data.get('title'),
        'public_description': data.get('public_description'),
        'subscribers': data.get('subscribers'),
        'active_users': data.get('accounts_active'),
        'subreddit_type': data.get('subreddit_type'),
        'over18': 1 if data.get('over18') else 0,
        'created_utc': epoch_to_iso(data.get('created_utc')),
        'url': f"https://www.reddit.com{data.get('url', f'/r/{subreddit}/')}",
    }


def fetch_posts(client, subreddit, sort, limit, time_filter):
    """Walk the listing with `after` cursors until we hit `limit` or run dry."""
    collected = []
    after = None

    while len(collected) < limit:
        params = {
            'limit': min(PAGE_SIZE, limit - len(collected)),
            'raw_json': 1,
        }

        if after:
            params['after'] = after

        if sort in ('top', 'controversial'):
            params['t'] = time_filter

        response = client.get(f'/r/{subreddit}/{sort}', params=params)

        if response.status_code != 200:
            print(f"  r/{subreddit}/{sort}: {response.status_code}, stopping this listing")
            break

        listing = response.json().get('data', {})
        children = listing.get('children', [])

        if not children:
            break

        for child in children:
            if child.get('kind') != 't3':
                continue
            collected.append(parse_post(child['data']))

        after = listing.get('after')

        if not after:
            # End of the listing. Reddit caps most sorts around 1000 items.
            break

        print(f"  r/{subreddit}: {len(collected)} posts so far...")

    return collected[:limit]


def parse_post(data):
    selftext = data.get('selftext') or ''

    return {
        'post_id': data.get('id'),
        'title': data.get('title', ''),
        'author': data.get('author'),
        'permalink': f"https://www.reddit.com{data.get('permalink', '')}",
        'url': data.get('url'),
        'domain': data.get('domain'),
        'flair': data.get('link_flair_text'),
        'score': data.get('score'),
        'upvote_ratio': data.get('upvote_ratio'),
        'num_comments': data.get('num_comments'),
        'over18': 1 if data.get('over_18') else 0,
        'is_self': 1 if data.get('is_self') else 0,
        'stickied': 1 if data.get('stickied') else 0,
        'selftext': selftext[:SELFTEXT_CAP],
        'created_utc': epoch_to_iso(data.get('created_utc')),
    }


def epoch_to_iso(value):
    if not value:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


# ------------------------------------------------------------------ storage

def upsert_community(connection, community):
    cursor = connection.cursor()

    cursor.execute(
        '''
        INSERT INTO Communities (
            name, display_name, title, public_description, subscribers,
            active_users, subreddit_type, over18, created_utc, url, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(name) DO UPDATE SET
            display_name = excluded.display_name,
            title = excluded.title,
            public_description = excluded.public_description,
            subscribers = COALESCE(excluded.subscribers, Communities.subscribers),
            active_users = COALESCE(excluded.active_users, Communities.active_users),
            subreddit_type = COALESCE(excluded.subreddit_type, Communities.subreddit_type),
            created_utc = COALESCE(excluded.created_utc, Communities.created_utc),
            over18 = excluded.over18,
            url = excluded.url,
            fetched_at = CURRENT_TIMESTAMP;
        ''',
        (
            community['name'], community['display_name'], community['title'],
            community['public_description'], community['subscribers'],
            community['active_users'], community['subreddit_type'],
            community['over18'], community['created_utc'], community['url'],
        ),
    )

    cursor.execute('SELECT id FROM Communities WHERE name = ?;', (community['name'],))
    return cursor.fetchone()[0]


def upsert_posts(connection, community_id, posts, run_id=None):
    """Returns (inserted, updated). Score and comments refresh; ids don't move.

    first_seen_at / first_seen_run_id are set on the INSERT branch only, so a
    post keeps the timestamp of the sweep that discovered it no matter how many
    later sweeps refresh its score.
    """
    cursor = connection.cursor()

    post_ids = [p['post_id'] for p in posts if p['post_id']]
    existing = set()

    # sqlite's parameter limit is high but not infinite - chunk the lookup.
    for start in range(0, len(post_ids), 500):
        chunk = post_ids[start:start + 500]
        placeholders = ','.join('?' * len(chunk))
        cursor.execute(
            f'SELECT post_id FROM Posts WHERE post_id IN ({placeholders});', chunk
        )
        existing.update(row[0] for row in cursor.fetchall())

    cursor.executemany(
        '''
        INSERT INTO Posts (
            post_id, community_id, title, author, permalink, url, domain, flair,
            score, upvote_ratio, num_comments, over18, is_self, stickied,
            selftext, created_utc, fetched_at, first_seen_at, first_seen_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)
        ON CONFLICT(post_id) DO UPDATE SET
            title = excluded.title,
            flair = excluded.flair,
            score = excluded.score,
            upvote_ratio = excluded.upvote_ratio,
            num_comments = excluded.num_comments,
            stickied = excluded.stickied,
            fetched_at = CURRENT_TIMESTAMP;
        ''',
        [
            (
                p['post_id'], community_id, p['title'], p['author'], p['permalink'],
                p['url'], p['domain'], p['flair'], p['score'], p['upvote_ratio'],
                p['num_comments'], p['over18'], p['is_self'], p['stickied'],
                p['selftext'], p['created_utc'], run_id,
            )
            for p in posts if p['post_id']
        ],
    )

    connection.commit()

    updated = len(existing)
    return len(post_ids) - updated, updated


# ---------------------------------------------------------------------- cli

def build_client():
    return RedditClient(
        client_id=os.getenv('REDDIT_CLIENT_ID'),
        client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
        user_agent=os.getenv('REDDIT_USER_AGENT', DEFAULT_USER_AGENT),
        username=os.getenv('REDDIT_USERNAME'),
        password=os.getenv('REDDIT_PASSWORD'),
    )


def main():
    parser = argparse.ArgumentParser(
        description='Ingest real Reddit communities and posts into the portal DB.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--subreddits', required=True,
        help='Comma-separated subreddit names, e.g. python,programming',
    )
    parser.add_argument('--sort', default='hot', choices=SORTS)
    parser.add_argument(
        '--time-filter', default='week', choices=TIME_FILTERS,
        help="Only used by --sort top / controversial (default: week)",
    )
    parser.add_argument(
        '--limit', type=int, default=100,
        help='Posts per subreddit (default 100; Reddit caps listings near 1000)',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Fetch and report, write nothing to the database',
    )
    args = parser.parse_args()

    subreddits = [s.strip().lstrip('r/').strip('/') for s in args.subreddits.split(',')]
    subreddits = [s for s in subreddits if s]

    if not subreddits:
        parser.error('No usable subreddit names in --subreddits')

    if not Path(DB_PATH).exists() and not args.dry_run:
        parser.error(f'No database at {DB_PATH}. Run: python createDb.py')

    try:
        client = build_client()
    except RedditAuthError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    connection = None if args.dry_run else sqlite3.connect(DB_PATH)
    totals = []

    try:
        for subreddit in subreddits:
            print(f"\nr/{subreddit} ({args.sort})")

            try:
                community = fetch_community(client, subreddit)
            except RedditAuthError as exc:
                print(f"\n{exc}\n", file=sys.stderr)
                return 1

            if community is None:
                continue

            posts = fetch_posts(client, subreddit, args.sort, args.limit, args.time_filter)

            if args.dry_run:
                print(f"  [dry run] {community['subscribers'] or 0:,} subscribers, "
                      f"{len(posts)} posts fetched, nothing written")
                totals.append((subreddit, len(posts), 0))
                continue

            community_id = upsert_community(connection, community)
            inserted, updated = upsert_posts(connection, community_id, posts)

            print(f"  {community['subscribers'] or 0:,} subscribers | "
                  f"{inserted} new post(s), {updated} refreshed")
            totals.append((subreddit, inserted, updated))
    finally:
        client.close()
        if connection is not None:
            connection.close()

    print("\n" + "-" * 46)
    for subreddit, inserted, updated in totals:
        print(f"  r/{subreddit:<26} +{inserted:<5} ~{updated}")

    if not args.dry_run and totals:
        # New communities need to land on somebody's watchlist or they stay
        # invisible behind the login.
        from seeder import seed_watchlists
        seed_watchlists()
        print("\nDone. Start the portal and sign in to see them at /records.")

    return 0


if __name__ == '__main__':
    sys.exit(main())
