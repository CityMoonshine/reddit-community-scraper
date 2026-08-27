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
import re
import sys
import time

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from app.config import (PLAYWRIGHT_HEADLESS, WEBSHARE_MAX_ATTEMPTS,
                        WEBSHARE_ROTATE)
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

# Chromium will not accept a per-context proxy unless one was declared at
# launch. This is playwright's documented placeholder for "there will be a
# proxy, just not this one" - see launch_browser.
PER_CONTEXT_PROXY = {'server': 'per-context'}

# One pass over the cards currently in the DOM. Runs in the page so a
# virtualised feed can't recycle a card out from under us mid-iteration.
HARVEST_JS = """
() => Array.from(document.querySelectorAll('shreddit-post')).map(el => {
    const attr = name => el.getAttribute(name);
    const flairEl = el.querySelector('shreddit-post-flair, a[href*="flair_name"], [class*="flair"]');
    const bodyEl = el.querySelector('[data-post-click-location="text-body"], a[slot="text-body"]');

    // Reddit lazy-loads thumbnails, so a card that has not been near the
    // viewport carries a data: placeholder. Only take a real remote URL.
    const imgEl = el.querySelector('img[src^="https"]');

    // The flair chip's colour is applied inline rather than via a class, so
    // the computed style is the only place it exists.
    let flairBg = null, flairColor = null;
    if (flairEl) {
        try {
            const style = getComputedStyle(flairEl);
            flairBg = style.backgroundColor || null;
            flairColor = style.color || null;
        } catch (e) { /* detached node mid-recycle */ }
    }

    // Awards and 'edited' render as text/icons rather than attributes, so
    // these are presence checks, not values.
    const awardEl = el.querySelector('award-button, shreddit-award, [class*="award"]');
    const editedEl = el.querySelector('[class*="edited"], time[datetime][title*="dit"]');

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

        thumbnail: imgEl ? imgEl.getAttribute('src') : null,
        flair_bg: flairBg,
        flair_text_color: flairColor,
        author_flair: (el.querySelector('[class*="author-flair"]') || {}).textContent || null,
        spoiler: el.hasAttribute('spoiler'),
        locked: el.hasAttribute('locked'),
        pinned: el.hasAttribute('pinned') || el.hasAttribute('stickied'),
        distinguished: attr('distinguished'),
        has_award: !!awardEl,
        edited: !!editedEl,
        comment_href: attr('comment-count-href'),
        subreddit: attr('subreddit-prefixed-name'),
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


def launch_browser(playwright, proxy=None):
    """Headed by default. Under Xvfb this still counts as headed.

    `proxy` is a playwright proxy dict, or the PER_CONTEXT_PROXY sentinel when
    the caller intends to set a different proxy per context. Chromium needs a
    proxy declared at launch before per-context proxies work at all, and the
    sentinel is how you declare one without committing to an address.
    """
    launch_kwargs = {'headless': PLAYWRIGHT_HEADLESS}

    if proxy is not None:
        launch_kwargs['proxy'] = proxy

    browser = playwright.chromium.launch(**launch_kwargs)

    # A launch-level proxy is inherited, so only pass it again when it is a
    # real address - handing the sentinel to new_context would try to resolve
    # 'per-context' as a hostname.
    context = new_context(browser, proxy if proxy is not PER_CONTEXT_PROXY else None)

    return browser, context


def new_context(browser, proxy=None):
    """A fresh context, optionally on its own exit IP.

    Rotating the context alongside the proxy is not incidental: a context
    carries the cookie jar and storage, so reusing one across exit IPs means
    the same Reddit session identifier arriving from two different countries -
    which is a louder signal than either half on its own.
    """
    kwargs = {
        'viewport': {'width': 1440, 'height': 900},
        'locale': 'en-US',
    }

    if proxy is not None:
        kwargs['proxy'] = proxy

    return browser.new_context(**kwargs)


class BrowserSession:
    """The browser, its current context/page, and the exit IP behind them.

    This exists so a sweep loop can say "give me a page" without caring whether
    there is a proxy pool underneath. With no pool it is exactly what the code
    did before Webshare existed: one browser, one context, one page, reused for
    the whole run.
    """

    def __init__(self, playwright, proxy_pool=None):
        self.pool = proxy_pool
        self.endpoint = None
        self.page = None

        self.browser, self.context = launch_browser(
            playwright, PER_CONTEXT_PROXY if proxy_pool else None
        )

        if proxy_pool:
            # The context launch_browser built has no exit assigned yet; the
            # first rotate replaces it, so only one is ever live at a time.
            self.rotate()
        else:
            self.page = self.context.new_page()

    @property
    def label(self):
        return self.endpoint.label if self.endpoint else None

    def rotate(self):
        """Fresh exit IP, fresh context, fresh page. No-op without a pool."""
        if not self.pool:
            return

        self.endpoint = self.pool.acquire()
        self._replace_context(self.endpoint.playwright_proxy())

        print(f'[webshare] exiting via {self.endpoint.label}', flush=True)

    def retire(self, reason='block page'):
        """Burn the current exit and move to another one."""
        if not self.pool:
            return

        self.pool.penalize(self.endpoint, reason)
        self.rotate()

    def next_community(self):
        """Called between communities; rotates only if configured to.

        Per-community rotation is the default because a single exit walking
        through twenty subreddits back to back is the pattern that got the
        original IP filtered in the first place.
        """
        if self.pool and WEBSHARE_ROTATE == 'community':
            self.rotate()

    def _replace_context(self, proxy):
        if self.context is not None:
            self.context.close()

        self.context = new_context(self.browser, proxy)
        self.page = self.context.new_page()

    def close(self):
        try:
            if self.context is not None:
                self.context.close()
        finally:
            self.browser.close()


def listing_url(subreddit, sort, time_filter):
    url = f'https://www.reddit.com/r/{subreddit}/{SORT_PATHS.get(sort, "")}'
    if sort == 'top':
        url += f'?t={time_filter}'
    return url


def normalise_colour(value):
    """getComputedStyle gives 'rgb(a, b, c)'; store '#rrggbb' like the API does.

    Without this the same flair arrives in two different notations depending on
    which backend ran, and the UI would need to understand both.
    """
    if not value:
        return None

    text = str(value).strip()

    if text.startswith('#'):
        return text

    # Fully transparent means "no colour set", not "black".
    numbers = re.findall(r'[\d.]+', text)

    if len(numbers) >= 4 and float(numbers[3]) == 0:
        return None

    if len(numbers) < 3:
        return None

    try:
        r, g, b = (int(float(n)) for n in numbers[:3])
    except ValueError:
        return None

    return f'#{r:02x}{g:02x}{b:02x}'


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

    post_type = (card.get('post_type') or '').lower()
    domain = card.get('domain') or ''

    # The feed calls a text post 'text', not 'self', so post_type alone misses
    # most of them. Reddit's own domain for a self post is 'self.<subreddit>',
    # which is the reliable tell - and getting this wrong means the AI scorer
    # is told a text post is a link post with its own permalink as the link.
    is_self = post_type.startswith('self') or domain.startswith('self.')

    return {
        'post_id': raw_id[3:],
        'title': card['title'],
        'author': card.get('author'),
        'permalink': permalink,
        'url': card.get('content_href'),
        'domain': domain or None,
        'flair': (card.get('flair') or '').strip() or None,
        'score': as_int(card.get('score')),
        'upvote_ratio': as_float(card.get('upvote_ratio')),
        'num_comments': as_int(card.get('comment_count')),
        'over18': 1 if card.get('nsfw') else 0,
        'is_self': 1 if is_self else 0,
        'stickied': 1 if card.get('pinned') else 0,
        'selftext': (card.get('preview') or '')[:PREVIEW_CAP],
        # Deliberately null rather than len(preview). The DOM gives a truncated
        # teaser, not the body, so reporting its length would assert a post is
        # short when we simply cannot see the rest of it.
        'selftext_chars': None,
        'created_utc': card.get('created'),

        'thumbnail': card.get('thumbnail'),
        'post_hint': post_type or None,
        'is_video': 1 if post_type == 'video' else 0,
        'is_gallery': 1 if post_type == 'gallery' else 0,
        'spoiler': 1 if card.get('spoiler') else 0,
        'locked': 1 if card.get('locked') else 0,
        'distinguished': card.get('distinguished'),
        'author_flair': (card.get('author_flair') or '').strip() or None,
        'flair_bg': normalise_colour(card.get('flair_bg')),
        'flair_text_color': normalise_colour(card.get('flair_text_color')),
        'edited_utc': None,
        # The feed shows that a post has awards, never how many. 1 here means
        # "at least one", and is left null rather than 0 when we cannot tell.
        'total_awards': 1 if card.get('has_award') else None,
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


def block_message(status_code, exit_label=None):
    detail = f'HTTP {status_code}' if status_code else 'a block page'

    if exit_label:
        # Different advice, because the diagnosis is different: one blocked
        # exit says nothing about the next one, so "switch backends" would be
        # premature. Only a pool that is blocked all the way through means
        # what an unproxied block means.
        return (
            f'Reddit refused the request ({detail}) through {exit_label}. That '
            f'exit IP is burned - it goes on cooldown and the next attempt '
            f'comes from another. If every exit in the pool comes back the '
            f'same way, the pool itself is filtered: widen the plan, change '
            f'WEBSHARE_COUNTRIES, or fall back to SCRAPE_BACKEND=api.'
        )

    return (
        f'Reddit refused the request ({detail}). On a VPS this is almost always '
        f'the datacenter IP being filtered rather than anything about the '
        f'browser, and it will not clear on its own. Switch to the sanctioned '
        f'API: set SCRAPE_BACKEND=api in .env, add Reddit app credentials, and '
        f'restart the worker.'
    )


def browse_subreddit(page, subreddit, sort='new', limit=50, time_filter='week',
                     on_progress=None, exit_label=None):
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
    via = f' via {exit_label}' if exit_label else ''
    print(f'  opening {url}{via}', flush=True)

    response = page.goto(url, wait_until='domcontentloaded', timeout=60000)

    status_code = response.status if response else None
    body = page.inner_text('body')[:400].lower() if page.locator('body').count() else ''

    verdict = classify_response(status_code, body)

    if verdict == 'blocked':
        raise BlockedError(block_message(status_code, exit_label))

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


def browse_with_retry(session, subreddit, sort='new', limit=50,
                      time_filter='week', on_progress=None, max_attempts=None):
    """browse_subreddit, except a block page rotates the exit and tries again.

    Without a pool this is a single attempt and BlockedError propagates
    untouched - a block on the machine's only IP is not retryable, and
    retrying it would only make the same failure slower.

    A pool that runs out mid-retry is reported as a BlockedError too. It is the
    same outcome from the caller's point of view (every route to Reddit is
    refused) and it keeps 'blocked aborts the sweep' as one rule rather than
    two.
    """
    from app.ingest.webshare import NoProxyAvailable

    attempts = (max_attempts or WEBSHARE_MAX_ATTEMPTS) if session.pool else 1

    for attempt in range(1, attempts + 1):
        try:
            return browse_subreddit(
                session.page, subreddit, sort, limit, time_filter,
                on_progress=on_progress, exit_label=session.label,
            )
        except BlockedError as exc:
            print(f'  {exc}', flush=True)

            if attempt == attempts:
                raise

            if on_progress:
                on_progress(f'r/{subreddit}: blocked, rotating exit IP '
                            f'(attempt {attempt + 1} of {attempts})')

            try:
                session.retire(f'blocked on r/{subreddit}')
            except NoProxyAvailable as exhausted:
                raise BlockedError(str(exhausted)) from exhausted


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
    parser.add_argument('--webshare', dest='webshare', action='store_true',
                        default=None,
                        help='Force routing through Webshare exits, whatever '
                             'WEBSHARE_ENABLED says.')
    parser.add_argument('--no-webshare', dest='webshare', action='store_false',
                        help='Force a direct connection instead.')
    args = parser.parse_args()

    subreddits = [s.strip().lstrip('r/').strip('/') for s in args.subreddits.split(',')]
    subreddits = [s for s in subreddits if s]

    if not subreddits:
        parser.error('No usable subreddit names in --subreddits')

    from app.ingest.webshare import WebshareError
    from app.ingest.webshare import pool as build_pool

    try:
        proxy_pool = build_pool(args.webshare)
    except WebshareError as exc:
        print(f'\n{exc}\n', file=sys.stderr)
        return 1

    if proxy_pool:
        print(proxy_pool.describe(), flush=True)

    with sync_playwright() as playwright:
        session = BrowserSession(playwright, proxy_pool)

        try:
            for index, subreddit in enumerate(subreddits):
                print(f'\nr/{subreddit} ({args.sort})', flush=True)

                if index:
                    snooze(SUBREDDIT_PAUSE)
                    session.next_community()

                try:
                    meta, posts = browse_with_retry(
                        session, subreddit, args.sort, args.limit, args.time_filter
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
            session.close()

    return 0


if __name__ == '__main__':
    sys.exit(main())
