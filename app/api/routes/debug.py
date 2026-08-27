"""The detection views. Unauthenticated by design - this is a lab target.

Excluded from RequestLog via UNLOGGED_PREFIXES, so inspecting the log doesn't
write to the log.
"""

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.db import connection_scope

router = APIRouter(prefix='/api/debug')


@router.get('/sessions')
def sessions(limit: int = Query(200, ge=1, le=1000)):
    with connection_scope() as connection:
        rows = connection.execute(
            '''
            SELECT
                s.id, s.session_token, s.ip_address, s.user_agent,
                s.created_at, s.expires_at, s.bot_score, s.verdict,
                u.username,
                (SELECT COUNT(*) FROM RequestLog r WHERE r.session_id = s.id) AS request_count,
                (SELECT COUNT(*) FROM ScoreEvents e WHERE e.session_id = s.id) AS signal_count,
                (SELECT COUNT(*) FROM Fingerprints f WHERE f.session_id = s.id) AS fingerprint_count
            FROM Sessions s
            LEFT JOIN Users u ON u.id = s.user_id
            ORDER BY s.created_at DESC
            LIMIT ?;
            ''',
            (limit,),
        ).fetchall()

    return {'sessions': [dict(row) for row in rows]}


@router.get('/sessions/{session_id}')
def session_detail(session_id: int):
    """Full replay for one session: requests, signals and behaviour on one timeline."""
    with connection_scope() as connection:
        session = connection.execute(
            '''
            SELECT s.*, u.username, u.agency_name
            FROM Sessions s
            LEFT JOIN Users u ON u.id = s.user_id
            WHERE s.id = ?;
            ''',
            (session_id,),
        ).fetchone()

        if session is None:
            return JSONResponse({'detail': 'No such session'}, status_code=404)

        fingerprints = connection.execute(
            'SELECT * FROM Fingerprints WHERE session_id = ? ORDER BY collected_at;',
            (session_id,),
        ).fetchall()

        requests_log = connection.execute(
            'SELECT * FROM RequestLog WHERE session_id = ? ORDER BY requested_at;',
            (session_id,),
        ).fetchall()

        score_events = connection.execute(
            'SELECT * FROM ScoreEvents WHERE session_id = ? ORDER BY fired_at;',
            (session_id,),
        ).fetchall()

        behavior_events = connection.execute(
            'SELECT * FROM BehaviorEvents WHERE session_id = ? ORDER BY occurred_at;',
            (session_id,),
        ).fetchall()

    # Merge the streams into one chronological timeline. Doing this here rather
    # than as a UNION keeps the differing columns readable.
    timeline = []

    for row in requests_log:
        detail = f"{row['latency_ms']}ms"
        if row['injected_fault']:
            detail += f" | fault: {row['injected_fault']}"
        if row['header_order']:
            detail += f" | headers: {row['header_order']}"

        timeline.append({
            'at': row['requested_at'],
            'kind': 'request',
            'summary': f"{row['method']} {row['path']} -> {row['status_code']}",
            'detail': detail,
            'weight': None,
        })

    for row in score_events:
        detail = f"saw {row['observed_value']!r}, expected {row['expected_value']!r}"
        if row['note']:
            detail += f" | {row['note']}"

        timeline.append({
            'at': row['fired_at'],
            'kind': 'signal',
            'summary': f"{row['signal_name']} ({row['signal_source']})",
            'detail': detail,
            'weight': row['weight'],
        })

    for row in behavior_events:
        timeline.append({
            'at': row['occurred_at'],
            'kind': 'behavior',
            'summary': row['event_type'],
            'detail': f"{row['target_selector'] or ''} "
                      f"({row['pointer_x']},{row['pointer_y']}) "
                      f"+{row['ms_since_previous']}ms".strip(),
            'weight': None,
        })

    timeline.sort(key=lambda entry: entry['at'] or '')

    running = 0
    for entry in timeline:
        if entry['weight']:
            running += entry['weight']
        entry['running_score'] = running

    return {
        'session': dict(session),
        'fingerprints': [dict(row) for row in fingerprints],
        'timeline': timeline,
        'total_score': running,
    }
