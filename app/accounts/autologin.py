"""Broker auto-login: each morning, log every Fyers and Zerodha account that has its login
details stored in without a browser, save the day's access token and reconnect the account.

Fyers (user id, PIN, TOTP secret): the flow Fyers' own web login uses (vagator API): send login
OTP -> verify the TOTP -> verify the PIN -> ask the token API for an auth code for our app ->
exchange it for an access token exactly like the browser callback does.

Zerodha (user id, password, TOTP secret): the flow the Kite web login uses: password login ->
TOTP 2FA -> open the Kite Connect login for our app with that session, which redirects to our
redirect URL with a request token (read from the redirect, never followed) -> exchange it for
an access token exactly like the browser callback does. The Kite app must have been authorized
once by a browser login, and the Kite account must use TOTP (external 2FA).

None of these endpoints are part of the brokers' documented APIs and they may change; when
they fail, the manual broker login (/api/accounts/<id>/broker-login) still works.

Runs at AUTOTRADE_FYERS_AUTO_LOGIN_TIME (IST; despite the name, for every broker) every day, and
once at startup if the server comes up after that time with an account that has no token for
today.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import struct
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx
from fyers_apiv3.fyersModel import SessionModel
from kiteconnect import KiteConnect

from app.accounts import store
from app.accounts.models import BrokerAccount
from app.accounts.specs import AUTO_LOGIN_FIELDS
from app.accounts.store import IST
from app.accounts.vault import VaultError
from app.core.config import Settings

log = logging.getLogger(__name__)

VAGATOR = "https://api-t2.fyers.in/vagator/v2"
TOKEN_URL = "https://api-t1.fyers.in/api/v3/token"
KITE = "https://kite.zerodha.com"
KITE_HOSTS = {"kite.zerodha.com", "kite.trade"}  # redirects we follow (with the Kite session)
MAX_REDIRECTS = 10
ATTEMPTS = 3  # per account per run; each waits for a fresh TOTP code
RETRY_DELAY = 35.0  # seconds: longer than one TOTP step, so a retry never reuses a code


class AutoLoginError(RuntimeError):
    """The broker refused a step of the login (message says which)."""


class AutoLoginRefused(AutoLoginError):
    """Refused in a way retrying won't fix and might make worse (a wrong password: repeated
    tries can lock the broker account). Not retried."""


# ---------- TOTP (RFC 6238: SHA-1, 30 s, 6 digits, like Google Authenticator) ----------

def totp(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits
    return str(code).zfill(digits)


def has_auto_login(broker: str, creds: dict | None) -> bool:
    fields = AUTO_LOGIN_FIELDS.get(broker)
    return bool(fields and creds) and all(creds.get(f) for f in fields)


# ---------- Fyers ----------

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


# ---------- Zerodha ----------

def _kite_check(resp: httpx.Response, step: str, refused: type[AutoLoginError] = AutoLoginError) -> dict:
    try:
        body = resp.json()
    except ValueError:
        raise AutoLoginError(f"{step}: HTTP {resp.status_code}") from None
    if body.get("status") != "success" or resp.status_code >= 400:
        raise refused(f"{step}: {body.get('message') or body}")
    return body.get("data") or {}


def _exchange_request_token(creds: dict, request_token: str) -> str:
    session = KiteConnect(api_key=creds["api_key"]).generate_session(request_token, api_secret=creds["api_secret"])
    if not session.get("access_token"):
        raise AutoLoginError("token exchange: Kite returned no access token")
    return session["access_token"]


async def zerodha_login(creds: dict, http: httpx.AsyncClient | None = None) -> str:
    """Today's access token for a Kite Connect app, from the account holder's user id, password
    and TOTP."""
    own = http is None
    http = http or httpx.AsyncClient(timeout=20)
    try:
        r = await http.post(f"{KITE}/api/login", data={"user_id": creds["user_id"], "password": creds["password"]})
        data = _kite_check(r, "password login", refused=AutoLoginRefused)
        if data.get("twofa_type") not in (None, "totp"):
            raise AutoLoginRefused(f"Kite 2FA is {data['twofa_type']!r}; enable external TOTP on the Kite account")

        r = await http.post(f"{KITE}/api/twofa", data={
            "user_id": creds["user_id"], "request_id": data["request_id"],
            "twofa_value": totp(creds["totp_secret"]), "twofa_type": "totp", "skip_totp": "true"})
        _kite_check(r, "verify TOTP")

        # Kite Connect login for our app, in that session: a few redirects inside Kite, then one
        # to our redirect URL carrying the request token, which is read here and not followed.
        url = f"{KITE}/connect/login?{urlencode({'v': 3, 'api_key': creds['api_key']})}"
        request_token = ""
        for _ in range(MAX_REDIRECTS):
            r = await http.get(url, follow_redirects=False)
            if not r.is_redirect:
                break
            url = urljoin(url, r.headers["location"])
            request_token = (parse_qs(urlparse(url).query).get("request_token") or [""])[0]
            if request_token or urlparse(url).hostname not in KITE_HOSTS:
                break
        if not request_token:
            raise AutoLoginRefused("authorize app: no request token (log in once via the browser to "
                                   "authorize this Kite app, and check its redirect URL)")
    finally:
        if own:
            await http.aclose()
    return await asyncio.to_thread(_exchange_request_token, creds, request_token)


# ---------- scheduling ----------

BROKER_TITLES = {"fyers": "Fyers", "zerodha": "Zerodha"}
DETAILS = {"fyers": "Fyers user id, PIN and TOTP secret", "zerodha": "Kite user id, password and TOTP secret"}


class AutoLogin:
    """Background job owned by the app (app.state.auto_login), for every broker in
    AUTO_LOGIN_FIELDS."""

    def __init__(self, accounts, settings: Settings) -> None:
        self.accounts = accounts  # AccountRegistry
        self.settings = settings
        self.retry_delay = RETRY_DELAY
        self.last: dict[int, dict] = {}  # account id -> {"at", "ok", "error"} of the last attempt
        self._task: asyncio.Task | None = None

    async def _token(self, broker: str, creds: dict) -> str:
        if broker == "fyers":
            return await fyers_login(creds, self.settings.fyers_redirect_uri)
        return await zerodha_login(creds)

    # One account, now (also used by POST /api/accounts/<id>/auto-login).
    async def login(self, account: BrokerAccount, attempts: int = ATTEMPTS) -> None:
        title = BROKER_TITLES.get(account.broker, account.broker)
        if account.broker not in AUTO_LOGIN_FIELDS:
            raise AutoLoginRefused(f"{title} accounts have no auto-login")
        creds = store.credentials_of(account)
        if not has_auto_login(account.broker, creds):
            raise AutoLoginRefused(f"Add the {DETAILS[account.broker]} to this account first")
        token, error = None, None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self.retry_delay)
            try:
                token = await self._token(account.broker, creds)
                break
            except Exception as e:  # network or broker refusal: try again with the next code
                error = e
                log.warning("%s auto-login for account %s failed (attempt %d/%d): %s",
                            title, account.id, attempt + 1, attempts, e)
                if isinstance(e, AutoLoginRefused):
                    break  # e.g. a wrong password: retrying could lock the broker account
        if token is None:
            self.last[account.id] = {"at": datetime.now(IST).isoformat(), "ok": False, "error": str(error)}
            raise error if isinstance(error, AutoLoginError) else AutoLoginError(str(error))
        await store.save_token(account.id, token)
        await self.accounts.reconnect(account.id)
        self.last[account.id] = {"at": datetime.now(IST).isoformat(), "ok": True, "error": None}
        log.info("%s auto-login for account %s succeeded", title, account.id)

    async def run_once(self) -> None:
        """Every Fyers / Zerodha account with login details and no token for today."""
        due = []
        for account in await store.list_accounts():
            if account.broker not in AUTO_LOGIN_FIELDS:
                continue
            try:
                if has_auto_login(account.broker, store.credentials_of(account)) and store.token_of(account) is None:
                    due.append(account)
            except VaultError as e:
                log.warning("Auto-login skipped for account %s: %s", account.id, e)
        results = await asyncio.gather(*(self.login(a) for a in due), return_exceptions=True)
        for account, result in zip(due, results):
            if isinstance(result, Exception):
                log.error("Auto-login for account %s (%s) gave up: %s", account.id, account.broker, result)

    def _today_at(self, now: datetime) -> datetime:
        hour, minute = map(int, self.settings.fyers_auto_login_time.split(":"))
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    async def _loop(self) -> None:
        if datetime.now(IST) >= self._today_at(datetime.now(IST)):
            try:
                await self.run_once()  # started after today's run time: catch up
            except Exception:
                log.exception("Auto-login run failed")
        while True:
            now = datetime.now(IST)
            at = self._today_at(now)
            if at <= now:
                at += timedelta(days=1)
            await asyncio.sleep((at - now).total_seconds())
            try:
                await self.run_once()
            except Exception:
                log.exception("Auto-login run failed")

    def start(self) -> None:
        if self.settings.fyers_auto_login_time and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="broker-auto-login")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
