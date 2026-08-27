"""The JSON API. No templates - the SPA in web/ renders everything.

A note on the detection instrumentation, because the SPA split changed what it
sees. RequestLog was written against document navigations: `sec-fetch-mode` was
'navigate' and the header order was a browser's navigation header order. Now
the browser makes fetch() calls, so the same columns record 'cors'/'same-origin'
and fetch's header order instead, and nginx normalises some of it on the way
through. That is not a regression - it is the signal moving to where the data
now lives. The JSON API is the thing a scraper would target, so it is the thing
worth fingerprinting. Just don't compare rows written before and after the split
as if they measured the same thing.
"""

import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.auth import session_id_from_request
from app.api.routes import auth as auth_routes
from app.api.routes import communities as community_routes
from app.api.routes import debug as debug_routes
from app.api.routes import monitor as monitor_routes
from app.api.routes import posts as post_routes
from app.db import connection_scope
from app.schema import init_db

app = FastAPI(title='Scraping Defense API', docs_url='/api/docs', openapi_url='/api/openapi.json')

# The api container owns schema creation; the worker waits for it.
init_db()

# Viewing the log shouldn't write to the log.
UNLOGGED_PREFIXES = ('/api/debug', '/api/health')


def write_request_log(session_id, method, path, status_code, latency_ms,
                      header_order, sec_fetch_mode, sec_ch_ua):
    with connection_scope() as connection:
        connection.execute(
            '''
            INSERT INTO RequestLog (
                session_id, method, path, status_code, latency_ms,
                header_order, sec_fetch_mode, sec_ch_ua, injected_fault
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            ''',
            (session_id, method, path, status_code, latency_ms,
             header_order, sec_fetch_mode, sec_ch_ua, None),
        )


@app.middleware('http')
async def log_requests(request: Request, call_next):
    if request.url.path.startswith(UNLOGGED_PREFIXES):
        return await call_next(request)

    started = time.perf_counter()
    response = await call_next(request)
    latency_ms = int((time.perf_counter() - started) * 1000)

    # Header ORDER is the signal, not just presence. Starlette preserves wire
    # order. Behind nginx this is the order nginx forwarded, which is close to
    # but not identical to what the client sent - see the module docstring.
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


@app.get('/api/health')
def health():
    """Compose healthcheck. Touches the DB so a broken volume mount shows up."""
    try:
        with connection_scope() as connection:
            connection.execute('SELECT 1 FROM Users LIMIT 1;').fetchone()
    except Exception as exc:
        return JSONResponse({'status': 'error', 'detail': str(exc)}, status_code=503)

    return {'status': 'ok'}


app.include_router(auth_routes.router)
app.include_router(post_routes.router)
app.include_router(community_routes.router)
app.include_router(monitor_routes.router)
app.include_router(debug_routes.router)
