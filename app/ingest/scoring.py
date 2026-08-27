"""Rank posts with Claude, against a rubric the operator edits on the dashboard.

The rubric is the point
-----------------------
Everything about *what* makes a post good lives in the database, in
ScoringPrompts, and is edited from the monitoring dashboard. This module owns
only *how* the question is asked: it fixes the output contract (a 0-100 number,
a verdict, a one-line rationale, per-dimension subscores) and leaves the
judgement entirely to the operator's text.

That split is what makes the feature useful. Reddit's own score tells you what
the crowd did; a rubric you can rewrite at 3am tells you what *you* care about,
and re-ranks the whole archive against it.

Rubrics are versioned, never edited in place
--------------------------------------------
A score only means something next to the rubric that produced it. If editing
the rubric overwrote the row, every score already on record would silently
start claiming to mean something it never measured. So an edit inserts a new
ScoringPrompts row and deactivates the old one; PostScores rows carry the
prompt_id they were produced under, and posts become "unscored" again under the
new rubric without anything being deleted. Switch the rubric back and the old
scores are still there, still correct.

Cost shape
----------
The rubric is identical for every post in a pass, and it is by far the largest
part of the prompt - so it goes in a cached system block. The first post in a
pass pays for it; the rest read it from cache at roughly a tenth of the price.
This is why the module scores in a pass rather than one post at a time on
demand: it is the cache locality that makes it affordable.

Standalone use:

    python -m app.ingest.scoring --show-rubric
    python -m app.ingest.scoring --pending 20
    python -m app.ingest.scoring --post 1a2b3c --dry-run
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor

from app.config import (ANTHROPIC_API_KEY, SCORING_BATCH_LIMIT,
                        SCORING_CONCURRENCY, SCORING_EFFORT, SCORING_MAX_TOKENS,
                        SCORING_MODEL, SCORING_SELFTEXT_CHARS)
from app.db import connection_scope

# Server-side refusal fallbacks. A public Reddit feed will eventually contain
# something a safety classifier declines to grade; without this a single such
# post would come back as a bare refusal. 'default' routes by refusal category
# so there is no model list here to maintain.
FALLBACK_BETA = 'server-side-fallback-2026-07-01'

# What the operator's rubric is NOT allowed to change. Kept separate from the
# rubric so that "rewrite how posts are judged" can never become "rewrite what
# the API returns" and break every consumer of the score.
OUTPUT_CONTRACT = """\
You are ranking Reddit posts for a monitoring dashboard. The operator's rubric
follows; it is the sole authority on what counts as good. Apply it as written,
including where it disagrees with your own instincts about quality.

For each post return:
  score      - 0-100 under the rubric's scale
  verdict    - two or three words, lowercase, e.g. "worth reading", "engagement
               bait", "thin but honest"
  rationale  - one sentence, under 25 words, naming the specific thing in THIS
               post that drove the score. No hedging, no restating the title.
  dimensions - the 2-5 axes the rubric actually cares about, each 0-10. Derive
               the names from the rubric rather than using a fixed set.

