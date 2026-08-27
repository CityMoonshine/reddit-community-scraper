"""Sweep history and the "Run sweep now" trigger."""

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import SCRAPE_BACKEND, SWEEP_INTERVAL_MINUTES
from app.db import connection_scope

router = APIRouter(prefix='/api')


class RunBody(BaseModel):
    backend: str = SCRAPE_BACKEND
    # None sweeps every monitored community; an id sweeps just that one.
    community_id: int | None = None


@router.get('/runs')
def list_runs(limit: int = Query(10, ge=1, le=100)):
    with connection_scope() as connection:
        runs = connection.execute(
            '''
            SELECT r.*,
                   c.name AS only_community_name,
                   (SELECT COUNT(*) FROM MonitorRunItems i
                     WHERE i.run_id = r.id AND i.status != 'ok') AS failed_items
            FROM MonitorRuns r
            LEFT JOIN Communities c ON c.id = r.only_community_id
            ORDER BY r.id DESC
            LIMIT ?;
            ''',
            (limit,),
        ).fetchall()

        active = connection.execute(
            """
            SELECT * FROM MonitorRuns
            WHERE status IN ('queued', 'running')
            ORDER BY id LIMIT 1;
            """
        ).fetchone()

    return {
        'runs': [dict(row) for row in runs],
        'active': dict(active) if active else None,
        'interval_minutes': SWEEP_INTERVAL_MINUTES,
        'default_backend': SCRAPE_BACKEND,
    }


@router.get('/runs/{run_id}')
def run_detail(run_id: int):
    with connection_scope() as connection:
        run = connection.execute(
            'SELECT * FROM MonitorRuns WHERE id = ?;', (run_id,)
        ).fetchone()

        if run is None:
            return JSONResponse({'detail': 'No such run'}, status_code=404)

        items = connection.execute(
            'SELECT * FROM MonitorRunItems WHERE run_id = ? ORDER BY id;', (run_id,)
        ).fetchall()

    return {'run': dict(run), 'items': [dict(row) for row in items]}


@router.get('/discoveries')
def discoveries(limit: int = Query(25, ge=1, le=200)):
    """The point of the hourly sweep: what showed up that wasn't there before."""
    with connection_scope() as connection:
        rows = connection.execute(
            '''
            SELECT p.*, c.name AS community_name, c.display_name AS community_display,
                   s.score AS ai_score, s.verdict AS ai_verdict,
                   s.rationale AS ai_rationale
            FROM Posts p
            JOIN Communities c ON c.id = p.community_id
            LEFT JOIN PostScores s
                   ON s.post_id = p.id
                  AND s.prompt_id = (SELECT id FROM ScoringPrompts
                                      WHERE is_active = 1 ORDER BY id DESC LIMIT 1)
            WHERE p.first_seen_at IS NOT NULL
            ORDER BY p.first_seen_at DESC, p.score DESC
            LIMIT ?;
            ''',
            (limit,),
        ).fetchall()

    return {'discoveries': [dict(row) for row in rows]}


@router.post('/runs')
def queue_run(body: RunBody):
    """Ask the worker for an out-of-band sweep.

    This container has no Chromium and no Xvfb, so it cannot run the sweep
    itself. It queues a row; the worker claims it within QUEUE_POLL_SECONDS.
    """
    backend = body.backend if body.backend in ('browser', 'api') else SCRAPE_BACKEND

    with connection_scope() as connection:
        cursor = connection.cursor()

        pending = cursor.execute(
            "SELECT id, status FROM MonitorRuns WHERE status IN ('queued', 'running') LIMIT 1;"
        ).fetchone()

        if pending:
            return JSONResponse(
                {'detail': f"A sweep is already {pending['status']} (#{pending['id']})."},
                status_code=409,
            )

        target = None

        if body.community_id is not None:
            target = cursor.execute(
                'SELECT id, name FROM Communities WHERE id = ?;', (body.community_id,)
            ).fetchone()

            if target is None:
                return JSONResponse({'detail': 'No such community.'}, status_code=404)

        cursor.execute(
            '''
            INSERT INTO MonitorRuns (trigger, backend, queued_at, status,
                                     only_community_id, kind)
            VALUES ('manual', ?, datetime('now'), 'queued', ?, 'sweep');
            ''',
            (backend, target['id'] if target else None),
        )
        run_id = cursor.lastrowid

    scope = f"r/{target['name']}" if target else 'all monitored communities'

    return {'run_id': run_id,
            'detail': f'Sweep queued for {scope}. The worker picks it up within a few seconds.'}
