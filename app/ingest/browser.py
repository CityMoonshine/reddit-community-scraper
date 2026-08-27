"""Scrape Reddit by driving a real browser, the way a person does.

Why a browser and not the JSON API:

    - www.reddit.com/r/x/hot.json answers 403 to plain HTTP clients.
    - old.reddit.com/r/x/.json redirects to a login wall.
    - Headless Chromium gets "You've been blocked by network security".

A headed Chromium window loads the same pages fine. On a VPS there is no
display, so the worker container runs it under Xvfb - see docker/worker.
Chromium is genuinely headed, it just draws to a virtual framebuffer.

Two things about the modern Reddit feed shape the code:

    1. Every field worth having is a DOM attribute on <shreddit-post> -
       id, post-title, author, score, comment-count, upvote-ratio,
       created-timestamp, domain, permalink. No text parsing needed.

    2. The feed is VIRTUALISED. Cards are recycled out of the DOM as you
       scroll past them - a live count goes 27 -> 49 -> 25. So posts are
       harvested after every scroll and accumulated in a dict keyed by post
       id. Reading the DOM once at the end would silently lose most of them.

Standalone use (outside the worker):

    python -m app.ingest.browser --subreddits python,programming --limit 60
"""

import argparse
import random
import sys
import time

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from app.config import PLAYWRIGHT_HEADLESS
from app.db import connection_scope
from app.ingest.store import upsert_community, upsert_posts

# Pause between scroll steps. Randomised because a scraper that scrolls on a
# perfect 2000ms metronome is the easiest thing in the world to spot - and this
# repo exists to think about exactly that.
SCROLL_PAUSE = (1.4, 2.6)

# Breather between subreddits, so a multi-sub sweep isn't one long hammer.
SUBREDDIT_PAUSE = (3.0, 5.0)

SCROLL_STEP_PX = 2200

# Give up on a listing after this many scrolls that surface nothing new.
# Reddit's lazy loader routinely stalls for 3-4 scrolls before the next batch
# lands, so a tight budget here truncates listings silently.
DRY_SCROLLS = 8

PREVIEW_CAP = 2000

SORT_PATHS = {
    'hot': '',
    'new': 'new/',
    'top': 'top/',
    'rising': 'rising/',
}

TIME_FILTERS = ('hour', 'day', 'week', 'month', 'year', 'all')

BLOCK_MARKERS = (
    'blocked by network security',
    'whoa there, pardner',
)

# One pass over the cards currently in the DOM. Runs in the page so a
# virtualised feed can't recycle a card out from under us mid-iteration.
HARVEST_JS = """
() => Array.from(document.querySelectorAll('shreddit-post')).map(el => {
    const attr = name => el.getAttribute(name);
    const flairEl = el.querySelector('shreddit-post-flair, a[href*="flair_name"], [class*="flair"]');
    const bodyEl = el.querySelector('[data-post-click-location="text-body"], a[slot="text-body"]');
    return {
        raw_id: attr('id'),
        title: attr('post-title'),
        author: attr('author'),
        permalink: attr('permalink'),
        content_href: attr('content-href'),
        domain: attr('domain'),
        score: attr('score'),
        comment_count: attr('comment-count'),
        upvote_ratio: attr('upvote-ratio'),
        created: attr('created-timestamp'),
        post_type: attr('post-type'),
        nsfw: el.hasAttribute('nsfw'),
        promoted: el.hasAttribute('promoted') || attr('view-context') === 'ADPost',
        flair: flairEl ? flairEl.textContent.trim() : null,
        preview: bodyEl ? bodyEl.textContent.trim() : '',
    };
})
"""

