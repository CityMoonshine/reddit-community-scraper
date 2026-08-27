"""Managing the monitored set - add, pause, remove. Open, no login."""

import re

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.db import connection_scope

router = APIRouter(prefix='/api')

# Reddit's own rule for subreddit names. Validated before the name reaches a
# URL or the database, so a bad paste fails here rather than mid-sweep.
SUBREDDIT_NAME = re.compile(r'^[A-Za-z0-9_]{2,21}$')

MONITOR_SORTS = ('new', 'hot', 'top', 'rising')


def normalise_name(raw):
    """Accept what people actually paste: 'r/python', '/r/python/', a full URL."""
    cleaned = raw.strip()
    cleaned = re.sub(r'^https?://(www\.|old\.|new\.)?reddit\.com', '', cleaned, flags=re.I)
    cleaned = cleaned.strip('/')
    cleaned = re.sub(r'^r/', '', cleaned, flags=re.I)
    return cleaned.split('/')[0].strip()


class AddBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    monitor_sort: str = 'new'
    monitor_limit: int = 50


class CommunityRef(BaseModel):
    community_id: int


@router.get('/communities')
def list_communities():
    with connection_scope() as connection:
        rows = connection.execute(
            '''
            SELECT
                c.*,
                (SELECT COUNT(*) FROM Posts p WHERE p.community_id = c.id) AS post_count,
                (SELECT COUNT(*) FROM Posts p
                  WHERE p.community_id = c.id
                    AND p.first_seen_at >= datetime('now', '-1 day')) AS new_24h,
                (SELECT MAX(p.first_seen_at) FROM Posts p
                  WHERE p.community_id = c.id) AS newest_at
            FROM Communities c
            ORDER BY c.monitor_enabled DESC, c.name COLLATE NOCASE;
            '''
        ).fetchall()

    return {'communities': [dict(row) for row in rows]}


@router.post('/communities')
def add_community(body: AddBody):
    """Add a subreddit to the monitored set.

    The row is created empty - the next sweep fills in metadata and posts. That
    keeps the request fast instead of blocking it on a multi-minute browse, and
    in the split layout the API has no browser to do it with anyway.
    """
    cleaned = normalise_name(body.name)

    if not SUBREDDIT_NAME.match(cleaned):
        return JSONResponse(
            {'detail': f'Not a valid subreddit name: {body.name.strip()[:40]}'},
            status_code=400,
        )

    monitor_sort = body.monitor_sort if body.monitor_sort in MONITOR_SORTS else 'new'
    monitor_limit = max(10, min(500, body.monitor_limit))

    with connection_scope() as connection:
        cursor = connection.cursor()

        cursor.execute(
            '''
            INSERT INTO Communities (name, display_name, monitor_enabled,
                                     monitor_sort, monitor_limit)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                monitor_enabled = 1,
                monitor_sort = excluded.monitor_sort,
                monitor_limit = excluded.monitor_limit;
            ''',
            (cleaned, 'r/' + cleaned, monitor_sort, monitor_limit),
        )

        community_id = cursor.execute(
            'SELECT id FROM Communities WHERE name = ?;', (cleaned,)
        ).fetchone()['id']

    return {'name': cleaned, 'community_id': community_id,
            'detail': f'Monitoring r/{cleaned}. It fills in on the next sweep.'}


@router.post('/communities/toggle')
def toggle_community(body: CommunityRef):
    with connection_scope() as connection:
        connection.execute(
            '''
            UPDATE Communities
            SET monitor_enabled = CASE WHEN monitor_enabled = 1 THEN 0 ELSE 1 END
            WHERE id = ?;
            ''',
            (body.community_id,),
        )

    return {'ok': True}


@router.post('/communities/remove')
def remove_community(body: CommunityRef):
    """Stop monitoring and drop the community.

    Posts are deleted with it - they reference the community by foreign key,
    and leaving them orphaned would show rows in the feed for a subreddit the
    dashboard no longer lists.
    """
    with connection_scope() as connection:
        connection.execute(
            'DELETE FROM Watchlist WHERE community_id = ?;', (body.community_id,)
        )
        connection.execute(
            'DELETE FROM Posts WHERE community_id = ?;', (body.community_id,)
        )
        connection.execute(
            'DELETE FROM MonitorRunItems WHERE community_id = ?;', (body.community_id,)
        )
        connection.execute(
            'DELETE FROM Communities WHERE id = ?;', (body.community_id,)
        )

    return {'ok': True}
