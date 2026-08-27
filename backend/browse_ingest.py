"""Ingest Reddit communities by driving a real browser, the way a person does.

Why a browser and not the JSON API:

    - www.reddit.com/r/x/hot.json answers 403 to plain HTTP clients.
    - old.reddit.com/r/x/hot.json redirects to a login wall.
    - Headless Chromium gets "You've been blocked by network security".

A headed Chromium window loads the same pages fine, so that is what this does:
open the subreddit, wait for the feed, scroll, and read the cards.

Two things about the modern Reddit feed shape the code:

    1. Every field worth having is a DOM attribute on <shreddit-post> -
       id, post-title, author, score, comment-count, upvote-ratio,
       created-timestamp, domain, permalink. No text parsing needed.

    2. The feed is VIRTUALISED. Cards are recycled out of the DOM as you
       scroll past them - a live count goes 27 -> 49 -> 25. So posts are
       harvested after every scroll and accumulated in a dict keyed by post
       id. Reading the DOM once at the end would silently lose most of them.

Usage:

    python browse_ingest.py --subreddits python,programming
    python browse_ingest.py --subreddits python --sort top --time-filter week --limit 150
    python browse_ingest.py --subreddits python --dry-run

The window is visible on purpose. Let it drive itself; clicking around in it
mid-run will fight the scroller.
"""

import argparse
import random
import sqlite3
import sys
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from reddit_ingest import DB_PATH, upsert_community, upsert_posts

BASE_DIR = Path(__file__).resolve().parent

# Pause between scroll steps. Randomised because a scraper that scrolls on a
# perfect 2000ms metronome is the easiest thing in the world to spot - and this
# repo exists to think about exactly that.
SCROLL_PAUSE = (1.4, 2.6)

# Breather between subreddits, so a multi-sub run isn't one long hammer.
SUBREDDIT_PAUSE = (3.0, 5.0)

SCROLL_STEP_PX = 2200

# Give up on a listing after this many scrolls that surface nothing new.
# Reddit's lazy loader routinely stalls for 3-4 scrolls before the next
# batch lands, so a tight budget here truncates listings silently.
DRY_SCROLLS = 8

# Feed previews only; the full body needs a per-post visit we don't make here.
PREVIEW_CAP = 2000

SORT_PATHS = {
    'hot': '',
    'new': 'new/',
    'top': 'top/',
    'rising': 'rising/',
}

