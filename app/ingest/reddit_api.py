"""Reddit's OAuth API - the alternative scrape backend.

Needs credentials, but runs truly headless, which makes it the fallback worth
having when a VPS IP gets the browser path blocked.

    1. https://www.reddit.com/prefs/apps -> "create another app..."
    2. Pick type "script", redirect uri http://localhost:8000 (unused).
    3. Copy the id under the app name (client id) and the secret into .env.

Standalone use:

    python -m app.ingest.reddit_api --subreddits python --sort top --limit 200
"""

import argparse
import sys
import time

import httpx

from app.config import (REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_PASSWORD,
                        REDDIT_USER_AGENT, REDDIT_USERNAME)
from app.db import connection_scope
from app.ingest.store import epoch_to_iso, upsert_community, upsert_posts

TOKEN_URL = 'https://www.reddit.com/api/v1/access_token'
API_BASE = 'https://oauth.reddit.com'

# OAuth clients get 100 requests/minute averaged over 10 minutes. Pacing at one
# request per 1.2s keeps us at ~50/min - half the budget, no bursts to explain.
MIN_REQUEST_INTERVAL = 1.2

# Back off early rather than riding the limit to zero.
RATELIMIT_FLOOR = 5

# Reddit caps listing pages at 100 items regardless of what you ask for.
PAGE_SIZE = 100

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
                'Missing credentials. Set REDDIT_CLIENT_ID and '
                'REDDIT_CLIENT_SECRET in .env - see .env.example.'
            )

        self.client_id = client_id
        self.client_secret = client_secret
        self.username = username
        self.password = password

        self._token = None
        self._token_expires_at = 0.0
        self._last_request_at = 0.0

        self._http = httpx.Client(timeout=30.0, headers={'User-Agent': user_agent})

    def _authenticate(self):
        if self.username and self.password:
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
            TOKEN_URL, auth=(self.client_id, self.client_secret), data=data
        )

        if response.status_code == 401:
            raise RedditAuthError(
                'Reddit rejected the credentials (401). Check that '
                'REDDIT_CLIENT_ID is the short string under the app name - not '
                "the app name itself - and that the app type is 'script'."
            )

        response.raise_for_status()
        payload = response.json()

        if 'access_token' not in payload:
            raise RedditAuthError(f'No access_token in token response: {payload}')

        self._token = payload['access_token']
        # Renew a minute early so a long sweep never trips over the boundary.
        self._token_expires_at = time.monotonic() + payload.get('expires_in', 3600) - 60

        print(f'  authenticated ({mode})', flush=True)

    def _ensure_token(self):
        if self._token is None or time.monotonic() >= self._token_expires_at:
            self._authenticate()

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
            print(f'  rate limit nearly spent, sleeping {reset:.0f}s', flush=True)
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

        if response.status_code == 429 and attempt <= 3:
            wait = float(response.headers.get('retry-after', 60))
            print(f'  429 from Reddit, backing off {wait:.0f}s', flush=True)
            time.sleep(wait + 1)
            return self.get(path, params, attempt + 1)

        if response.status_code >= 500 and attempt <= 3:
            wait = 2 ** attempt
            time.sleep(wait)
            return self.get(path, params, attempt + 1)

        self._respect_ratelimit(response)
        return response

    def close(self):
        self._http.close()


def build_client():
    return RedditClient(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
    )


def fetch_community(client, subreddit):
    """/r/{sub}/about -> the Communities row, or None if we can't read it."""
    response = client.get(f'/r/{subreddit}/about', params={'raw_json': 1})

    if response.status_code == 404:
        print(f'  r/{subreddit}: not found (or banned)', flush=True)
        return None

    if response.status_code == 403:
        print(f'  r/{subreddit}: private / quarantined', flush=True)
        return None

    if response.status_code != 200:
        print(f'  r/{subreddit}: unexpected {response.status_code}', flush=True)
        return None

    data = response.json().get('data', {})

    if data.get('subreddit_type') == 'user':
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


def fetch_posts(client, subreddit, sort='new', limit=50, time_filter='week'):
    """Walk the listing with `after` cursors until we hit `limit` or run dry."""
    collected = []
    after = None

    while len(collected) < limit:
        params = {'limit': min(PAGE_SIZE, limit - len(collected)), 'raw_json': 1}

        if after:
            params['after'] = after

        if sort in ('top', 'controversial'):
            params['t'] = time_filter

        response = client.get(f'/r/{subreddit}/{sort}', params=params)

        if response.status_code != 200:
            print(f'  r/{subreddit}/{sort}: {response.status_code}', flush=True)
            break

        listing = response.json().get('data', {})
        children = listing.get('children', [])

        if not children:
            break

        for child in children:
            if child.get('kind') == 't3':
                collected.append(parse_post(child['data']))

        after = listing.get('after')

        # End of the listing. Reddit caps most sorts around 1000 items.
        if not after:
            break

    return collected[:limit]


def parse_post(data):
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
        'selftext': (data.get('selftext') or '')[:SELFTEXT_CAP],
        'created_utc': epoch_to_iso(data.get('created_utc')),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Ingest Reddit communities via the OAuth API."
    )
    parser.add_argument('--subreddits', required=True)
    parser.add_argument('--sort', default='hot', choices=SORTS)
    parser.add_argument('--time-filter', default='week', choices=TIME_FILTERS)
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()

    subreddits = [s.strip().lstrip('r/').strip('/') for s in args.subreddits.split(',')]
    subreddits = [s for s in subreddits if s]

    try:
        client = build_client()
    except RedditAuthError as exc:
        print(f'\n{exc}\n', file=sys.stderr)
        return 1

    try:
        for subreddit in subreddits:
            print(f'\nr/{subreddit} ({args.sort})', flush=True)

            meta = fetch_community(client, subreddit)

            if meta is None:
                continue

            posts = fetch_posts(client, subreddit, args.sort, args.limit, args.time_filter)

            with connection_scope() as connection:
                community_id = upsert_community(connection, meta)
                new, refreshed = upsert_posts(connection, community_id, posts)

            print(f'  {new} new, {refreshed} refreshed', flush=True)
    finally:
        client.close()

    return 0


if __name__ == '__main__':
    sys.exit(main())
