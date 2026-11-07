"""Fyers auto-login: each morning, log every Fyers account that has its login details stored
(Fyers user id, PIN, TOTP secret) in without a browser, save the day's access token and
reconnect the account.

The headless flow is the one Fyers' own web login uses (vagator API): send login OTP ->
verify the TOTP -> verify the PIN -> ask the token API for an auth code for our app -> exchange
it for an access token exactly like the browser callback does. These endpoints are not part of
the documented API v3 and Fyers may change them; when they fail, the manual broker login
(/api/accounts/<id>/broker-login) still works.

Runs at AUTOTRADE_FYERS_AUTO_LOGIN_TIME (IST) every day, and once at startup if the server
comes up after that time with an account that has no token for today.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import struct
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from fyers_apiv3.fyersModel import SessionModel

from app.accounts import store
from app.accounts.models import BrokerAccount
from app.accounts.specs import AUTO_LOGIN_FIELDS
from app.accounts.store import IST
from app.accounts.vault import VaultError
from app.core.config import Settings

log = logging.getLogger(__name__)

VAGATOR = "https://api-t2.fyers.in/vagator/v2"
TOKEN_URL = "https://api-t1.fyers.in/api/v3/token"
ATTEMPTS = 3  # per account per run; each waits for a fresh TOTP code
RETRY_DELAY = 35.0  # seconds: longer than one TOTP step, so a retry never reuses a code


class AutoLoginError(RuntimeError):
    """Fyers refused a step of the login (message says which)."""


# ---------- TOTP (RFC 6238: SHA-1, 30 s, 6 digits, like Google Authenticator) ----------

def totp(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits
    return str(code).zfill(digits)


def has_auto_login(creds: dict | None) -> bool:
    return bool(creds) and all(creds.get(f) for f in AUTO_LOGIN_FIELDS)


# ---------- the login itself ----------

def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _check(resp: httpx.Response, step: str) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise AutoLoginError(f"{step}: HTTP {resp.status_code}") from None
    if data.get("s") == "error" or (resp.status_code >= 400 and resp.status_code != 308):
        raise AutoLoginError(f"{step}: {data.get('message') or data}")
    return data


def _exchange_auth_code(creds: dict, redirect_uri: str, auth_code: str) -> str:
    session = SessionModel(client_id=creds["client_id"], secret_key=creds["secret_key"],
                           redirect_uri=redirect_uri, response_type="code", grant_type="authorization_code")
    session.set_token(auth_code)
    resp = session.generate_token()
    if not resp.get("access_token"):
        raise AutoLoginError(f"token exchange: {resp.get('message', resp)}")
    return resp["access_token"]


async def fyers_login(creds: dict, redirect_uri: str, http: httpx.AsyncClient | None = None) -> str:
    """Today's access token for a Fyers app, from the account holder's user id, PIN and TOTP."""
    own = http is None
    http = http or httpx.AsyncClient(timeout=20)
    try:
        r = await http.post(f"{VAGATOR}/send_login_otp_v2", json={"fy_id": _b64(creds["fy_id"]), "app_id": "2"})
        request_key = _check(r, "send login OTP")["request_key"]

        r = await http.post(f"{VAGATOR}/verify_otp", json={"request_key": request_key, "otp": totp(creds["totp_secret"])})
        request_key = _check(r, "verify TOTP")["request_key"]

        r = await http.post(f"{VAGATOR}/verify_pin_v2", json={
            "request_key": request_key, "identity_type": "pin", "identifier": _b64(creds["pin"])})
        trade_token = _check(r, "verify PIN")["data"]["access_token"]

        app_id, _, app_type = creds["client_id"].partition("-")
        r = await http.post(TOKEN_URL, headers={"Authorization": f"Bearer {trade_token}"}, json={
            "fyers_id": creds["fy_id"], "app_id": app_id, "redirect_uri": redirect_uri,
            "appType": app_type or "100", "code_challenge": "", "state": "autologin", "scope": "",
            "nonce": "", "response_type": "code", "create_cookie": True})
        data = _check(r, "authorize app")
        url = data.get("Url") or data.get("url") or ""
        auth_code = (parse_qs(urlparse(url).query).get("auth_code") or [""])[0]
        if not auth_code:
            raise AutoLoginError(f"authorize app: no auth code ({data.get('message') or 'check the redirect URI'})")
    finally:
        if own:
            await http.aclose()
    return await asyncio.to_thread(_exchange_auth_code, creds, redirect_uri, auth_code)


# ---------- scheduling ----------

class FyersAutoLogin:
    """Background job owned by the app (app.state.auto_login)."""

    def __init__(self, accounts, settings: Settings) -> None:
        self.accounts = accounts  # AccountRegistry
        self.settings = settings
        self.retry_delay = RETRY_DELAY
        self.last: dict[int, dict] = {}  # account id -> {"at", "ok", "error"} of the last attempt
        self._task: asyncio.Task | None = None

    # One account, now (also used by POST /api/accounts/<id>/auto-login).
    async def login(self, account: BrokerAccount, attempts: int = ATTEMPTS) -> None:
        creds = store.credentials_of(account)
        if not has_auto_login(creds):
            raise AutoLoginError("Add the Fyers user id, PIN and TOTP secret to this account first")
        error: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self.retry_delay)
            try:
                token = await fyers_login(creds, self.settings.fyers_redirect_uri)
                break
            except Exception as e:  # network or Fyers refusal: try again with the next code
                error = e
                log.warning("Fyers auto-login for account %s failed (attempt %d/%d): %s",
                            account.id, attempt + 1, attempts, e)
        else:
            self.last[account.id] = {"at": datetime.now(IST).isoformat(), "ok": False, "error": str(error)}
            raise AutoLoginError(str(error))
        await store.save_token(account.id, token)
        await self.accounts.reconnect(account.id)
        self.last[account.id] = {"at": datetime.now(IST).isoformat(), "ok": True, "error": None}
        log.info("Fyers auto-login for account %s succeeded", account.id)

    async def run_once(self) -> None:
        """Every Fyers account with login details and no token for today."""
        due = []
        for account in await store.list_accounts():
            if account.broker != "fyers":
                continue
            try:
                if has_auto_login(store.credentials_of(account)) and store.token_of(account) is None:
                    due.append(account)
            except VaultError as e:
                log.warning("Fyers auto-login skipped for account %s: %s", account.id, e)
        results = await asyncio.gather(*(self.login(a) for a in due), return_exceptions=True)
        for account, result in zip(due, results):
            if isinstance(result, Exception):
                log.error("Fyers auto-login for account %s gave up: %s", account.id, result)

    def _today_at(self, now: datetime) -> datetime:
        hour, minute = map(int, self.settings.fyers_auto_login_time.split(":"))
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    async def _loop(self) -> None:
        if datetime.now(IST) >= self._today_at(datetime.now(IST)):
            try:
                await self.run_once()  # started after today's run time: catch up
            except Exception:
                log.exception("Fyers auto-login run failed")
        while True:
            now = datetime.now(IST)
            at = self._today_at(now)
            if at <= now:
                at += timedelta(days=1)
            await asyncio.sleep((at - now).total_seconds())
            try:
                await self.run_once()
            except Exception:
                log.exception("Fyers auto-login run failed")

    def start(self) -> None:
        if self.settings.fyers_auto_login_time and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="fyers-auto-login")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
