"""Authentication app: broker connection status and the daily broker login flows.

Flow (same shape for Fyers and Zerodha): while logged in to this app, open
/api/auth/<broker>/login in a browser (or GET it with ?redirect=false to get the URL as JSON)
-> broker's login page -> broker redirects to the callback -> we exchange the code for an
access token, save it for the day, and reconnect the broker if it is the active one. With
AUTOTRADE_FRONTEND_URL set, the callback then sends the browser back to the frontend with
?broker_login=<broker> (or ?broker_login_error=<message>).

The callbacks are served at /auth/<broker>/callback (the URL registered with the broker) and
also under /api.
"""

import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fyers_apiv3.fyersModel import SessionModel
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

from app.auth.dependencies import require_user
from app.core.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])

LOGIN_STATE_TTL = 600  # seconds a started login stays valid
BROKERS = ("fyers", "zerodha")
SUCCESS_PAGE = "<h3>{} login successful.</h3><p>You can close this tab.</p>"


# ---------- shared login plumbing ----------

def _start_login(request: Request, broker: str) -> str:
    """Random state ties the callback to a login we started (CSRF protection)."""
    pending: dict[str, tuple[str, float]] = request.app.state.login_states
    now = time.monotonic()
    for s, (_, expires) in list(pending.items()):
        if expires < now:
            del pending[s]
    state = secrets.token_urlsafe(16)
    pending[state] = (broker, now + LOGIN_STATE_TTL)
    return state


def _check_state(request: Request, broker: str, state: str) -> None:
    entry = request.app.state.login_states.pop(state, None)
    if entry is None or entry[0] != broker or entry[1] < time.monotonic():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown or expired login; start again")


async def _finish_login(request: Request, broker: str, access_token: str) -> None:
    await request.app.state.token_stores[broker].save(access_token)
    if request.app.state.brokers.name == broker:
        await request.app.state.brokers.restart()


def _login_response(url: str, redirect: bool) -> Response | dict:
    return RedirectResponse(url) if redirect else {"url": url}


def _callback_result(broker: str, error: str | None = None) -> Response:
    """Where the browser lands after the broker login: the frontend if configured, else a page."""
    if settings.frontend_url:
        query = urlencode({"broker_login_error": error} if error else {"broker_login": broker})
        sep = "&" if "?" in settings.frontend_url else "?"
        return RedirectResponse(f"{settings.frontend_url}{sep}{query}", status.HTTP_303_SEE_OTHER)
    if error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, error)
    return HTMLResponse(SUCCESS_PAGE.format(broker.capitalize()))


def _require(*values: str, message: str) -> None:
    if not all(values):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, message)


@router.get("/status", dependencies=[Depends(require_user)])
async def auth_status(request: Request):
    brokers = request.app.state.brokers
    return {
        "broker": brokers.name,
        "connected": brokers.connected,
        "reason": None if brokers.connected else brokers.not_connected_reason,
        "logged_in_today": {
            b: await request.app.state.token_stores[b].load() is not None for b in BROKERS
        },
    }


@router.post("/{broker}/logout", dependencies=[Depends(require_user)])
async def logout(request: Request, broker: str):
    if broker not in BROKERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown broker {broker!r}")
    await request.app.state.token_stores[broker].clear()
    if request.app.state.brokers.name == broker:
        await request.app.state.brokers.stop(f"Logged out of {broker}")
    return {"connected": request.app.state.brokers.connected}


# ---------- Fyers ----------

def _fyers_session(state: str | None = None) -> SessionModel:
    return SessionModel(
        client_id=settings.fyers_client_id,
        secret_key=settings.fyers_secret_key,
        redirect_uri=settings.fyers_redirect_uri,
        response_type="code",
        grant_type="authorization_code",
        state=state,
    )


@router.get("/fyers/login", dependencies=[Depends(require_user)])
async def fyers_login(request: Request, redirect: bool = True):
    """Start the Fyers login. Default: redirect the browser. ?redirect=false: {"url": ...}."""
    _require(settings.fyers_client_id, settings.fyers_secret_key,
             message="Set AUTOTRADE_FYERS_CLIENT_ID and AUTOTRADE_FYERS_SECRET_KEY first")
    state = _start_login(request, "fyers")
    return _login_response(_fyers_session(state).generate_authcode(), redirect)


@router.get("/fyers/callback", response_class=HTMLResponse)
async def fyers_callback(request: Request, auth_code: str = "", state: str = ""):
    try:
        _check_state(request, "fyers", state)
        _require(auth_code, message="Fyers did not return an auth code")
        session = _fyers_session()
        session.set_token(auth_code)
        resp = session.generate_token()
        access_token = resp.get("access_token")
        if not access_token:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                                f"Fyers login failed: {resp.get('message', resp)}")
    except HTTPException as e:
        if not settings.frontend_url:
            raise
        return _callback_result("fyers", str(e.detail))

    await _finish_login(request, "fyers", access_token)
    return _callback_result("fyers")


# ---------- Zerodha ----------
# The redirect URL is set on the Kite developer console (not sent with the login), so
# register http://127.0.0.1:8000/auth/zerodha/callback there.

@router.get("/zerodha/login", dependencies=[Depends(require_user)])
async def zerodha_login(request: Request, redirect: bool = True):
    """Start the Zerodha login. Default: redirect the browser. ?redirect=false: {"url": ...}."""
    _require(settings.zerodha_api_key, settings.zerodha_api_secret,
             message="Set AUTOTRADE_ZERODHA_API_KEY and AUTOTRADE_ZERODHA_API_SECRET first")
    state = _start_login(request, "zerodha")
    # Kite echoes redirect_params back as query params on the callback.
    login_url = KiteConnect(api_key=settings.zerodha_api_key).login_url()
    return _login_response(
        f"{login_url}&{urlencode({'redirect_params': urlencode({'state': state})})}", redirect
    )


@router.get("/zerodha/callback", response_class=HTMLResponse)
async def zerodha_callback(request: Request, request_token: str = "", state: str = ""):
    try:
        _check_state(request, "zerodha", state)
        login_status = request.query_params.get("status", "")
        _require(request_token, message="Zerodha did not return a request token")
        if login_status and login_status != "success":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Zerodha login status: {login_status}")
        kite = KiteConnect(api_key=settings.zerodha_api_key)
        try:
            session = kite.generate_session(request_token, api_secret=settings.zerodha_api_secret)
        except KiteException as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Zerodha login failed: {e}")
    except HTTPException as e:
        if not settings.frontend_url:
            raise
        return _callback_result("zerodha", str(e.detail))

    await _finish_login(request, "zerodha", session["access_token"])
    return _callback_result("zerodha")


# The redirect URLs registered with the brokers (no /api prefix). Kept so existing broker app
# settings keep working.
callback_router = APIRouter(include_in_schema=False)
callback_router.add_api_route("/auth/fyers/callback", fyers_callback, methods=["GET"])
callback_router.add_api_route("/auth/zerodha/callback", zerodha_callback, methods=["GET"])
