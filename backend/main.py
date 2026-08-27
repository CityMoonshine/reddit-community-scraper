import hashlib
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from createDb import create_tables

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / "portal.db")
COOKIE_NAME = 'portal_session'
SESSION_LIFETIME = timedelta(hours=8)

app = FastAPI()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

create_tables()


@app.get("/")
async def root():
    return {"message": "Hello World"}


# ---------------------------------------------------------------- request log

# Viewing the log shouldn't write to the log.
UNLOGGED_PREFIXES = ('/debug', '/static', '/favicon.ico')


def session_id_from_request(request):
    """Cheap cookie -> Sessions.id lookup. Returns None for anonymous traffic."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None

    with get_connection() as connection:
        cursor = connection.cursor()
        cursor.execute('SELECT id FROM Sessions WHERE session_token = ?;', (token,))
        row = cursor.fetchone()

    return row['id'] if row else None


def write_request_log(session_id, method, path, status_code, latency_ms,
                      header_order, sec_fetch_mode, sec_ch_ua):
    with get_connection() as connection:
        cursor = connection.cursor()
        cursor.execute(
            '''
            INSERT INTO RequestLog (
                session_id, method, path, status_code, latency_ms,
                header_order, sec_fetch_mode, sec_ch_ua, injected_fault
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            ''',
            (session_id, method, path, status_code, latency_ms,
             header_order, sec_fetch_mode, sec_ch_ua, None),
        )
        connection.commit()


@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.url.path.startswith(UNLOGGED_PREFIXES):
        return await call_next(request)

    started = time.perf_counter()
    response = await call_next(request)
    latency_ms = int((time.perf_counter() - started) * 1000)

    # Header ORDER is the signal, not just presence. Starlette preserves wire
    # order, and real browsers are far more consistent about it than HTTP
    # clients are - this column is doing real work at Stage 3.
    header_order = ','.join(request.headers.keys())

    session_id = await run_in_threadpool(session_id_from_request, request)

    # sqlite is blocking; keep it off the event loop.
    await run_in_threadpool(
        write_request_log,
        session_id,
        request.method,
        request.url.path,
        response.status_code,
        latency_ms,
        header_order,
        request.headers.get('sec-fetch-mode'),
        request.headers.get('sec-ch-ua'),
    )

    return response


# ---------------------------------------------------------------- helpers

def get_connection():
    connection = sqlite3.connect(DB_PATH)
    # Lets you do row['username'] instead of row[3]
    connection.row_factory = sqlite3.Row
    return connection


def hash_password(password):
    # Matches the seeder. Fine for a throwaway target; swap for passlib/bcrypt
    # if you ever want this to resemble real auth.
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password, stored_hash):
    return secrets.compare_digest(hash_password(password), stored_hash)


def create_session(user_id, request):
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + SESSION_LIFETIME

    with get_connection() as connection:
        cursor = connection.cursor()
        cursor.execute(
            '''
            INSERT INTO Sessions (session_token, user_id, ip_address, user_agent, expires_at)
            VALUES (?, ?, ?, ?, ?);
            ''',
            (
                token,
                user_id,
                request.client.host if request.client else None,
                request.headers.get('user-agent'),
                expires_at.isoformat(),
            ),
        )
        connection.commit()

    return token


def load_session(request):
    """Resolve the cookie to a Sessions row, or None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None

    with get_connection() as connection:
        cursor = connection.cursor()
        cursor.execute(
            '''
            SELECT s.*, u.username, u.agency_name
            FROM Sessions s
            JOIN Users u ON u.id = s.user_id
            WHERE s.session_token = ?;
            ''',
            (token,),
        )
        row = cursor.fetchone()

    if row is None:
        return None

    # Enforced server-side. max_age on the cookie is a hint to a cooperative
    # browser; a scraper will send an expired cookie forever.
    if datetime.fromisoformat(row['expires_at']) < datetime.now(timezone.utc):
        return None

    return row


def require_session(request: Request):
    """Dependency for protected routes."""
    session = load_session(request)

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={'Location': '/login'},
        )

    # Stage 3+ hooks in here: check session['bot_score'] against a threshold
    # and decide what to serve. Keep it log-only until the detector is tuned.

    return session


# ---------------------------------------------------------------- routes

