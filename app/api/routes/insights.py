"""Aggregates for the dashboard charts.

One endpoint rather than several: every chart on a page is drawn from the same
instant, and three round trips would let the strip and the charts disagree about
how many posts exist.

Everything here is a GROUP BY over data the sweeps already wrote. Nothing is
computed on the fly per request beyond what sqlite does in the query, so this
stays cheap enough to sit behind the same poll as the status strip.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query

from app.db import connection_scope

router = APIRouter(prefix='/api')

# Buckets are ordered, which is what makes a one-hue ramp the right encoding for
# them. Kept server-side so the chart and any future export agree on the edges.
SCORE_BUCKETS = ((0, 19), (20, 39), (40, 59), (60, 79), (80, 100))


def day_series(connection, days):
    """Posts first seen over time, zero-filled, at whichever granularity has
    something to show.

    Granularity is chosen from the data, not fixed. A portal that has been
    collecting for a day has all its posts inside one daily bucket: charted by
    day that is thirteen empty columns and one spike, which reads as "nothing is
    happening" when the opposite is true. Under three distinct days of history
    the series switches to hourly over 48 hours, where the sweep rhythm is
    actually visible. The client renders whatever it is handed and reads the
    label off `granularity`.

    Zero-filling happens here rather than in SQL because a bucket with no
    discoveries produces no row at all, and a chart that silently omits empty
    buckets compresses time - making a quiet week look busy.
    """
    distinct_days = connection.execute(
        'SELECT COUNT(DISTINCT substr(first_seen_at, 1, 10)) FROM Posts '
        'WHERE first_seen_at IS NOT NULL;'
    ).fetchone()[0]

    if distinct_days < 3:
        return hour_series(connection, 48), 'hour'

    since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date()

    rows = connection.execute(
        '''
        SELECT substr(first_seen_at, 1, 10) AS bucket, COUNT(*) AS posts
        FROM Posts
        WHERE first_seen_at >= ?
        GROUP BY bucket;
        ''',
        (since.isoformat(),),
    ).fetchall()

    found = {row['bucket']: row['posts'] for row in rows}

    return [
        {'bucket': (d := (since + timedelta(days=i)).isoformat()),
         'posts': found.get(d, 0)}
        for i in range(days)
    ], 'day'


def hour_series(connection, hours):
    """Posts first seen per hour, zero-filled."""
    rows = connection.execute(
        '''
        SELECT substr(first_seen_at, 1, 13) AS bucket, COUNT(*) AS posts
        FROM Posts
        WHERE first_seen_at >= datetime('now', ?)
        GROUP BY bucket;
        ''',
        (f'-{hours} hours',),
    ).fetchall()

    # sqlite writes 'YYYY-MM-DD HH', python's isoformat writes 'YYYY-MM-DDTHH'.
    # Both land in this column, so normalise before matching or half the
    # buckets silently miss.
    found = {}
    for row in rows:
        found[row['bucket'].replace('T', ' ')] = row['posts']

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    return [
        {'bucket': (k := (now - timedelta(hours=hours - 1 - i)).strftime('%Y-%m-%d %H')),
         'posts': found.get(k, 0)}
        for i in range(hours)
    ]


def community_series(connection, days):
    """Per-community totals plus a daily count series for the sparklines."""
    since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date()

    rows = connection.execute(
        '''
        SELECT c.id, c.name, c.display_name,
               COUNT(p.id) AS posts,
               SUM(CASE WHEN p.first_seen_at >= datetime('now', '-1 day')
                        THEN 1 ELSE 0 END) AS new_24h
        FROM Communities c
        LEFT JOIN Posts p ON p.community_id = c.id
        GROUP BY c.id
        ORDER BY posts DESC;
        '''
    ).fetchall()

    daily = connection.execute(
        '''
        SELECT community_id, substr(first_seen_at, 1, 10) AS day, COUNT(*) AS posts
        FROM Posts
        WHERE first_seen_at >= ?
        GROUP BY community_id, day;
        ''',
        (since.isoformat(),),
    ).fetchall()

    by_community = {}
    for row in daily:
        by_community.setdefault(row['community_id'], {})[row['day']] = row['posts']

    span = [(since + timedelta(days=i)).isoformat() for i in range(days)]

    return [
        {
            'id': row['id'],
            'name': row['name'],
            'display_name': row['display_name'],
            'posts': row['posts'] or 0,
            'new_24h': row['new_24h'] or 0,
            'daily': [by_community.get(row['id'], {}).get(day, 0) for day in span],
        }
        for row in rows
    ]


def score_distribution(connection):
    """AI scores under the active rubric, in ordered buckets.

    Ordered is the operative word: it is what makes a single-hue ramp the
    correct encoding here, where it would be wrong on the nominal series.
    """
    rows = connection.execute(
        '''
        SELECT s.score FROM PostScores s
        WHERE s.status = 'ok' AND s.score IS NOT NULL
          AND s.prompt_id = (SELECT id FROM ScoringPrompts
                              WHERE is_active = 1 ORDER BY id DESC LIMIT 1);
        '''
    ).fetchall()

    scores = [row['score'] for row in rows]

    return [
        {
            'label': f'{low}–{high}',
            'low': low,
            'high': high,
            'posts': sum(1 for s in scores if low <= s <= high),
        }
        for low, high in SCORE_BUCKETS
    ]


# Reddit scores are unbounded and heavily skewed - a linear bucketing puts
# every post in the first bucket and one outlier in the last. Ordered
# magnitude bands, so the one-hue ramp is legitimate here too.
REDDIT_BANDS = ((0, 9, '0–9'), (10, 99, '10–99'), (100, 999, '100–999'),
                (1000, 9999, '1k–10k'), (10000, 10 ** 9, '10k+'))


def reddit_distribution(connection):
    """Reddit's own score distribution.

    Shown in place of the AI histogram before anything has been scored. It is
    labelled as Reddit's rather than dressed up as the rubric's - an empty
    panel teaches nothing, but a mislabelled one is worse than empty.
    """
    rows = connection.execute(
        'SELECT score FROM Posts WHERE score IS NOT NULL;'
    ).fetchall()

    scores = [row['score'] for row in rows]

    return [
        {'label': label, 'low': low, 'high': high,
         'posts': sum(1 for s in scores if low <= s <= high)}
        for low, high, label in REDDIT_BANDS
    ]


def scoring_spend(connection):
    """Token spend under the active rubric.

    Cached tokens are reported separately because they are the whole argument
    for scoring in passes - a number that only shows the total hides the saving
    the design exists to produce.
    """
    row = connection.execute(
        '''
        SELECT COUNT(*) AS verdicts,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COALESCE(AVG(score), 0) AS mean_score
        FROM PostScores
        WHERE status = 'ok'
          AND prompt_id = (SELECT id FROM ScoringPrompts
                            WHERE is_active = 1 ORDER BY id DESC LIMIT 1);
        '''
    ).fetchone()

    return dict(row)


def request_series(connection, hours=24):
    """Requests per hour, zero-filled - the detection page's pulse."""
    rows = connection.execute(
        '''
        SELECT substr(requested_at, 1, 13) AS hour, COUNT(*) AS hits
        FROM RequestLog
        WHERE requested_at >= datetime('now', ?)
        GROUP BY hour;
        ''',
        (f'-{hours} hours',),
    ).fetchall()

    found = {row['hour']: row['hits'] for row in rows}
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    return [
        {'hour': (k := (now - timedelta(hours=hours - 1 - i)).strftime('%Y-%m-%d %H')),
         'hits': found.get(k, 0)}
        for i in range(hours)
    ]


def detection_totals(connection):
    return dict(connection.execute(
        '''
        SELECT
            (SELECT COUNT(*) FROM Sessions) AS sessions,
            (SELECT COUNT(*) FROM RequestLog) AS requests,
            (SELECT COUNT(*) FROM ScoreEvents) AS signals,
            (SELECT COUNT(*) FROM Fingerprints) AS fingerprints,
            (SELECT COUNT(*) FROM BehaviorEvents) AS behaviours,
            (SELECT COUNT(*) FROM RequestLog
              WHERE requested_at >= datetime('now', '-1 hour')) AS requests_1h;
        '''
    ).fetchone())


@router.get('/insights')
def insights(days: int = Query(14, ge=2, le=90)):
    with connection_scope() as connection:
        discovery, granularity = day_series(connection, days)

        return {
            'days': days,
            'granularity': granularity,
            'discovery': discovery,
            'communities': community_series(connection, days),
            'scores': score_distribution(connection),
            'reddit_scores': reddit_distribution(connection),
            'spend': scoring_spend(connection),
            'requests': request_series(connection),
            'detection': detection_totals(connection),
        }
