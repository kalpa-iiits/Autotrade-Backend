"""Who is calling: a logged-in user (session cookie) or a script with the API token."""

import secrets
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request, WebSocket, status
from starlette.requests import HTTPConnection

from app.auth.models import User
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


def token_is_valid(token: str | None) -> bool:
    # An empty AUTOTRADE_API_TOKEN disables token access entirely.
    return bool(settings.api_token and token) and secrets.compare_digest(token, settings.api_token)


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
) -> str:
    """REST guard: a session cookie, or `Authorization: Bearer <API token>`. Returns the
    username ("api" for token access). Not `?token=`: URLs end up in access logs, proxy logs
    and browser history. (WebSockets still take ?token=, as browsers can't set their headers.)"""
    if user := await session_user(request):
        return user.username
    if authorization and authorization.lower().startswith("bearer ") and token_is_valid(authorization[7:]):
        return "api"
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Log in at /login or send a valid API token")


async def websocket_user(ws: WebSocket) -> str | None:
    if _same_origin(ws) and (user := await session_user(ws)):
        return user.username
    if token_is_valid(ws.query_params.get("token")):
        return "api"
    return None