@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    with get_connection() as connection:
        cursor = connection.cursor()
        cursor.execute('SELECT * FROM Users WHERE username = ?;', (username,))
        user = cursor.fetchone()

    # Same generic message either way, so the response doesn't reveal
    # which usernames exist.
    if user is None or not verify_password(password, user['password_hash']):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid username or password."},
            status_code=401,
        )

    token = create_session(user['id'], request)

    redirect = RedirectResponse(url="/records", status_code=303)
    redirect.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,      # JS can't read it
        samesite="lax",
        secure=False,       # flip to True once you're behind Cloudflare
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
    )
    return redirect


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)

    if token:
        with get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute('DELETE FROM Sessions WHERE session_token = ?;', (token,))
            connection.commit()

    redirect = RedirectResponse(url="/login", status_code=303)
    redirect.delete_cookie(COOKIE_NAME, path="/")
    return redirect


@app.get("/records")
def records(
    request: Request,
    session=Depends(require_session),
    community: str = "",
    flair: str = "",
    sort: str = "score",
    page: int = 1,
    per_page: int = 25,
):
    # Posts are gated by the watchlist, not by a column on the row itself -
    # an account sees a subreddit's posts only while it watches that subreddit.
    query = '''
        SELECT p.*, c.name AS community_name, c.display_name AS community_display
        FROM Posts p
        JOIN Communities c ON c.id = p.community_id
        JOIN Watchlist w ON w.community_id = c.id
        WHERE w.user_id = ?
    '''
    params = [session['user_id']]

    if community:
        query += ' AND c.name = ?'
        params.append(community)

    if flair:
        query += ' AND p.flair = ?'
        params.append(flair)

    # Whitelisted, because this one is interpolated rather than bound.
    ORDERINGS = {
        'score': 'p.score DESC',
        'comments': 'p.num_comments DESC',
        'new': 'p.created_utc DESC',
    }
    query += f' ORDER BY {ORDERINGS.get(sort, ORDERINGS["score"])} LIMIT ? OFFSET ?;'
    params.extend([per_page, (page - 1) * per_page])

    with get_connection() as connection:
        cursor = connection.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()

        cursor.execute(
            '''
            SELECT c.name, c.display_name, c.subscribers, c.active_users
            FROM Communities c
            JOIN Watchlist w ON w.community_id = c.id
            WHERE w.user_id = ?
            ORDER BY c.name;
            ''',
            (session['user_id'],),
        )
        communities = cursor.fetchall()

        cursor.execute(
            '''
            SELECT DISTINCT p.flair
            FROM Posts p
            JOIN Watchlist w ON w.community_id = p.community_id
            WHERE w.user_id = ? AND p.flair IS NOT NULL AND p.flair != ''
            ORDER BY p.flair;
            ''',
            (session['user_id'],),
        )
        flairs = [r['flair'] for r in cursor.fetchall()]

    return templates.TemplateResponse(
        "records.html",
        {
            "request": request,
            "records": rows,
            "communities": communities,
            "flairs": flairs,
            "username": session['username'],
            "agency_name": session['agency_name'],
            "page": page,
            "community": community,
            "flair": flair,
            "sort": sort,
        },
    )


# ---------------------------------------------------------------- monitoring

# Reddit's own rule for subreddit names. Validated before the name reaches a
# URL or the database, so a bad paste fails here rather than mid-sweep.
SUBREDDIT_NAME = re.compile(r'^[A-Za-z0-9_]{2,21}$')

MONITOR_SORTS = ('new', 'hot', 'top', 'rising')


def monitor_dashboard_context(request, session, notice=None, error=None):
    with get_connection() as connection:
        cursor = connection.cursor()

        # Everything the user watches, with how much it has produced. The
        # 24h count is what tells you a monitor has quietly gone dry.
        cursor.execute(
            """
            SELECT
                c.*,
                (SELECT COUNT(*) FROM Posts p WHERE p.community_id = c.id) AS post_count,
                (SELECT COUNT(*) FROM Posts p
                  WHERE p.community_id = c.id
                    AND p.first_seen_at >= datetime('now', '-1 day')) AS new_24h,
                (SELECT MAX(p.first_seen_at) FROM Posts p WHERE p.community_id = c.id) AS newest_at
            FROM Communities c
            JOIN Watchlist w ON w.community_id = c.id
            WHERE w.user_id = ?
            ORDER BY c.monitor_enabled DESC, c.name;
            """,
            (session['user_id'],),
        )
        communities = cursor.fetchall()

        cursor.execute(
            """
            SELECT r.*,
                   (SELECT COUNT(*) FROM MonitorRunItems i
                     WHERE i.run_id = r.id AND i.status != 'ok') AS failed_items
            FROM MonitorRuns r
            ORDER BY r.id DESC
            LIMIT 10;
            """
        )
        runs = cursor.fetchall()

        cursor.execute("SELECT * FROM MonitorRuns WHERE status = 'running' LIMIT 1;")
        active_run = cursor.fetchone()

        # The actual point of the hourly sweep: what showed up that wasn't
        # there before, newest first, with the details.
        cursor.execute(
            """
            SELECT p.*, c.name AS community_name, c.display_name AS community_display
            FROM Posts p
            JOIN Communities c ON c.id = p.community_id
            JOIN Watchlist w ON w.community_id = c.id
            WHERE w.user_id = ? AND p.first_seen_at IS NOT NULL
            ORDER BY p.first_seen_at DESC, p.score DESC
            LIMIT 25;
            """,
            (session['user_id'],),
        )
        discoveries = cursor.fetchall()

    return {
        "request": request,
        "communities": communities,
        "runs": runs,
        "active_run": active_run,
        "discoveries": discoveries,
        "sorts": MONITOR_SORTS,
        "username": session['username'],
        "agency_name": session['agency_name'],
        "notice": notice,
        "error": error,
    }


