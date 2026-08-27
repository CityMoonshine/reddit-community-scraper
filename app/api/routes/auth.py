"""Login, logout, and "who am I".

The SPA calls /api/me on boot to decide whether to show the login screen or the
app, so this is the only place the cookie is minted or cleared.
"""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.auth import (create_session, require_session, verify_password)
from app.config import COOKIE_NAME, COOKIE_SECURE, SESSION_LIFETIME
from app.db import connection_scope

router = APIRouter(prefix='/api')


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@router.post('/login')
def login(body: LoginBody, request: Request):
    with connection_scope() as connection:
        user = connection.execute(
            'SELECT * FROM Users WHERE username = ?;', (body.username,)
        ).fetchone()

    # Same generic message either way, so the response doesn't reveal which
    # usernames exist.
    if user is None or not verify_password(body.password, user['password_hash']):
        return JSONResponse({'detail': 'Invalid username or password.'}, status_code=401)

    token = create_session(user['id'], request)

    response = JSONResponse({
        'username': user['username'],
        'agency_name': user['agency_name'],
    })
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,      # JS can't read it
        samesite='lax',
        secure=COOKIE_SECURE,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path='/',
    )
    return response


@router.post('/logout')
def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)

    if token:
        # Expire rather than DELETE. RequestLog rows reference this session, so
        # deleting it would either violate the foreign key or - worse, with the
        # constraint off - orphan the exact request history the detection views
        # exist to replay. load_session() enforces expiry server-side, so a
        # backdated expires_at kills the session just as dead.
        with connection_scope() as connection:
            connection.execute(
                """
                UPDATE Sessions
                SET expires_at = datetime('now', '-1 second')
                WHERE session_token = ?;
                """,
                (token,),
            )

    response = JSONResponse({'ok': True})
    response.delete_cookie(COOKIE_NAME, path='/')
    return response


@router.get('/me')
def me(session=Depends(require_session)):
    return {
        'username': session['username'],
        'agency_name': session['agency_name'],
        'session_id': session['id'],
        'bot_score': session['bot_score'],
        'verdict': session['verdict'],
    }