COMMUNITY_JS = """
() => {
    const h = document.querySelector('shreddit-subreddit-header');
    if (!h) return null;
    const attr = n => h.getAttribute(n);

    // Total subscribers is NOT rendered on this layout - the header shows
    // weekly actives and weekly contributions instead, and old.reddit (which
    // does show "N readers") answers with a login wall. So: only accept a
    // count explicitly labelled "members", and take null otherwise. Guessing
    // at faceplate-number here picks up post vote counts instead.
    let subscribers = null;
    const labelled = Array.from(document.querySelectorAll('faceplate-number'))
        .filter(e => !e.closest('shreddit-post'))
        .find(e => /member|subscriber/i.test((e.parentElement || {}).innerText || ''));

    if (labelled) {
        subscribers = parseInt(labelled.getAttribute('number'), 10) || null;
    }

    return {
        name: attr('name') || attr('display-name'),
        display_name: attr('prefixed-name'),
        title: attr('display-name'),
        public_description: attr('description'),
        subscribers: subscribers,
        active_users: parseInt(attr('weekly-active-users') || '0', 10) || null,
    };
}
"""


class BlockedError(RuntimeError):
    """Reddit served an interstitial instead of the feed."""


def snooze(bounds):
    time.sleep(random.uniform(*bounds))


def launch_browser(playwright):
    """Headed by default. Under Xvfb this still counts as headed."""
    browser = playwright.chromium.launch(headless=PLAYWRIGHT_HEADLESS)
    context = browser.new_context(
        viewport={'width': 1440, 'height': 900},
        locale='en-US',
    )
    return browser, context


def listing_url(subreddit, sort, time_filter):
    url = f'https://www.reddit.com/r/{subreddit}/{SORT_PATHS.get(sort, "")}'
    if sort == 'top':
        url += f'?t={time_filter}'
    return url