TIME_FILTERS = ('hour', 'day', 'week', 'month', 'year', 'all')

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
        subreddit: attr('subreddit-name'),
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
    // count that is explicitly labelled "members", and take null otherwise.
    // Guessing at faceplate-number here picks up post vote counts instead.
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
        subreddit_id: attr('subreddit-id'),
    };
}
"""


def describe_size(community):
    """Report what the page actually gave us, not a zero standing in for null."""
    if community.get('subscribers'):
        return f"{community['subscribers']:,} subscribers"
    if community.get('active_users'):
        return f"{community['active_users']:,} weekly actives (subscriber count not rendered)"
    return "size unknown"


def snooze(bounds):
    time.sleep(random.uniform(*bounds))


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

    if card.get('promoted'):
        return None

    if not card.get('title'):
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

    flair = (card.get('flair') or '').strip() or None

    return {
        'post_id': raw_id[3:],
        'title': card['title'],
        'author': card.get('author'),
        'permalink': permalink,
        'url': card.get('content_href'),
        'domain': card.get('domain'),
        'flair': flair,
        'score': as_int(card.get('score')),
        'upvote_ratio': as_float(card.get('upvote_ratio')),
        'num_comments': as_int(card.get('comment_count')),
        'over18': 1 if card.get('nsfw') else 0,
        'is_self': 1 if (card.get('post_type') or '').startswith('self') else 0,
        'stickied': 0,
        'selftext': (card.get('preview') or '')[:PREVIEW_CAP],
        'created_utc': card.get('created'),
    }


def browse_subreddit(page, subreddit, sort, limit, time_filter):
    """Open the listing and scroll it, harvesting after every step.

    Returns (community_dict_or_None, [post rows]).
    """
    url = listing_url(subreddit, sort, time_filter)
    print(f"  opening {url}")

    response = page.goto(url, wait_until='domcontentloaded', timeout=60000)

    if response and response.status >= 400:
        print(f"  r/{subreddit}: HTTP {response.status}")
        return None, []

    body = (page.inner_text('body')[:400] if page.locator('body').count() else '').lower()

    if 'blocked by network security' in body:
        print(f"  r/{subreddit}: Reddit served a block page. Slow down or try again later.")
        return None, []

    if 'this community is private' in body or 'been banned' in body:
        print(f"  r/{subreddit}: private or banned")
        return None, []

    try:
        page.wait_for_selector('shreddit-post', timeout=30000)
    except PlaywrightTimeout:
        print(f"  r/{subreddit}: no posts rendered (does it exist?)")
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

        if len(posts) >= limit:
            break

        print(f"  r/{subreddit}: {len(posts)} posts harvested (+{gained})")

        # Wheel first (that's what a person does, and it fires the same
        # scroll handlers), then pin to the bottom so the sentinel the
        # lazy loader watches actually enters the viewport.
        page.mouse.wheel(0, SCROLL_STEP_PX)
        page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
        snooze(SCROLL_PAUSE)

    rows = list(posts.values())[:limit]
    print(f"  r/{subreddit}: {len(rows)} posts collected")

    return community, rows


def main():
    parser = argparse.ArgumentParser(
        description='Browse Reddit in a real browser and ingest what a user would see.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--subreddits', required=True,
                        help='Comma-separated subreddit names, e.g. python,programming')
    parser.add_argument('--sort', default='hot', choices=sorted(SORT_PATHS))
    parser.add_argument('--time-filter', default='week', choices=TIME_FILTERS,
                        help='Only used with --sort top (default: week)')
    parser.add_argument('--limit', type=int, default=100,
                        help='Posts per subreddit (default 100)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Browse and report, write nothing to the database')
    parser.add_argument('--headless', action='store_true',
                        help='Run without a window. Reddit currently blocks this - '
                             'here so you can watch it get blocked.')
    args = parser.parse_args()

    subreddits = [s.strip().lstrip('r/').strip('/') for s in args.subreddits.split(',')]
    subreddits = [s for s in subreddits if s]

    if not subreddits:
        parser.error('No usable subreddit names in --subreddits')

    if not Path(DB_PATH).exists() and not args.dry_run:
        parser.error(f'No database at {DB_PATH}. Run: python createDb.py')

    connection = None if args.dry_run else sqlite3.connect(DB_PATH)
    totals = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless)
        context = browser.new_context(
            viewport={'width': 1440, 'height': 900},
            locale='en-US',
        )
        page = context.new_page()

        try:
            for index, subreddit in enumerate(subreddits):
                print(f"\nr/{subreddit} ({args.sort})")

                if index:
                    snooze(SUBREDDIT_PAUSE)

                community, posts = browse_subreddit(
                    page, subreddit, args.sort, args.limit, args.time_filter
                )

                if community is None or not posts:
                    totals.append((subreddit, 0, 0))
                    continue

                if args.dry_run:
                    print(f"  [dry run] {describe_size(community)}, {len(posts)} posts, nothing written")
                    totals.append((subreddit, len(posts), 0))
                    continue

                community_row = {
                    'name': community['name'],
                    'display_name': community.get('display_name'),
                    'title': community.get('title'),
                    'public_description': community.get('public_description'),
                    'subscribers': community.get('subscribers'),
                    'active_users': community.get('active_users'),
                    'subreddit_type': 'public',
                    'over18': 0,
                    'created_utc': None,
                    'url': f"https://www.reddit.com/r/{community['name']}/",
                }

                community_id = upsert_community(connection, community_row)
                inserted, updated = upsert_posts(connection, community_id, posts)

                print(f"  {describe_size(community)} | "
                      f"{inserted} new post(s), {updated} refreshed")
                totals.append((subreddit, inserted, updated))
        finally:
            browser.close()
            if connection is not None:
                connection.close()

    print("\n" + "-" * 46)
    for subreddit, inserted, updated in totals:
        print(f"  r/{subreddit:<26} +{inserted:<5} ~{updated}")

    if not args.dry_run and any(t[1] or t[2] for t in totals):
        from seeder import seed_watchlists
        seed_watchlists()
        print("\nDone. Start the portal and sign in to see them at /records.")

    return 0


if __name__ == '__main__':
    sys.exit(main())