@app.get("/monitor")
def monitor_dashboard(request: Request, session=Depends(require_session),
                      notice: str = "", error: str = ""):
    return templates.TemplateResponse(
        "monitor.html",
        monitor_dashboard_context(request, session, notice or None, error or None),
    )


@app.post("/monitor/add")
def monitor_add(
    request: Request,
    session=Depends(require_session),
    name: str = Form(...),
    monitor_sort: str = Form("new"),
    monitor_limit: int = Form(50),
):
    """Add a subreddit to the monitored set and this user's watchlist.

    The row is created empty - the next sweep fills in the metadata and posts.
    That keeps the dashboard responsive instead of blocking an HTTP request on
    a multi-minute browser session.
    """
    # Accept what people actually paste: 'r/python', '/r/python/', a full URL.
    cleaned = name.strip()
    cleaned = re.sub(r'^https?://(www\.|old\.)?reddit\.com', '', cleaned, flags=re.I)
    cleaned = cleaned.strip('/')
    cleaned = re.sub(r'^r/', '', cleaned, flags=re.I)
    cleaned = cleaned.split('/')[0].strip()

    if not SUBREDDIT_NAME.match(cleaned):
        return RedirectResponse(
            url=f"/monitor?error=Not+a+valid+subreddit+name:+{name.strip()[:40]}",
            status_code=303,
        )

    if monitor_sort not in MONITOR_SORTS:
        monitor_sort = 'new'

    monitor_limit = max(10, min(500, monitor_limit))

    with get_connection() as connection:
        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO Communities (name, display_name, monitor_enabled,
                                     monitor_sort, monitor_limit, added_by_user_id)
            VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                monitor_enabled = 1,
                monitor_sort = excluded.monitor_sort,
                monitor_limit = excluded.monitor_limit;
            """,
            (cleaned, 'r/' + cleaned, monitor_sort, monitor_limit, session['user_id']),
        )

        cursor.execute('SELECT id FROM Communities WHERE name = ?;', (cleaned,))
        community_id = cursor.fetchone()['id']

        cursor.execute(
            'INSERT OR IGNORE INTO Watchlist (user_id, community_id) VALUES (?, ?);',
            (session['user_id'], community_id),
        )
        connection.commit()

    return RedirectResponse(
        url=f"/monitor?notice=Monitoring+r/{cleaned}.+It+fills+in+on+the+next+sweep.",
        status_code=303,
    )


@app.post("/monitor/toggle")
def monitor_toggle(request: Request, session=Depends(require_session),
                   community_id: int = Form(...)):
    with get_connection() as connection:
        cursor = connection.cursor()

        # Scoped to the caller's own watchlist, so an id from somewhere else
        # can't be toggled by guessing at it.
        cursor.execute(
            """
            UPDATE Communities
            SET monitor_enabled = CASE WHEN monitor_enabled = 1 THEN 0 ELSE 1 END
            WHERE id = ? AND id IN (
                SELECT community_id FROM Watchlist WHERE user_id = ?
            );
            """,
            (community_id, session['user_id']),
        )
        connection.commit()

    return RedirectResponse(url="/monitor", status_code=303)


@app.post("/monitor/remove")
def monitor_remove(request: Request, session=Depends(require_session),
                   community_id: int = Form(...)):
    """Drop it from this user's watchlist. Posts already collected stay."""
    with get_connection() as connection:
        cursor = connection.cursor()

        cursor.execute(
            'DELETE FROM Watchlist WHERE user_id = ? AND community_id = ?;',
            (session['user_id'], community_id),
        )

        # Nobody watching it means there is nothing to sweep for.
        cursor.execute(
            """
            UPDATE Communities SET monitor_enabled = 0
            WHERE id = ? AND NOT EXISTS (
                SELECT 1 FROM Watchlist WHERE community_id = ?
            );
            """,
            (community_id, community_id),
        )
        connection.commit()

    return RedirectResponse(url="/monitor?notice=Removed+from+your+watchlist.",
                            status_code=303)


