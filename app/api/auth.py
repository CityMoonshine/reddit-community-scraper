"""Session handling. Cookie-based, same as before the SPA split.

The SPA is served same-origin through nginx (/ -> static, /api -> this app), so
the httpOnly cookie still works and there is no token for client JS to leak.
"""

import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, Request, status

from app.config import COOKIE_NAME, SESSION_LIFETIME, TRUST_PROXY_HEADERS
from app.db import connection_scope


def hash_password(password):
    if isinstance(password, str):
        password = password.encode()
    return hashlib.sha256(password).hexdigest()


def verify_password(password, stored_hash):
    return secrets.compare_digest(hash_password(password), stored_hash)


def client_ip(request: Request):
    """The real client address, not nginx's.

    Only consults X-Forwarded-For when TRUST_PROXY_HEADERS is on, because the
    header is trivially spoofable by anything that can reach the API directly.
    In compose nginx is the only ingress, so there it is safe.
    """
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get('x-forwarded-for')
        if forwarded:
            # Left-most entry is the original client; the rest are proxies.
            return forwarded.split(',')[0].strip()

        real_ip = request.headers.get('x-real-ip')
        if real_ip:
            return real_ip.strip()

    return request.client.host if request.client else None


def create_session(user_id, request: Request):
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + SESSION_LIFETIME

    with connection_scope() as connection:
        connection.execute(
            '''
            INSERT INTO Sessions (session_token, user_id, ip_address, user_agent, expires_at)
            VALUES (?, ?, ?, ?, ?);
            ''',
            (
                token,
                user_id,
                client_ip(request),
                request.headers.get('user-agent'),
                expires_at.isoformat(),
            ),
        )

    return token


def load_session(request: Request):
    """Resolve the cookie to a Sessions row, or None."""
    token = request.cookies.get(COOKIE_NAME)

    if not token:
        return None

    with connection_scope() as connection:
        row = connection.execute(
            '''
            SELECT s.*, u.username, u.agency_name
            FROM Sessions s
            JOIN Users u ON u.id = s.user_id
            WHERE s.session_token = ?;
            ''',
            (token,),
        ).fetchone()

    if row is None:
        return None

    # Enforced server-side. max_age on the cookie is a hint to a cooperative
    # browser; a scraper will send an expired cookie forever.
    if datetime.fromisoformat(row['expires_at']) < datetime.now(timezone.utc):
        return None

    return row


def session_id_from_request(request: Request):
    """Cheap cookie -> Sessions.id lookup for the request log."""
    token = request.cookies.get(COOKIE_NAME)

    if not token:
        return None

    with connection_scope() as connection:
        row = connection.execute(
            'SELECT id FROM Sessions WHERE session_token = ?;', (token,)
        ).fetchone()

    return row['id'] if row else None


def require_session(request: Request):
    """Dependency for protected routes.

    Returns 401 rather than the old 303-to-login: the SPA decides what to show,
    and a redirect inside a fetch() is not something the client can act on.
    """
    session = load_session(request)

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Not signed in',
        )

    # Stage 3+ hooks in here: check session['bot_score'] against a threshold and
    # decide what to serve. Keep it log-only until the detector is tuned.

    return session
