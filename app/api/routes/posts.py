"""The feed.

Open, and unfiltered by account: every post from every community is visible.
The Watchlist table still exists (the seeder and older data reference it) but
nothing here joins against it any more.

Every row carries its AI verdict under the *active* rubric, joined on
prompt_id. That join is what makes rubric versioning honest: change the rubric
and these columns go null until the worker rescores, rather than continuing to
show numbers that measured something else.
"""

import json

from fastapi import APIRouter, Query

from app.db import connection_scope

router = APIRouter(prefix='/api')

# Interpolated into the query rather than bound, so it is a whitelist, not a
# parameter. Anything not in here falls back to rank.
ORDERINGS = {
    # Unscored posts sort last rather than first: a NULL score means "not yet
    # judged", and floating those to the top of a ranked feed would make the
    # ranking look broken every time new posts land.
    'rank': 's.score IS NULL, s.score DESC, p.score DESC',
    'contrarian': ('s.score IS NULL, '
                   'CAST(s.score AS REAL) - (p.score * 100.0 / '
                   '  NULLIF((SELECT MAX(score) FROM Posts), 0)) DESC'),
    'score': 'p.score DESC',
    'comments': 'p.num_comments DESC',
    'new': 'p.created_utc DESC',
    'discovered': 'p.first_seen_at DESC',
}


def shape(row):
    """One joined row -> the post dict the UI renders."""
    post = dict(row)

    # Stored as JSON text because sqlite has no array type; the UI wants them
    # as arrays, and parsing in the client would mean every consumer knowing
    # this column is doubly encoded.
    for column in ('dimensions', 'gallery_urls'):
        raw = post.get(column)
        if raw:
            try:
                post[column] = json.loads(raw)
            except (TypeError, ValueError):
                post[column] = None

    return post


@router.get('/posts')
def list_posts(
    community: str = '',
    flair: str = '',
    sort: str = 'rank',
    min_score: int = Query(0, ge=0, le=100),
    unscored: str = 'include',
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    where = ['1=1']
    params = []

    if community:
        where.append('c.name = ?')
        params.append(community)

    if flair:
        where.append('p.flair = ?')
        params.append(flair)

    if min_score:
        where.append('s.score >= ?')
        params.append(min_score)

    if unscored == 'only':
        where.append('s.id IS NULL')
    elif unscored == 'exclude':
        where.append('s.id IS NOT NULL')

    clause = ' AND '.join(where)
    ordering = ORDERINGS.get(sort, ORDERINGS['rank'])

    # The join is against whichever rubric is active right now. Resolved as a
    # scalar subquery rather than fetched first so the whole read stays one
    # round trip and cannot straddle a rubric change.
    join = '''
        LEFT JOIN PostScores s
               ON s.post_id = p.id
              AND s.prompt_id = (SELECT id FROM ScoringPrompts
                                  WHERE is_active = 1 ORDER BY id DESC LIMIT 1)
    '''

    with connection_scope() as connection:
        total = connection.execute(
            f'''
            SELECT COUNT(*) FROM Posts p
            JOIN Communities c ON c.id = p.community_id
            {join}
            WHERE {clause};
            ''',
            params,
        ).fetchone()[0]

        rows = connection.execute(
            f'''
            SELECT p.*,
                   c.name AS community_name,
                   c.display_name AS community_display,
                   s.score AS ai_score,
                   s.verdict AS ai_verdict,
                   s.rationale AS ai_rationale,
                   s.dimensions AS dimensions,
                   s.status AS ai_status,
                   s.error AS ai_error,
                   s.scored_at AS ai_scored_at
            FROM Posts p
            JOIN Communities c ON c.id = p.community_id
            {join}
            WHERE {clause}
            ORDER BY {ordering}
            LIMIT ? OFFSET ?;
            ''',
            params + [per_page, (page - 1) * per_page],
        ).fetchall()

        flairs = [
            row['flair'] for row in connection.execute(
                '''
                SELECT DISTINCT flair FROM Posts
                WHERE flair IS NOT NULL AND flair != ''
                ORDER BY flair;
                '''
            ).fetchall()
        ]

    return {
        'posts': [shape(row) for row in rows],
        'flairs': flairs,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': max(1, -(-total // per_page)),
        # The offset of the first row, so the UI can number a ranked list
        # continuously across pages instead of restarting at 1.
        'rank_offset': (page - 1) * per_page,
        'sort': sort,
    }