def parse_card(card):
    """DOM card -> a Posts row. Returns None for anything not worth storing."""
    raw_id = card.get('raw_id') or ''

    # Reddit's fullname form is t3_<base36>; the bare id is what we key on.
    if not raw_id.startswith('t3_'):
        return None

    if card.get('promoted') or not card.get('title'):
        return None

    def as_int(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    def as_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    permalink = card.get('permalink') or ''
    if permalink.startswith('/'):
        permalink = f'https://www.reddit.com{permalink}'

    return {
        'post_id': raw_id[3:],
        'title': card['title'],
        'author': card.get('author'),
        'permalink': permalink,
        'url': card.get('content_href'),
        'domain': card.get('domain'),
        'flair': (card.get('flair') or '').strip() or None,
        'score': as_int(card.get('score')),
        'upvote_ratio': as_float(card.get('upvote_ratio')),
        'num_comments': as_int(card.get('comment_count')),
        'over18': 1 if card.get('nsfw') else 0,
        'is_self': 1 if (card.get('post_type') or '').startswith('self') else 0,
        'stickied': 0,
        'selftext': (card.get('preview') or '')[:PREVIEW_CAP],
        'created_utc': card.get('created'),
    }


def classify_response(status_code, body):
    """'ok' | 'blocked' | 'missing' | 'error' for a listing page load.

    Split out because getting this wrong is expensive. A 403 reported as
    "no data" reads as "that subreddit is empty", when it actually means every
    future request will fail the same way. Blocked has to be distinguishable
    from missing, because only one of them is worth changing the backend over.
    """
    body = (body or '').lower()

    if any(marker in body for marker in BLOCK_MARKERS):
        return 'blocked'

    # 403 on a public listing is an access decision about the client, not about
    # the subreddit. 429 is the rate limiter saying the same thing politely.
    if status_code in (403, 429):
        return 'blocked'

    if status_code == 404:
        return 'missing'

    if 'this community is private' in body or 'been banned' in body:
        return 'missing'

    if status_code is not None and status_code >= 400:
        return 'error'

    return 'ok'


def block_message(status_code):
    detail = f'HTTP {status_code}' if status_code else 'a block page'

    return (
        f'Reddit refused the request ({detail}). On a VPS this is almost always '
        f'the datacenter IP being filtered rather than anything about the '
        f'browser, and it will not clear on its own. Switch to the sanctioned '
        f'API: set SCRAPE_BACKEND=api in .env, add Reddit app credentials, and '
        f'restart the worker.'
    )


def browse_subreddit(page, subreddit, sort='new', limit=50, time_filter='week',
                     on_progress=None):
    """Open the listing and scroll it, harvesting after every step.

    Returns (community_dict_or_None, [post rows]).
    Raises BlockedError if Reddit served an interstitial - the caller should
    treat that as fatal for the whole sweep, not just this subreddit.

    on_progress(text) is called as the scroll proceeds so the worker can put
    live progress on the dashboard. A sweep is slow enough that "it is doing
    something" needs to be observable, not inferred.
    """
    def report(text):
        print(f'  {text}', flush=True)
        if on_progress:
            on_progress(text)

    url = listing_url(subreddit, sort, time_filter)
    print(f'  opening {url}', flush=True)

    response = page.goto(url, wait_until='domcontentloaded', timeout=60000)

    status_code = response.status if response else None
    body = page.inner_text('body')[:400].lower() if page.locator('body').count() else ''

    verdict = classify_response(status_code, body)

    if verdict == 'blocked':
        raise BlockedError(block_message(status_code))

    if verdict == 'missing':
        report(f'r/{subreddit}: private, banned, or does not exist (HTTP {status_code})')
        return None, []

    if verdict == 'error':
        report(f'r/{subreddit}: HTTP {status_code}')
        return None, []

    try:
        page.wait_for_selector('shreddit-post', timeout=30000)
    except PlaywrightTimeout:
        report(f'r/{subreddit}: no posts rendered (does it exist?)')
        return None, []

    # Let the first screenful settle before reading anything.
    page.wait_for_timeout(1500)

    community = page.evaluate(COMMUNITY_JS)

    if community and not community.get('name'):
        community['name'] = subreddit

    posts = {}
    dry = 0

    while len(posts) < limit and dry < DRY_SCROLLS:
        before = len(posts)

        for card in page.evaluate(HARVEST_JS):
            row = parse_card(card)
            if row:
                posts[row['post_id']] = row

        gained = len(posts) - before
        dry = 0 if gained else dry + 1

        report(f'r/{subreddit}: {len(posts)}/{limit} posts harvested')

        if len(posts) >= limit:
            break

        # Wheel first (that's what a person does, and it fires the same scroll
        # handlers), then pin to the bottom so the sentinel the lazy loader
        # watches actually enters the viewport.
        page.mouse.wheel(0, SCROLL_STEP_PX)
        page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
        snooze(SCROLL_PAUSE)

    rows = list(posts.values())[:limit]
    report(f'r/{subreddit}: {len(rows)} posts collected')

    return community, rows


def community_row(meta):
    """browse_subreddit metadata -> the dict upsert_community wants."""
    return {
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


def main():
    parser = argparse.ArgumentParser(
        description='Browse Reddit in a real browser and ingest what a user would see.'
    )
    parser.add_argument('--subreddits', required=True)
    parser.add_argument('--sort', default='hot', choices=sorted(SORT_PATHS))
    parser.add_argument('--time-filter', default='week', choices=TIME_FILTERS)
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    subreddits = [s.strip().lstrip('r/').strip('/') for s in args.subreddits.split(',')]
    subreddits = [s for s in subreddits if s]

    if not subreddits:
        parser.error('No usable subreddit names in --subreddits')

    with sync_playwright() as playwright:
        browser, context = launch_browser(playwright)
        page = context.new_page()

        try:
            for index, subreddit in enumerate(subreddits):
                print(f'\nr/{subreddit} ({args.sort})', flush=True)

                if index:
                    snooze(SUBREDDIT_PAUSE)

                try:
                    meta, posts = browse_subreddit(
                        page, subreddit, args.sort, args.limit, args.time_filter
                    )
                except BlockedError as exc:
                    print(f'\n{exc}', file=sys.stderr)
                    return 1

                if meta is None or not posts:
                    continue

                if args.dry_run:
                    print(f'  [dry run] {len(posts)} posts, nothing written', flush=True)
                    continue

                with connection_scope() as connection:
                    community_id = upsert_community(connection, community_row(meta))
                    new, refreshed = upsert_posts(connection, community_id, posts)

                print(f'  {new} new, {refreshed} refreshed', flush=True)
        finally:
            browser.close()

    return 0


if __name__ == '__main__':
    sys.exit(main())
