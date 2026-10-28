"""JSON sign-in for a separate frontend (React etc.), mounted under /api/auth.

Same session cookie, lockout and checks as the HTML /login page. The browser keeps the cookie;
call these with `credentials: "include"` from another origin listed in AUTOTRADE_CORS_ORIGINS.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.auth.dependencies import require_user, session_user
from app.auth.models import User
from app.auth.pages import SignInFailed, sign_in
from app.auth.tokens import create_token, list_tokens, revoke_token, token_dict
from app.auth.users import (
    UserError, check_password, create_user, get_user, normalize_email, password_fingerprint, set_password,
)
from app.core.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    # Sign-up takes an email here; sign-in takes the email, or an older account's plain username.
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class Me(BaseModel):
    username: str
    last_login_at: str | None


async def signed_in_user(request: Request) -> User:
    """Dependency: the session's user, or 401 (checked before the request body)."""
    user = await session_user(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    return user


def _me(user) -> Me:
    return Me(username=user.username,
              last_login_at=user.last_login_at.isoformat() if user.last_login_at else None)


@router.post("/login", response_model=Me)
async def login(request: Request, body: Credentials, response: Response):
    """Sign in. Sets the session cookie. 401 wrong credentials, 429 locked out (Retry-After)."""
    try:
        user = await sign_in(request, body.username, body.password)
    except SignInFailed as e:
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else None
        raise HTTPException(e.status_code, e.message, headers=headers)
    response.headers["Cache-Control"] = "no-store"
    return _me(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request):
    request.session.clear()


@router.get("/me", response_model=Me)
async def me(user: User = Depends(signed_in_user)):
    """The signed-in user, or 401. Use it on app load to decide whether to show the login form."""
    return _me(user)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(request: Request, body: PasswordChange,
                          user: User = Depends(signed_in_user)):
    """Change your own password. Other sessions are signed out; this one stays signed in."""
    if not await check_password(user.username, body.current_password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is wrong")
    try:
        await set_password(user.username, body.new_password)
    except UserError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    request.session["pw"] = password_fingerprint(await get_user(user.id))


@router.post("/signup", response_model=Me, status_code=status.HTTP_201_CREATED)
async def signup(request: Request, body: Credentials, response: Response):
    """Create your own user, with your email as its username, and sign in. Only when
    AUTOTRADE_ALLOW_SIGNUP=true (404 otherwise). Shares the failed-login lockout, so it can't be
    used to hammer the server."""
    if not settings.allow_signup:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sign-up is disabled on this server")
    throttle = request.app.state.login_throttle
    client = request.client.host if request.client else "unknown"
    if wait := await throttle.retry_after(client):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Too many attempts. Try again in {wait} seconds.",
                            headers={"Retry-After": str(wait)})
    try:
        email = normalize_email(body.username)
        await create_user(email, body.password)
    except UserError as e:
        await throttle.record(client, body.username, success=False)
        message = "An account with this email already exists." if "already exists" in str(e) else str(e)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, message)
    user = await sign_in(request, email, body.password)
    response.headers["Cache-Control"] = "no-store"
    return _me(user)


# ---------- personal API tokens (for scripts and bots) ----------

class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


@router.get("/tokens")
async def tokens(user: User = Depends(require_user)):
    return [token_dict(t) for t in await list_tokens(user.id)]


@router.post("/tokens", status_code=status.HTTP_201_CREATED)
async def new_token(body: TokenCreate, response: Response, user: User = Depends(require_user)):
    """The token is in this response only; it is stored hashed and can't be shown again.
    It acts as you: send it as Authorization: Bearer <token> (or ?token= on WebSockets)."""
    row, token = await create_token(user.id, body.name)
    response.headers["Cache-Control"] = "no-store"
    return token_dict(row) | {"token": token}


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_token(token_id: int, user: User = Depends(require_user)):
    if not await revoke_token(user.id, token_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such token")