@app.post("/monitor/run")
def monitor_run_now(request: Request, session=Depends(require_session),
                    backend: str = Form("browser")):
    """Kick off a sweep out of band.

    Spawned as a detached subprocess rather than run inline: a browser sweep
    takes minutes, and Playwright's sync API refuses to run on a thread that
    already has an event loop. The dashboard picks the run up from MonitorRuns.
    """
    with get_connection() as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT id FROM MonitorRuns WHERE status = 'running' LIMIT 1;")
        if cursor.fetchone():
            return RedirectResponse(
                url="/monitor?error=A+sweep+is+already+running.", status_code=303)

    if backend not in ('browser', 'api'):
        backend = 'browser'

    subprocess.Popen(
        [sys.executable, str(BASE_DIR / 'monitor.py'), '--once',
         '--backend', backend, '--trigger', 'manual'],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return RedirectResponse(
        url="/monitor?notice=Sweep+started.+Refresh+in+a+minute+for+results.",
        status_code=303,
    )



# ---------------------------------------------------------------- debug

@app.get("/debug/sessions")
def debug_sessions(request: Request):
    """Every session, newest first. No auth by design - this is a lab target."""
    with get_connection() as connection:
        cursor = connection.cursor()
        cursor.execute(
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
            LIMIT 200;
            '''
        )
        sessions = cursor.fetchall()

    return templates.TemplateResponse(
        "debug_sessions.html",
        {"request": request, "sessions": sessions},
    )


@app.get("/debug/sessions/{session_id}")
def debug_session_detail(request: Request, session_id: int):
    """Full replay for one session: requests, signals and behaviour on one timeline."""
    with get_connection() as connection:
        cursor = connection.cursor()

        cursor.execute(
            '''
            SELECT s.*, u.username, u.agency_name
            FROM Sessions s
            LEFT JOIN Users u ON u.id = s.user_id
            WHERE s.id = ?;
            ''',
            (session_id,),
        )
        session = cursor.fetchone()

        if session is None:
            raise HTTPException(status_code=404, detail="No such session")

        cursor.execute(
            'SELECT * FROM Fingerprints WHERE session_id = ? ORDER BY collected_at;',
            (session_id,),
        )
        fingerprints = cursor.fetchall()

        cursor.execute(
            'SELECT * FROM RequestLog WHERE session_id = ? ORDER BY requested_at;',
            (session_id,),
        )
        requests_log = cursor.fetchall()

        cursor.execute(
            'SELECT * FROM ScoreEvents WHERE session_id = ? ORDER BY fired_at;',
            (session_id,),
        )
        score_events = cursor.fetchall()

        cursor.execute(
            'SELECT * FROM BehaviorEvents WHERE session_id = ? ORDER BY occurred_at;',
            (session_id,),
        )
        behavior_events = cursor.fetchall()

    # Merge the three streams into one chronological timeline. Doing this in
    # Python rather than a UNION keeps the differing columns readable.
    timeline = []

    for row in requests_log:
        timeline.append({
            "at": row["requested_at"],
            "kind": "request",
            "summary": f"{row['method']} {row['path']} -> {row['status_code']}",
            "detail": f"{row['latency_ms']}ms"
                      + (f" | fault: {row['injected_fault']}" if row["injected_fault"] else "")
                      + (f" | headers: {row['header_order']}" if row["header_order"] else ""),
            "weight": None,
        })

    for row in score_events:
        timeline.append({
            "at": row["fired_at"],
            "kind": "signal",
            "summary": f"{row['signal_name']} ({row['signal_source']})",
            "detail": f"saw {row['observed_value']!r}, expected {row['expected_value']!r}"
                      + (f" | {row['note']}" if row["note"] else ""),
            "weight": row["weight"],
        })

    for row in behavior_events:
        timeline.append({
            "at": row["occurred_at"],
            "kind": "behavior",
            "summary": row["event_type"],
            "detail": f"{row['target_selector'] or ''} "
                      f"({row['pointer_x']},{row['pointer_y']}) "
                      f"+{row['ms_since_previous']}ms".strip(),
            "weight": None,
        })

    timeline.sort(key=lambda e: e["at"] or "")

    running = 0
    for entry in timeline:
        if entry["weight"]:
            running += entry["weight"]
        entry["running_score"] = running

    return templates.TemplateResponse(
        "debug_session_detail.html",
        {
            "request": request,
            "session": session,
            "fingerprints": fingerprints,
            "timeline": timeline,
            "total_score": running,
        },
    )