"""Who is calling: a signed-in user (session cookie) or a script with one of a user's API
tokens. Either way the result is a User, and everything they can see is scoped to them."""

from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request, WebSocket, status
from starlette.requests import HTTPConnection

from app.auth.models import User
from app.auth.tokens import user_for_token
from app.auth.users import get_user, password_fingerprint
from app.core.config import settings


async def session_user(conn: HTTPConnection) -> User | None:
    if "session" not in conn.scope or "uid" not in conn.session:
        return None
    user = await get_user(conn.session["uid"])
    # Disabled users and sessions from before a password change are no longer valid.
    if user is None or not user.is_active or conn.session.get("pw") != password_fingerprint(user):
        return None
    return user


def _same_origin(conn: HTTPConnection) -> bool:
    """Browsers always send Origin on WebSockets; reject pages from other sites using our cookie.
    Our own origin and the frontend origins in AUTOTRADE_CORS_ORIGINS are allowed."""
    origin = conn.headers.get("origin")
    if origin is None or urlparse(origin).netloc == conn.headers.get("host"):
        return True
    return origin.rstrip("/") in {o.rstrip("/") for o in settings.cors_origins}


async def require_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> User:
    """REST guard: a session cookie, or `Authorization: Bearer <API token>`. Not `?token=`:
    URLs end up in access logs, proxy logs and browser history. (WebSockets still take
    ?token=, as browsers can't set their headers.)"""
    if user := await session_user(request):
        return user
    if authorization and authorization.lower().startswith("bearer "):
        if user := await user_for_token(authorization[7:]):
            return user
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Log in at /login or send a valid API token")


async def websocket_user(ws: WebSocket) -> User | None:
    if _same_origin(ws) and (user := await session_user(ws)):
        return user
    return await user_for_token(ws.query_params.get("token"))
