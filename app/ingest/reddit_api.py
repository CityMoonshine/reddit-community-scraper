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
from app.ingest.store import (SELFTEXT_CAP, epoch_to_iso, upsert_community,
                             upsert_posts)

TOKEN_URL = 'https://www.reddit.com/api/v1/access_token'
API_BASE = 'https://oauth.reddit.com'

# OAuth clients get 100 requests/minute averaged over 10 minutes. Pacing at one
# request per 1.2s keeps us at ~50/min - half the budget, no bursts to explain.
MIN_REQUEST_INTERVAL = 1.2

# Back off early rather than riding the limit to zero.
RATELIMIT_FLOOR = 5

# Reddit caps listing pages at 100 items regardless of what you ask for.
PAGE_SIZE = 100

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


def placeholder_thumb(value):
    """Reddit puts sentinels like 'self', 'default', 'nsfw' in `thumbnail`.

    They are not URLs, and storing them means the UI has to know the sentinel
    list to avoid rendering a broken image.
    """
    if not value or not str(value).startswith('http'):
        return None
    return value


def preview_url(data):
    """The largest preview Reddit generated, HTML-unescaped.

    Preview URLs arrive with &amp; in them even under raw_json=1 in some
    responses, and a URL with a literal &amp; 404s.
    """
    images = ((data.get('preview') or {}).get('images') or [])

    if not images:
        return None

    source = (images[0] or {}).get('source') or {}
    url = source.get('url')

    return url.replace('&amp;', '&') if url else None


def gallery_urls(data):
    """Ordered media URLs for a gallery post, or None if it isn't one."""
    items = (data.get('gallery_data') or {}).get('items') or []
    metadata = data.get('media_metadata') or {}

    urls = []

    for item in items:
        media = metadata.get(item.get('media_id')) or {}
        # 's' is the full-size variant; 'u' is its URL, 'gif' for animations.
        source = media.get('s') or {}
        url = source.get('u') or source.get('gif')

        if url:
            urls.append(url.replace('&amp;', '&'))

    return urls or None


def media_url(data):
    """The playable/embedded media behind a post, if Reddit hosts one."""
    media = data.get('secure_media') or data.get('media') or {}

    reddit_video = media.get('reddit_video') or {}
    if reddit_video.get('fallback_url'):
        return reddit_video['fallback_url']

    oembed = media.get('oembed') or {}
    return oembed.get('url') or oembed.get('thumbnail_url')


def crosspost_origin(data):
    """'r/<sub>' this was crossposted from, or None."""
    parents = data.get('crosspost_parent_list') or []

    if not parents:
        return None

    return (parents[0] or {}).get('subreddit_name_prefixed')


def parse_post(data):
    selftext = data.get('selftext') or ''

    # `edited` is False when untouched and an epoch float when edited - a union
    # type in the wire format, so it cannot go straight into a TEXT column.
    edited = data.get('edited')

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
        'selftext_chars': len(selftext) or None,
        'created_utc': epoch_to_iso(data.get('created_utc')),

        'thumbnail': placeholder_thumb(data.get('thumbnail')),
        'preview_image': preview_url(data),
        'media_url': media_url(data),
        'post_hint': data.get('post_hint'),
        'is_video': 1 if data.get('is_video') else 0,
        'is_gallery': 1 if data.get('is_gallery') else 0,
        'gallery_urls': gallery_urls(data),
        'total_awards': data.get('total_awards_received'),
        'gilded': data.get('gilded'),
        'edited_utc': (epoch_to_iso(edited)
                       if isinstance(edited, (int, float))
                       and not isinstance(edited, bool) else None),
        'locked': 1 if data.get('locked') else 0,
        'archived': 1 if data.get('archived') else 0,
        'spoiler': 1 if data.get('spoiler') else 0,
        'distinguished': data.get('distinguished'),
        'contest_mode': 1 if data.get('contest_mode') else 0,
        'is_original_content': 1 if data.get('is_original_content') else 0,
        'author_flair': data.get('author_flair_text'),
        'flair_bg': data.get('link_flair_background_color') or None,
        'flair_text_color': data.get('link_flair_text_color') or None,
        'num_crossposts': data.get('num_crossposts'),
        'crosspost_origin': crosspost_origin(data),
        'removed_by_category': data.get('removed_by_category'),
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
