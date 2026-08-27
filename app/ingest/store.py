"""Writing fetched Reddit data into SQLite. Shared by both scrape backends."""

from datetime import datetime, timezone

# Self-post bodies can be enormous; the UI only ever renders a preview.
SELFTEXT_CAP = 2000


def epoch_to_iso(value):
    if not value:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def upsert_community(connection, community):
    """Insert or refresh one subreddit. Returns its Communities.id.

    COALESCE on the metadata columns matters: the browser backend can't see
    subscriber counts or creation dates, and without it a browser sweep would
    null out values a previous API ingest had filled in.
    """
    cursor = connection.cursor()

    cursor.execute(
        '''
        INSERT INTO Communities (
            name, display_name, title, public_description, subscribers,
            active_users, subreddit_type, over18, created_utc, url, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(name) DO UPDATE SET
            display_name = COALESCE(excluded.display_name, Communities.display_name),
            title = COALESCE(excluded.title, Communities.title),
            public_description = COALESCE(excluded.public_description,
                                          Communities.public_description),
            subscribers = COALESCE(excluded.subscribers, Communities.subscribers),
            active_users = COALESCE(excluded.active_users, Communities.active_users),
            subreddit_type = COALESCE(excluded.subreddit_type, Communities.subreddit_type),
            created_utc = COALESCE(excluded.created_utc, Communities.created_utc),
            over18 = excluded.over18,
            url = COALESCE(excluded.url, Communities.url),
            fetched_at = CURRENT_TIMESTAMP;
        ''',
        (
            community['name'], community.get('display_name'), community.get('title'),
            community.get('public_description'), community.get('subscribers'),
            community.get('active_users'), community.get('subreddit_type'),
            community.get('over18') or 0, community.get('created_utc'),
            community.get('url'),
        ),
    )

    row = cursor.execute(
        'SELECT id FROM Communities WHERE name = ?;', (community['name'],)
    ).fetchone()

    return row[0]


def upsert_posts(connection, community_id, posts, run_id=None):
    """Insert new posts, refresh the volatile fields on ones we already have.

    Returns (inserted, updated).

    first_seen_at / first_seen_run_id are set on the INSERT branch only, so a
    post keeps the timestamp of the sweep that discovered it no matter how many
    later sweeps refresh its score.
    """
    cursor = connection.cursor()

    rows = [p for p in posts if p.get('post_id')]

    if not rows:
        return 0, 0

    post_ids = [p['post_id'] for p in rows]
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
                p['post_id'], community_id, p['title'], p.get('author'),
                p.get('permalink'), p.get('url'), p.get('domain'), p.get('flair'),
                p.get('score'), p.get('upvote_ratio'), p.get('num_comments'),
                p.get('over18') or 0, p.get('is_self') or 0, p.get('stickied') or 0,
                (p.get('selftext') or '')[:SELFTEXT_CAP], p.get('created_utc'), run_id,
            )
            for p in rows
        ],
    )

    updated = len(existing)
    return len(post_ids) - updated, updated