Judge only what you are shown. A truncated body is marked as such - score what
is there and let the rationale say the body was clipped, rather than penalising
the post for the truncation.
"""


class ScoringError(RuntimeError):
    pass


class ScoringUnavailable(ScoringError):
    """No credentials, or the SDK is not installed."""


# --------------------------------------------------------------- the rubric

def active_prompt():
    """The rubric in force, as a dict. None if the table was never seeded."""
    with connection_scope() as connection:
        row = connection.execute(
            '''
            SELECT * FROM ScoringPrompts
            WHERE is_active = 1 ORDER BY id DESC LIMIT 1;
            '''
        ).fetchone()

    return dict(row) if row else None


def set_prompt(body, label=None, user_id=None):
    """Publish a new rubric version and make it active. Returns the new row.

    Deliberately an insert. See the module docstring: editing in place would
    retroactively change what every existing score claims to mean.
    """
    body = (body or '').strip()

    if not body:
        raise ScoringError('The rubric cannot be empty.')

    with connection_scope() as connection:
        cursor = connection.cursor()

        current = cursor.execute(
            'SELECT id, body FROM ScoringPrompts WHERE is_active = 1;'
        ).fetchone()

        # An unchanged save is a no-op rather than a new version, so clicking
        # Save twice does not orphan every score in the database.
        if current and current['body'].strip() == body:
            return dict(cursor.execute(
                'SELECT * FROM ScoringPrompts WHERE id = ?;', (current['id'],)
            ).fetchone())

        cursor.execute('UPDATE ScoringPrompts SET is_active = 0;')
        cursor.execute(
            '''
            INSERT INTO ScoringPrompts (body, label, is_active, created_by_user_id)
            VALUES (?, ?, 1, ?);
            ''',
            (body, label, user_id),
        )

        return dict(cursor.execute(
            'SELECT * FROM ScoringPrompts WHERE id = ?;', (cursor.lastrowid,)
        ).fetchone())


def prompt_history(limit=20):
    with connection_scope() as connection:
        rows = connection.execute(
            '''
            SELECT p.*,
                   (SELECT COUNT(*) FROM PostScores s WHERE s.prompt_id = p.id)
                       AS scored_count
            FROM ScoringPrompts p
            ORDER BY p.id DESC LIMIT ?;
            ''',
            (limit,),
        ).fetchall()

    return [dict(row) for row in rows]


def activate_prompt(prompt_id):
    """Roll back to an earlier rubric. Its old scores become current again."""
    with connection_scope() as connection:
        cursor = connection.cursor()

        if not cursor.execute(
            'SELECT 1 FROM ScoringPrompts WHERE id = ?;', (prompt_id,)
        ).fetchone():
            raise ScoringError(f'No rubric version #{prompt_id}.')

        cursor.execute('UPDATE ScoringPrompts SET is_active = 0;')
        cursor.execute(
            'UPDATE ScoringPrompts SET is_active = 1 WHERE id = ?;', (prompt_id,)
        )

    return active_prompt()


# ---------------------------------------------------------------- the queue

def pending_posts(prompt_id, limit):
    """Posts with no score under this rubric, newest discoveries first.

    Newest-first because a dashboard is read at the top: the posts an operator
    is about to look at should be the ones that get scored first when a pass
    cannot drain the whole backlog.
    """
    with connection_scope() as connection:
        rows = connection.execute(
            '''
            SELECT p.*, c.name AS community_name
            FROM Posts p
            JOIN Communities c ON c.id = p.community_id
            LEFT JOIN PostScores s
                   ON s.post_id = p.id AND s.prompt_id = ?
            WHERE s.id IS NULL
            ORDER BY p.first_seen_at DESC, p.id DESC
            LIMIT ?;
            ''',
            (prompt_id, limit),
        ).fetchall()

    return [dict(row) for row in rows]


def pending_count(prompt_id):
    with connection_scope() as connection:
        return connection.execute(
            '''
            SELECT COUNT(*) FROM Posts p
            LEFT JOIN PostScores s ON s.post_id = p.id AND s.prompt_id = ?
            WHERE s.id IS NULL;
            ''',
            (prompt_id,),
        ).fetchone()[0]


def record_score(post_id, prompt_id, result):
    """Upsert one verdict. Failures are recorded too, not swallowed.

    A post that could not be scored has to leave a row behind, or the next pass
    picks it up again, fails the same way, and the queue never drains while
    quietly spending money on every cycle.
    """
    with connection_scope() as connection:
        connection.execute(
            '''
            INSERT INTO PostScores (
                post_id, prompt_id, score, verdict, rationale, dimensions,
                model, input_tokens, output_tokens, cached_tokens, status,
                error, scored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(post_id, prompt_id) DO UPDATE SET
                score = excluded.score,
                verdict = excluded.verdict,
                rationale = excluded.rationale,
                dimensions = excluded.dimensions,
                model = excluded.model,
                input_tokens = excluded.input_tokens,
                output_tokens = excluded.output_tokens,
                cached_tokens = excluded.cached_tokens,
                status = excluded.status,
                error = excluded.error,
                scored_at = CURRENT_TIMESTAMP;
            ''',
            (
                post_id, prompt_id, result.get('score'), result.get('verdict'),
                result.get('rationale'),
                json.dumps(result['dimensions']) if result.get('dimensions') else None,
                result.get('model'), result.get('input_tokens'),
                result.get('output_tokens'), result.get('cached_tokens'),
                result.get('status', 'ok'), result.get('error'),
            ),
        )


# ------------------------------------------------------------------- claude

def build_client():
    if not ANTHROPIC_API_KEY:
        raise ScoringUnavailable(
            'SCORING_ENABLED is on but ANTHROPIC_API_KEY is empty. Put a key '
            'from console.anthropic.com in .env and restart the worker.'
        )

    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - image build would catch it
        raise ScoringUnavailable(
            'The anthropic package is not installed in this image. Rebuild: '
            'docker compose build worker'
        ) from exc

    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def response_model():
    """The structured-output schema, as a pydantic model.

    Built here rather than at import time so that importing this module never
    requires pydantic - the API container reads and writes rubrics without ever
    calling Claude, and should not need the scoring dependencies to do it.
    """
    from typing import List

    from pydantic import BaseModel, Field

    class Dimension(BaseModel):
        name: str = Field(description='Axis name, taken from the rubric')
        score: int = Field(ge=0, le=10)

    class Verdict(BaseModel):
        score: int = Field(ge=0, le=100)
        verdict: str
        rationale: str
        dimensions: List[Dimension]

    return Verdict


def render_post(post):
    """One post as the text Claude judges.

    Explicitly labelled fields rather than prose: the rubric may key off any of
    them ("punish undisclosed promotion" needs the domain; "reward first-hand
    experience" needs the body), and a paragraph would bury them.
    """
    body = post.get('selftext') or ''
    stored_chars = len(body)
    true_chars = post.get('selftext_chars') or stored_chars

    if len(body) > SCORING_SELFTEXT_CHARS:
        body = body[:SCORING_SELFTEXT_CHARS]

    lines = [
        f"subreddit: r/{post.get('community_name')}",
        f"title: {post.get('title')}",
        f"author: u/{post.get('author')}",
    ]

    # A self post's `url` is its own permalink; printing it as a "link url"
    # would tell the model this is a link post pointing at itself.
    link = post.get('url')
    if post.get('is_self') or (link and link == post.get('permalink')):
        link = None

    optional = [
        ('flair', post.get('flair')),
        ('author flair', post.get('author_flair')),
        ('link domain', post.get('domain')),
        ('link url', link),
        ('post type', post.get('post_hint')),
        ('posted at', post.get('created_utc')),
        ('reddit score', post.get('score')),
        ('upvote ratio', post.get('upvote_ratio')),
        ('comments', post.get('num_comments')),
        ('awards', post.get('total_awards')),
        ('crossposted from', post.get('crosspost_origin')),
        ('edited at', post.get('edited_utc')),
    ]

    for label, value in optional:
        if value not in (None, '', 0):
            lines.append(f'{label}: {value}')

    for label, flag in (('nsfw', 'over18'), ('spoiler', 'spoiler'),
                        ('locked', 'locked'), ('stickied', 'stickied')):
        if post.get(flag):
            lines.append(f'{label}: yes')

    if post.get('distinguished'):
        lines.append(f"distinguished: {post['distinguished']}")

    if body:
        # Say so explicitly - the contract tells Claude not to punish a post
        # for a truncation that is our doing rather than the author's.
        if len(body) < true_chars:
            lines.append(
                f'body (first {len(body)} of {true_chars} characters, truncated):'
            )
        else:
            lines.append('body:')
        lines.append(body)
    elif post.get('is_self'):
        lines.append('body: (empty)')
    else:
        lines.append('body: (link post, no text)')

    return '\n'.join(lines)


def score_post(client, post, rubric, model=None):
    """One post -> a result dict. Never raises for a per-post failure.

    A post that cannot be scored must not take the pass down with it: one
    malformed body among fifty should cost one row, not the other forty-nine.
    """
    model = model or SCORING_MODEL

    try:
        response = client.beta.messages.parse(
            model=model,
            max_tokens=SCORING_MAX_TOKENS,
            betas=[FALLBACK_BETA],
            fallbacks='default',
            output_config={'effort': SCORING_EFFORT},
            # The contract and the rubric are byte-identical across every post
            # in a pass, so caching this prefix is what makes the pass cheap.
            system=[
                {'type': 'text', 'text': OUTPUT_CONTRACT},
                {
                    'type': 'text',
                    'text': f'--- OPERATOR RUBRIC ---\n{rubric}',
                    'cache_control': {'type': 'ephemeral'},
                },
            ],
            messages=[{'role': 'user', 'content': render_post(post)}],
            output_format=response_model(),
        )
    except Exception as exc:  # noqa: BLE001 - one bad post must not stop the pass
        return {
            'status': 'error',
            'error': f'{type(exc).__name__}: {exc}'[:400],
            'model': model,
        }

    usage = getattr(response, 'usage', None)

    tokens = {
        'model': model,
        'input_tokens': getattr(usage, 'input_tokens', None),
        'output_tokens': getattr(usage, 'output_tokens', None),
        'cached_tokens': getattr(usage, 'cache_read_input_tokens', None),
    }

    # A refusal is a 200 with no parsed output, so it has to be checked before
    # touching .parsed_output rather than caught as an exception.
    if getattr(response, 'stop_reason', None) == 'refusal':
        details = getattr(response, 'stop_details', None)
        category = getattr(details, 'category', None) or 'unspecified'
        return {
            **tokens,
            'status': 'refused',
            'error': f'declined to score ({category})',
        }

    parsed = getattr(response, 'parsed_output', None)

    if parsed is None:
        return {**tokens, 'status': 'error',
                'error': f'no parsed output (stop_reason='
                         f'{getattr(response, "stop_reason", "?")})'}

    return {
        **tokens,
        'status': 'ok',
        'score': parsed.score,
        'verdict': parsed.verdict,
        'rationale': parsed.rationale,
        'dimensions': [{'name': d.name, 'score': d.score} for d in parsed.dimensions],
    }


def score_pending(limit=None, on_progress=None, dry_run=False):
    """Score one pass of the backlog. Returns a summary dict.

    Concurrency is bounded and small: the rubric cache is written by the first
    request, so firing fifty at once would have fifty of them miss the cache and
    each pay full price for the rubric.
    """
    limit = limit or SCORING_BATCH_LIMIT
    prompt = active_prompt()

    if prompt is None:
        raise ScoringError(
            'No scoring rubric exists. The api container seeds one at startup - '
            'is it running current code?'
        )

    posts = pending_posts(prompt['id'], limit)
    remaining = pending_count(prompt['id'])

    if not posts:
        return {'scored': 0, 'failed': 0, 'remaining': 0, 'prompt_id': prompt['id']}

    def report(text):
        print(f'  {text}', flush=True)
        if on_progress:
            on_progress(text)

    report(f'scoring {len(posts)} of {remaining} unscored posts '
           f'(rubric #{prompt["id"]})')

    if dry_run:
        print('\n--- prompt for the first post ---\n', flush=True)
        print(render_post(posts[0]), flush=True)
        return {'scored': 0, 'failed': 0, 'remaining': remaining,
                'prompt_id': prompt['id'], 'dry_run': True}

    client = build_client()

    # The first request alone, to write the rubric into the cache. Without this
    # the whole first wave races and every one of them is a cache miss.
    results = [(posts[0], score_post(client, posts[0], prompt['body']))]

    if len(posts) > 1:
        with ThreadPoolExecutor(max_workers=max(1, SCORING_CONCURRENCY)) as pool:
            futures = [
                (post, pool.submit(score_post, client, post, prompt['body']))
                for post in posts[1:]
            ]
            results.extend((post, future.result()) for post, future in futures)

    scored = failed = cached = 0

    for post, result in results:
        record_score(post['id'], prompt['id'], result)

        if result.get('status') == 'ok':
            scored += 1
            report(f"[{result['score']:>3}] {post['title'][:60]} "
                   f"- {result['verdict']}")
        else:
            failed += 1
            report(f"[ ! ] {post['title'][:60]} - {result.get('error')}")

        cached += result.get('cached_tokens') or 0

    report(f'{scored} scored, {failed} failed, {remaining - len(posts)} still queued'
           + (f' ({cached:,} rubric tokens served from cache)' if cached else ''))

    return {
        'scored': scored,
        'failed': failed,
        'remaining': max(0, remaining - len(posts)),
        'prompt_id': prompt['id'],
        'cached_tokens': cached,
    }


# ------------------------------------------------------------------- cli

def _cmd_show_rubric():
    prompt = active_prompt()

    if prompt is None:
        print('No rubric exists yet.', flush=True)
        return 1

    print(f"rubric #{prompt['id']} ({prompt.get('label') or 'unlabelled'}), "
          f"created {prompt['created_at']}\n", flush=True)
    print(prompt['body'], flush=True)
    return 0


def _cmd_one(post_ref, dry_run):
    prompt = active_prompt()

    with connection_scope() as connection:
        row = connection.execute(
            '''
            SELECT p.*, c.name AS community_name
            FROM Posts p JOIN Communities c ON c.id = p.community_id
            WHERE p.post_id = ? OR p.id = ?;
            ''',
            (post_ref, post_ref if str(post_ref).isdigit() else -1),
        ).fetchone()

    if row is None:
        print(f'No post matching {post_ref!r}.', file=sys.stderr)
        return 1

    post = dict(row)

    if dry_run:
        print(render_post(post), flush=True)
        return 0

    result = score_post(build_client(), post, prompt['body'])
    record_score(post['id'], prompt['id'], result)

    print(json.dumps(result, indent=2), flush=True)
    return 0 if result.get('status') == 'ok' else 1


def main():
    parser = argparse.ArgumentParser(
        description='Rank posts with Claude against the dashboard rubric.'
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--pending', type=int, nargs='?', const=0, metavar='N',
                       help='Score up to N unscored posts (default: the '
                            'configured batch limit).')
    group.add_argument('--post', metavar='ID',
                       help="Score one post by Reddit id or row id.")
    group.add_argument('--show-rubric', action='store_true')
    parser.add_argument('--dry-run', action='store_true',
                        help='Render the prompt without calling the API.')
    args = parser.parse_args()

    try:
        if args.show_rubric:
            return _cmd_show_rubric()

        if args.post:
            return _cmd_one(args.post, args.dry_run)

        summary = score_pending(args.pending or None, dry_run=args.dry_run)
        return 0 if not summary['failed'] else 1
    except ScoringError as exc:
        print(f'\n{exc}\n', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
