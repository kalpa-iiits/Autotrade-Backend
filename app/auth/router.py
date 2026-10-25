"""Daily broker login, per broker account.

Flow (same shape for Fyers and Zerodha): the account's owner opens
/api/accounts/<id>/broker-login (or GETs it with ?redirect=false for the URL as JSON) -> the
broker's login page for *that account's* app -> the broker redirects to our callback -> we
exchange the code using that account's (decrypted) app secret, store the access token
encrypted for the day, and reconnect the account. With AUTOTRADE_FRONTEND_URL set, the callback
then sends the browser back to the frontend with ?broker_login=<broker>&account_id=<id> (or
?broker_login_error=<message>).

One callback URL per broker for every account (register it on your Fyers app / Kite console):
/auth/<broker>/callback (also served under /api). The random `state` says which account a
login is for, and only the account it was started for can complete it.
"""

import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fyers_apiv3.fyersModel import SessionModel
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

from app.accounts import store
from app.accounts.dependencies import owned_account
from app.accounts.models import BrokerAccount
from app.accounts.vault import VaultError
from app.core.config import settings

router = APIRouter(tags=["accounts"])

LOGIN_STATE_TTL = 600  # seconds a started login stays valid
SUCCESS_PAGE = "<h3>{} login successful.</h3><p>You can close this tab.</p>"


# ---------- shared login plumbing ----------

def _start_login(request: Request, account: BrokerAccount) -> str:
    """Random state ties the callback to a login we started for this account (CSRF protection)."""
    pending: dict[str, tuple[int, str, float]] = request.app.state.login_states
    now = time.monotonic()
    for s, (_, _, expires) in list(pending.items()):
        if expires < now:
            del pending[s]
    state = secrets.token_urlsafe(16)
    pending[state] = (account.id, account.broker, now + LOGIN_STATE_TTL)
    return state


async def _account_for_state(request: Request, broker: str, state: str) -> BrokerAccount:
    entry = request.app.state.login_states.pop(state, None)
    if entry is None or entry[1] != broker or entry[2] < time.monotonic():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown or expired login; start again")
    account = await store.get_account(entry[0])
    if account is None or account.status != "active":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That broker account no longer exists")
    return account


def _credentials(account: BrokerAccount) -> dict:
    try:
        creds = store.credentials_of(account)
    except VaultError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    if not creds:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add this account's broker app credentials first")
    return creds


async def _finish_login(request: Request, account: BrokerAccount, access_token: str) -> None:
    await store.save_token(account.id, access_token)
    await request.app.state.accounts.reconnect(account.id)


def _callback_result(broker: str, account_id: int | None, error: str | None = None) -> Response:
    """Where the browser lands after the broker login: the frontend if configured, else a page."""
    if settings.frontend_url:
        params = {"broker_login_error": error} if error else {"broker_login": broker}
        if account_id is not None:
            params["account_id"] = account_id
        sep = "&" if "?" in settings.frontend_url else "?"
        return RedirectResponse(f"{settings.frontend_url}{sep}{urlencode(params)}", status.HTTP_303_SEE_OTHER)
    if error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, error)
    return HTMLResponse(SUCCESS_PAGE.format(broker.capitalize()))


def _require(*values: str, message: str) -> None:
    if not all(values):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, message)


def _fyers_session(creds: dict, state: str | None = None) -> SessionModel:
    return SessionModel(
        client_id=creds["client_id"],
        secret_key=creds["secret_key"],
        redirect_uri=settings.fyers_redirect_uri,
        response_type="code",
        grant_type="authorization_code",
        state=state,
    )


@router.get("/accounts/{account_id}/broker-login")
async def broker_login(request: Request, redirect: bool = True, account: BrokerAccount = Depends(owned_account)):
    """Start the broker login for this account. Default: redirect the browser.
    ?redirect=false: {"url": ...} (a frontend then sets window.location)."""
    if account.broker == "fyers":
        creds = _credentials(account)
        url = _fyers_session(creds, _start_login(request, account)).generate_authcode()
    elif account.broker == "zerodha":
        creds = _credentials(account)
        # Kite echoes redirect_params back as query params on the callback.
        login_url = KiteConnect(api_key=creds["api_key"]).login_url()
        url = f"{login_url}&{urlencode({'redirect_params': urlencode({'state': _start_login(request, account)})})}"
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{account.broker} accounts need no login")
    return RedirectResponse(url) if redirect else {"url": url}


# ---------- callbacks (no session needed: the state identifies the login) ----------

@router.get("/auth/fyers/callback", response_class=HTMLResponse, include_in_schema=False)
async def fyers_callback(request: Request, auth_code: str = "", state: str = ""):
    account_id = None
    try:
        account = await _account_for_state(request, "fyers", state)
        account_id = account.id
        _require(auth_code, message="Fyers did not return an auth code")
        session = _fyers_session(_credentials(account))
        session.set_token(auth_code)
        resp = session.generate_token()
        access_token = resp.get("access_token")
        if not access_token:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Fyers login failed: {resp.get('message', resp)}")
    except HTTPException as e:
        if not settings.frontend_url:
            raise
        return _callback_result("fyers", account_id, str(e.detail))

    await _finish_login(request, account, access_token)
    return _callback_result("fyers", account.id)


@router.get("/auth/zerodha/callback", response_class=HTMLResponse, include_in_schema=False)
async def zerodha_callback(request: Request, request_token: str = "", state: str = ""):
    account_id = None
    try:
        account = await _account_for_state(request, "zerodha", state)
        account_id = account.id
        login_status = request.query_params.get("status", "")
        _require(request_token, message="Zerodha did not return a request token")
        if login_status and login_status != "success":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Zerodha login status: {login_status}")
        creds = _credentials(account)
        kite = KiteConnect(api_key=creds["api_key"])
        try:
            session = kite.generate_session(request_token, api_secret=creds["api_secret"])
        except KiteException as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Zerodha login failed: {e}")
    except HTTPException as e:
        if not settings.frontend_url:
            raise
        return _callback_result("zerodha", account_id, str(e.detail))

    await _finish_login(request, account, session["access_token"])
    return _callback_result("zerodha", account.id)


# The redirect URLs registered with the brokers (no /api prefix), so existing broker app
# settings keep working.
callback_router = APIRouter(include_in_schema=False)
callback_router.add_api_route("/auth/fyers/callback", fyers_callback, methods=["GET"])
callback_router.add_api_route("/auth/zerodha/callback", zerodha_callback, methods=["GET"])
