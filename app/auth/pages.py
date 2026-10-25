"""App login: the /login page, /logout, and the guard for browser pages."""

import html
from pathlib import Path
from string import Template
from urllib.parse import quote

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth.dependencies import session_user
from app.auth.users import authenticate, count_users, password_fingerprint
from app.auth.models import User

router = APIRouter(tags=["auth"], include_in_schema=False)  # HTML, for the test page
LOGIN_TEMPLATE = Template((Path(__file__).parent / "templates" / "login.html").read_text())


def safe_next(target: str | None) -> str:
    """Only allow local paths, so ?next= can't bounce a user to another site."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target)}", status.HTTP_303_SEE_OTHER)


def _page(next_: str, error: str = "", username: str = "", disabled: bool = False,
          status_code: int = 200, headers: dict | None = None) -> HTMLResponse:
    body = LOGIN_TEMPLATE.substitute(
        next=html.escape(next_),
        username=html.escape(username),
        error=f'<div class="error" role="alert">{html.escape(error)}</div>' if error else "",
        disabled="disabled" if disabled else "",
    )
    return HTMLResponse(body, status_code=status_code, headers=headers)


NOT_CONFIGURED = "No users yet. Create one with: python -m app.auth.users create <username>"


class SignInFailed(Exception):
    def __init__(self, message: str, status_code: int, retry_after: int | None = None):
        super().__init__(message)
        self.message, self.status_code, self.retry_after = message, status_code, retry_after


async def sign_in(request: Request, username: str, password: str) -> User:
    """Check credentials (with the failed-login lockout) and start a session. Shared by the
    HTML login page and POST /api/auth/login."""
    if not await count_users():
        raise SignInFailed(NOT_CONFIGURED, 503)
    throttle = request.app.state.login_throttle
    client = request.client.host if request.client else "unknown"
    if wait := await throttle.retry_after(client):
        raise SignInFailed(f"Too many failed attempts. Try again in {wait} seconds.", 429, wait)
    user = await authenticate(username, password)
    await throttle.record(client, username, success=user is not None)
    if user is None:
        raise SignInFailed("Invalid username or password.", 401)
    request.session.clear()
    request.session.update({"uid": user.id, "pw": password_fingerprint(user)})
    return user


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    next_ = safe_next(next)
    if await session_user(request):
        return RedirectResponse(next_, status.HTTP_303_SEE_OTHER)
    if not await count_users():
        return _page(next_, NOT_CONFIGURED, disabled=True)
    return _page(next_)


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    next_ = safe_next(next)
    try:
        await sign_in(request, username, password)
    except SignInFailed as e:
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else None
        return _page(next_, e.message, username=username, disabled=e.status_code == 503,
                     status_code=e.status_code, headers=headers)
    return RedirectResponse(next_, status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
