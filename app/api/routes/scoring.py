"""The ranking rubric: read it, rewrite it, roll it back.

This container never calls Claude. It owns the rubric as data and queues work;
the worker owns the API key and does the scoring. Keeping it that way means the
public-facing service holds no model credentials at all.

Editing the rubric is the expensive button on the dashboard - it invalidates
every score in the database at once - so the response says exactly how many
posts that is before anything is spent.
"""

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.auth import session_id_from_request
from app.config import SCORING_ENABLED, SCORING_MODEL
from app.db import connection_scope
from app.ingest.scoring import (ScoringError, activate_prompt, active_prompt,
                                pending_count, prompt_history, set_prompt)

router = APIRouter(prefix='/api')

# The rubric is a prompt, not a novel. A cap here is what stops the dashboard
# from being a way to paste a megabyte into every scoring request.
MAX_RUBRIC_CHARS = 8000


class RubricBody(BaseModel):
    body: str
    label: str | None = None
    # Queue a scoring pass straight away rather than waiting for the next
    # sweep. Default on: an operator who just rewrote the rubric wants to see
    # what it does, not to wait an hour.
    rescore: bool = True


class ActivateBody(BaseModel):
    prompt_id: int


def user_id_for(request: Request):
    """Whoever is logged in, or None. Attribution only - never a gate."""
    session_id = session_id_from_request(request)

    if session_id is None:
        return None

    with connection_scope() as connection:
        row = connection.execute(
            'SELECT user_id FROM Sessions WHERE id = ?;', (session_id,)
        ).fetchone()

    return row['user_id'] if row else None


def scoring_stats(prompt_id):
    """Coverage for the active rubric - what is scored, what is queued."""
    with connection_scope() as connection:
        return dict(connection.execute(
            '''
            SELECT
                (SELECT COUNT(*) FROM Posts) AS posts,
                (SELECT COUNT(*) FROM PostScores
                  WHERE prompt_id = ? AND status = 'ok') AS scored,
                (SELECT COUNT(*) FROM PostScores
                  WHERE prompt_id = ? AND status != 'ok') AS failed,
                (SELECT AVG(score) FROM PostScores
                  WHERE prompt_id = ? AND status = 'ok') AS mean_score;
            ''',
            (prompt_id, prompt_id, prompt_id),
        ).fetchone())


@router.get('/scoring')
def read_scoring(history: int = Query(10, ge=0, le=50)):
    prompt = active_prompt()

    if prompt is None:
        return {'enabled': SCORING_ENABLED, 'prompt': None, 'history': [],
                'stats': None, 'pending': 0, 'model': SCORING_MODEL}

    return {
        'enabled': SCORING_ENABLED,
        'model': SCORING_MODEL,
        'prompt': prompt,
        'history': prompt_history(history) if history else [],
        'stats': scoring_stats(prompt['id']),
        'pending': pending_count(prompt['id']),
        'max_chars': MAX_RUBRIC_CHARS,
    }


def queue_scoring_run():
    """Queue a score-only run, or explain why it could not be queued.

    Shares the one-run-at-a-time guard with sweeps deliberately: a rescore that
    started mid-sweep would race the sweep's own scoring pass for the same
    backlog.
    """
    with connection_scope() as connection:
        cursor = connection.cursor()

        pending = cursor.execute(
            """
            SELECT id, status, kind FROM MonitorRuns
            WHERE status IN ('queued', 'running') LIMIT 1;
            """
        ).fetchone()

        if pending:
            return None, (f"A {pending['kind']} run is already "
                          f"{pending['status']} (#{pending['id']}); scoring will "
                          f"follow it.")

        cursor.execute(
            '''
            INSERT INTO MonitorRuns (trigger, backend, queued_at, status, kind)
            VALUES ('manual', 'none', datetime('now'), 'queued', 'score');
            '''
        )

        return cursor.lastrowid, None


@router.post('/scoring')
def write_scoring(body: RubricBody, request: Request):
    text = (body.body or '').strip()

    if not text:
        return JSONResponse({'detail': 'The rubric cannot be empty.'}, status_code=400)

    if len(text) > MAX_RUBRIC_CHARS:
        return JSONResponse(
            {'detail': f'The rubric is {len(text):,} characters; the limit is '
                       f'{MAX_RUBRIC_CHARS:,}.'},
            status_code=400,
        )

    previous = active_prompt()

    try:
        prompt = set_prompt(text, body.label, user_id_for(request))
    except ScoringError as exc:
        return JSONResponse({'detail': str(exc)}, status_code=400)

    unchanged = previous and previous['id'] == prompt['id']

    if unchanged:
        return {'prompt': prompt, 'run_id': None, 'requeued': 0,
                'detail': 'The rubric is unchanged, so nothing was requeued.'}

    requeued = pending_count(prompt['id'])
    run_id = None
    note = ''

    if body.rescore and SCORING_ENABLED:
        run_id, blocked = queue_scoring_run()
        note = f' {blocked}' if blocked else ' Scoring has been queued.'
    elif body.rescore and not SCORING_ENABLED:
        note = ' Scoring is disabled, so nothing will run until SCORING_ENABLED=true.'

    return {
        'prompt': prompt,
        'run_id': run_id,
        'requeued': requeued,
        'detail': f'Rubric #{prompt["id"]} is now active. {requeued:,} posts need '
                  f'scoring under it.{note}',
    }


@router.post('/scoring/activate')
def rollback(body: ActivateBody):
    """Switch back to an earlier rubric version.

    Cheap by design: that version's scores were never deleted, so rolling back
    restores them rather than re-earning them.
    """
    try:
        prompt = activate_prompt(body.prompt_id)
    except ScoringError as exc:
        return JSONResponse({'detail': str(exc)}, status_code=404)

    return {
        'prompt': prompt,
        'pending': pending_count(prompt['id']),
        'detail': f'Rubric #{prompt["id"]} is active again. Scores already '
                  f'recorded under it were kept.',
    }


@router.post('/scoring/run')
def trigger_scoring():
    if not SCORING_ENABLED:
        return JSONResponse(
            {'detail': 'Scoring is disabled. Set SCORING_ENABLED=true and add '
                       'ANTHROPIC_API_KEY, then restart the worker.'},
            status_code=409,
        )

    run_id, blocked = queue_scoring_run()

    if blocked:
        return JSONResponse({'detail': blocked}, status_code=409)

    return {'run_id': run_id, 'detail': 'Scoring queued.'}
