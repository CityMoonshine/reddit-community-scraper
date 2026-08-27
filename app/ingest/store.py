"""Writing fetched Reddit data into SQLite. Shared by both scrape backends."""

import json
from datetime import datetime, timezone

# Self-post bodies can be enormous. This is generous rather than tight because
# the AI scorer reads the stored body - truncating to a UI preview length would
# mean ranking posts on their first paragraph. Posts.selftext_chars records the
# true length, so a truncated body is visible as truncated.
SELFTEXT_CAP = 40000

# Columns the browser backend often cannot see. Refreshing them with NULL would
# wipe values a previous API sweep filled in, so the upsert COALESCEs these.
PRESERVE_ON_NULL = (
    'thumbnail', 'preview_image', 'media_url', 'post_hint', 'gallery_urls',
    'total_awards', 'gilded', 'edited_utc', 'distinguished', 'author_flair',
    'flair_bg', 'flair_text_color', 'num_crossposts', 'crosspost_origin',
    'removed_by_category', 'upvote_ratio', 'url', 'domain', 'created_utc',
    'selftext_chars',
    # Flags only the OAuth payload carries. They must stay nullable for this to
    # work: coercing an absent flag to 0 would make "the browser cannot see
    # this" indistinguishable from "Reddit says no", and a browser sweep would
    # quietly clear them.
    'archived', 'contest_mode', 'is_original_content',
)


def as_json(value):
    """Lists are stored as JSON text - sqlite has no array type."""
    if not value:
        return None
    return json.dumps(value)


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


# Declared once and used to build the column list, the placeholders and the row
# tuples. Keeping three parallel 39-item lists in sync by hand is exactly how a
# column ends up quietly holding the value of its neighbour.
POST_COLUMNS = (
    'post_id', 'community_id', 'title', 'author', 'permalink', 'url', 'domain',
    'flair', 'score', 'upvote_ratio', 'num_comments', 'over18', 'is_self',
    'stickied', 'selftext', 'selftext_chars', 'created_utc',
    'thumbnail', 'preview_image', 'media_url', 'post_hint', 'is_video',
    'is_gallery', 'gallery_urls', 'total_awards', 'gilded', 'edited_utc',
    'locked', 'archived', 'spoiler', 'distinguished', 'contest_mode',
    'is_original_content', 'author_flair', 'flair_bg', 'flair_text_color',
    'num_crossposts', 'crosspost_origin', 'removed_by_category',
)

# Identity, not content. Refreshing these would either be a no-op or would move
# a post between communities on a crosspost sighting.
IMMUTABLE = ('post_id', 'community_id')

INT_FLAGS = (
    'over18', 'is_self', 'stickied', 'is_video', 'is_gallery', 'locked',
    'archived', 'spoiler', 'contest_mode', 'is_original_content',
)


def _refresh_clause():
    """The ON CONFLICT SET body: COALESCE for the columns a backend may not see."""
    parts = []

    for column in POST_COLUMNS:
        if column in IMMUTABLE:
            continue

        if column == 'selftext':
            # Never trade a longer body for a shorter one. The browser backend
            # can only see the feed's truncated teaser, so without this a
            # browser sweep following an API sweep would replace a complete
            # self-post with its first paragraph - and the AI scorer reads this
            # column, so the damage would show up as worse rankings, not as an
            # obviously empty field.
            parts.append(
                'selftext = CASE WHEN length(excluded.selftext) >= '
                'length(COALESCE(Posts.selftext, \'\')) '
                'THEN excluded.selftext ELSE Posts.selftext END'
            )
        elif column in PRESERVE_ON_NULL:
            parts.append(f'{column} = COALESCE(excluded.{column}, Posts.{column})')
        else:
            parts.append(f'{column} = excluded.{column}')

    parts.append('fetched_at = CURRENT_TIMESTAMP')
    return ',\n            '.join(parts)


def post_values(post, community_id, run_id):
    """One post dict -> the row tuple, in POST_COLUMNS order plus the extras."""
    selftext = post.get('selftext') or ''

    values = {
        **post,
        'community_id': community_id,
        'selftext': selftext[:SELFTEXT_CAP],
        'gallery_urls': as_json(post.get('gallery_urls')),
    }

    # The true length, not the stored length - so a body clipped at the cap is
    # identifiable rather than passing for a complete post. An explicit None
    # from a backend means "I saw a teaser, not the body" and is honoured;
    # only a backend that never mentions the field falls back to measuring.
    if 'selftext_chars' not in post:
        values['selftext_chars'] = len(selftext) or None

    for flag in INT_FLAGS:
        # Absent means unknown, and unknown must stay NULL for the COALESCE in
        # the refresh clause to have anything to preserve.
        if flag in values and values[flag] is not None:
            values[flag] = 1 if values[flag] else 0
        else:
            values[flag] = None

    return tuple(values.get(column) for column in POST_COLUMNS) + (run_id,)


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

    columns = ', '.join(POST_COLUMNS)
    placeholders = ', '.join('?' * len(POST_COLUMNS))

    cursor.executemany(
        f'''
        INSERT INTO Posts (
            {columns}, fetched_at, first_seen_at, first_seen_run_id
        ) VALUES ({placeholders}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)
        ON CONFLICT(post_id) DO UPDATE SET
            {_refresh_clause()};
        ''',
        [post_values(p, community_id, run_id) for p in rows],
    )

    updated = len(existing)
    return len(post_ids) - updated, updated
