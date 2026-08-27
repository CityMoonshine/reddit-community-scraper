"""The feed - what /records used to render server-side."""

from fastapi import APIRouter, Depends, Query

from app.api.auth import require_session
from app.db import connection_scope

router = APIRouter(prefix='/api')

# Interpolated into the query rather than bound, so it is a whitelist, not a
# parameter. Anything not in here falls back to score.
ORDERINGS = {
    'score': 'p.score DESC',
    'comments': 'p.num_comments DESC',
    'new': 'p.created_utc DESC',
    'discovered': 'p.first_seen_at DESC',
}


@router.get('/posts')
def list_posts(
    session=Depends(require_session),
    community: str = '',
    flair: str = '',
    sort: str = 'score',
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    # Posts are gated by the watchlist, not by a column on the row itself - an
    # account sees a subreddit's posts only while it watches that subreddit.
    where = ['w.user_id = ?']
    params = [session['user_id']]

    if community:
        where.append('c.name = ?')
        params.append(community)

    if flair:
        where.append('p.flair = ?')
        params.append(flair)

    clause = ' AND '.join(where)
    ordering = ORDERINGS.get(sort, ORDERINGS['score'])

    with connection_scope() as connection:
        total = connection.execute(
            f'''
            SELECT COUNT(*) FROM Posts p
            JOIN Communities c ON c.id = p.community_id
            JOIN Watchlist w ON w.community_id = c.id
            WHERE {clause};
            ''',
            params,
        ).fetchone()[0]

        rows = connection.execute(
            f'''
            SELECT p.*, c.name AS community_name, c.display_name AS community_display
            FROM Posts p
            JOIN Communities c ON c.id = p.community_id
            JOIN Watchlist w ON w.community_id = c.id
            WHERE {clause}
            ORDER BY {ordering}
            LIMIT ? OFFSET ?;
            ''',
            params + [per_page, (page - 1) * per_page],
        ).fetchall()

        flairs = [
            row['flair'] for row in connection.execute(
                '''
                SELECT DISTINCT p.flair
                FROM Posts p
                JOIN Watchlist w ON w.community_id = p.community_id
                WHERE w.user_id = ? AND p.flair IS NOT NULL AND p.flair != ''
                ORDER BY p.flair;
                ''',
                (session['user_id'],),
            ).fetchall()
        ]

    return {
        'posts': [dict(row) for row in rows],
        'flairs': flairs,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': max(1, -(-total // per_page)),
    }
