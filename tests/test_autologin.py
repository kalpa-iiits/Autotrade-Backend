"""Morning auto-login (Fyers, Zerodha): TOTP, the stored login details, the headless flows and scheduling."""

import base64
import json
from datetime import datetime

import httpx
import pytest

import app.accounts.autologin as autologin
from app.accounts import store
from app.accounts.autologin import AutoLogin, AutoLoginError, AutoLoginRefused, fyers_login, totp, zerodha_login
from app.accounts.store import IST
from tests.conftest import AUTH, run

TOTP_KEY = base64.b32encode(b"12345678901234567890").decode()  # RFC 6238 test key
FYERS_AUTO = {"client_id": "XB12345-100", "secret_key": "super-secret-value",
              "fy_id": "XA12345", "pin": "1234", "totp_secret": TOTP_KEY}


def add_fyers(client, credentials=FYERS_AUTO):
    add_fyers.n = getattr(add_fyers, "n", 0) + 1
    return client.post("/api/accounts", json={"broker": "fyers", "label": f"Fyers {add_fyers.n}",
                                              "credentials": credentials}, headers=AUTH)


def test_totp_matches_rfc_6238():
    assert totp(TOTP_KEY, 59, digits=8) == "94287082"
    assert totp(TOTP_KEY, 1111111109, digits=8) == "07081804"
    assert totp(TOTP_KEY.lower(), 59) == "287082"  # case and 6 digits like authenticator apps


def test_login_details_are_optional_all_or_none_and_never_shown(client):
    r = add_fyers(client, {"client_id": "XB12345-100", "secret_key": "s"})
    assert r.status_code == 201 and r.json()["auto_login"]["enabled"] is False

    for bad in ({"fy_id": "XA1"}, {"pin": "12a4"}, {"totp_secret": "not base32!"}):
        r = add_fyers(client, {**FYERS_AUTO, **bad} if "fy_id" not in bad else {"client_id": "a", "secret_key": "b", **bad})
        assert r.status_code == 422, bad

    view = add_fyers(client).json()
    assert view["auto_login"]["enabled"] is True
    assert view["credentials"]["pin"] == view["credentials"]["totp_secret"] == "set"
    assert view["credentials"]["fy_id"] == "XA12…45"
    assert "1234" not in json.dumps(view) and TOTP_KEY not in json.dumps(view)


def test_headless_flow(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.url.path, body))
        if request.url.path.endswith("send_login_otp_v2"):
            assert base64.b64decode(body["fy_id"]).decode() == "XA12345"
            return httpx.Response(200, json={"s": "ok", "request_key": "k1"})
        if request.url.path.endswith("verify_otp"):
            assert body == {"request_key": "k1", "otp": totp(TOTP_KEY)}
            return httpx.Response(200, json={"s": "ok", "request_key": "k2"})
        if request.url.path.endswith("verify_pin_v2"):
            assert base64.b64decode(body["identifier"]).decode() == "1234"
            return httpx.Response(200, json={"s": "ok", "data": {"access_token": "trade"}})
        if request.url.path.endswith("profile"):
            assert request.headers["Authorization"] == "XB12345-100:token-for-CODE"
            return httpx.Response(200, json={"s": "ok", "data": {"name": "Test"}})
        assert request.headers["Authorization"] == "Bearer trade"
        assert body["app_id"] == "XB12345" and body["appType"] == "100"
        return httpx.Response(308, json={"s": "ok", "Url": "https://x/cb?auth_code=CODE&state=autologin"})

    monkeypatch.setattr(autologin, "_exchange_auth_code", lambda creds, uri, code: f"token-for-{code}")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await fyers_login(FYERS_AUTO, "https://x/cb", http)

    import asyncio
    assert asyncio.run(go()) == "token-for-CODE"
    assert len(seen) == 5


def test_headless_flow_rejects_an_unusable_access_token(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("send_login_otp_v2"):
            return httpx.Response(200, json={"s": "ok", "request_key": "k1"})
        if request.url.path.endswith("verify_otp"):
            return httpx.Response(200, json={"s": "ok", "request_key": "k2"})
        if request.url.path.endswith("verify_pin_v2"):
            return httpx.Response(200, json={"s": "ok", "data": {"access_token": "trade"}})
        if request.url.path.endswith("token"):
            return httpx.Response(308, json={"s": "ok", "Url": "https://x/cb?auth_code=CODE"})
        return httpx.Response(401, json={"s": "error", "message": "Could not authenticate the user"})

    monkeypatch.setattr(autologin, "_exchange_auth_code", lambda *_: "bad-token")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await fyers_login(FYERS_AUTO, "https://x/cb", http)

    import asyncio
    with pytest.raises(AutoLoginError, match="validate access token: Could not authenticate the user"):
        asyncio.run(go())


def test_headless_flow_reports_the_failing_step():
    def handler(request):
        return httpx.Response(200, json={"s": "error", "message": "Invalid PIN"} if "pin" in request.url.path
                              else {"s": "ok", "request_key": "k"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await fyers_login(FYERS_AUTO, "https://x/cb", http)

    import asyncio
    with pytest.raises(AutoLoginError, match="verify PIN: Invalid PIN"):
        asyncio.run(go())


def test_auto_login_endpoint_saves_todays_token(client, monkeypatch):
    calls = []

    async def fake_login(creds, redirect_uri, http=None):
        calls.append(creds["fy_id"])
        return "fresh-token"

    monkeypatch.setattr(autologin, "fyers_login", fake_login)
    acct = add_fyers(client).json()["id"]
    r = client.post(f"/api/accounts/{acct}/auto-login", headers=AUTH)
    assert r.status_code == 200, r.text
    view = client.get(f"/api/accounts/{acct}", headers=AUTH).json()
    assert view["logged_in_today"] is True and view["auto_login"]["last"]["ok"] is True
    assert calls == ["XA12345"]

    no_details = add_fyers(client, {"client_id": "XB1-100", "secret_key": "s"}).json()["id"]
    assert client.post(f"/api/accounts/{no_details}/auto-login", headers=AUTH).status_code == 422
    assert client.post("/api/accounts/1/auto-login", headers=AUTH).status_code == 422  # paper


def test_auto_login_endpoint_reports_fyers_errors(client, monkeypatch):
    async def refuse(creds, redirect_uri, http=None):
        raise AutoLoginError("verify TOTP: Invalid OTP")

    monkeypatch.setattr(autologin, "fyers_login", refuse)
    acct = add_fyers(client).json()["id"]
    r = client.post(f"/api/accounts/{acct}/auto-login", headers=AUTH)
    assert r.status_code == 502 and "Invalid OTP" in r.json()["detail"]
    view = client.get(f"/api/accounts/{acct}", headers=AUTH).json()
    assert view["logged_in_today"] is False and view["auto_login"]["last"]["ok"] is False


def test_morning_run_logs_in_only_accounts_that_need_it(client, monkeypatch):
    attempts = []

    async def flaky(creds, redirect_uri, http=None):
        attempts.append(creds["client_id"])
        if len(attempts) == 1:
            raise AutoLoginError("verify TOTP: Invalid OTP")  # first try fails, retry works
        return "tok"

    monkeypatch.setattr(autologin, "fyers_login", flaky)
    due = add_fyers(client).json()["id"]
    add_fyers(client, {"client_id": "XB9-100", "secret_key": "s"})  # no login details: skipped
    job: AutoLogin = client.app.state.auto_login
    job.retry_delay = 0
    run(client, job.run_once)
    assert attempts == ["XB12345-100", "XB12345-100"]
    assert run(client, store.token_of, run(client, store.get_account, due)) == "tok"

    run(client, job.run_once)  # already logged in today: nothing to do
    assert len(attempts) == 2


def test_schedule_time(client):
    job: AutoLogin = client.app.state.auto_login
    now = datetime(2026, 11, 9, 7, 0, tzinfo=IST)
    assert job._today_at(now) == datetime(2026, 11, 9, 8, 45, tzinfo=IST)
    assert job._next_run(now) == datetime(2026, 11, 9, 8, 45, tzinfo=IST)
    assert job._next_run(datetime(2026, 11, 9, 8, 46, tzinfo=IST)) == datetime(2026, 11, 9, 9, 10, tzinfo=IST)
    assert job._next_run(datetime(2026, 11, 9, 9, 10, tzinfo=IST)) == datetime(2026, 11, 10, 8, 45, tzinfo=IST)
    assert job._attempts_at(datetime(2026, 11, 9, 8, 45, tzinfo=IST)) == 3
    assert job._attempts_at(datetime(2026, 11, 9, 9, 10, tzinfo=IST)) == 1


# ---------- Zerodha ----------

ZERODHA_AUTO = {"api_key": "kiteapikey", "api_secret": "kite-secret-value",
                "user_id": "AB1234", "password": "hunter2-pass", "totp_secret": TOTP_KEY}


def add_zerodha(client, credentials=ZERODHA_AUTO):
    add_zerodha.n = getattr(add_zerodha, "n", 0) + 1
    return client.post("/api/accounts", json={"broker": "zerodha", "label": f"Kite {add_zerodha.n}",
                                              "credentials": credentials}, headers=AUTH)


def test_zerodha_login_details_are_optional_all_or_none_and_never_shown(client):
    r = add_zerodha(client, {"api_key": "k", "api_secret": "s"})
    assert r.status_code == 201 and r.json()["auto_login"]["enabled"] is False

    r = add_zerodha(client, {"api_key": "k", "api_secret": "s", "user_id": "AB1234"})
    assert r.status_code == 422 and "auto-login needs all of" in r.text
    assert add_zerodha(client, {**ZERODHA_AUTO, "totp_secret": "not base32!"}).status_code == 422

    view = add_zerodha(client).json()
    assert view["auto_login"]["enabled"] is True
    assert view["credentials"]["password"] == view["credentials"]["totp_secret"] == "set"
    assert view["credentials"]["user_id"] == "••••"  # an identifier, masked (short ones fully)
    dumped = json.dumps(view)
    assert "hunter2-pass" not in dumped and TOTP_KEY not in dumped


def kite_handler(seen, password_ok=True, authorized=True):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/login":
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["user_id"] == "AB1234"
            if not password_ok or form["password"] != "hunter2-pass":
                return httpx.Response(403, json={"status": "error", "message": "Invalid username or password."})
            return httpx.Response(200, json={"status": "success", "data": {"request_id": "rid", "twofa_type": "totp"}})
        if request.url.path == "/api/twofa":
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["request_id"] == "rid" and form["twofa_value"] == totp(TOTP_KEY)
            return httpx.Response(200, json={"status": "success", "data": {}})
        if request.url.path == "/connect/login":
            assert request.url.params["api_key"] == "kiteapikey"
            return httpx.Response(302, headers={"location": "/connect/finish?sess_id=s1&api_key=kiteapikey"})
        if request.url.path == "/connect/finish":
            if not authorized:
                return httpx.Response(200, text="<html>Authorize app</html>")
            return httpx.Response(302, headers={
                "location": "http://127.0.0.1:8000/auth/zerodha/callback?request_token=RT&action=login&status=success"})
        raise AssertionError(f"unexpected request to {request.url}")  # never our own callback
    return handler


def _run_zerodha(handler):
    import asyncio

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await zerodha_login(ZERODHA_AUTO, http)

    return asyncio.run(go())


def test_zerodha_headless_flow(monkeypatch):
    monkeypatch.setattr(autologin, "_exchange_request_token", lambda creds, rt: f"token-for-{rt}")
    seen = []
    assert _run_zerodha(kite_handler(seen)) == "token-for-RT"
    assert seen == ["/api/login", "/api/twofa", "/connect/login", "/connect/finish"]


def test_zerodha_wrong_password_and_unauthorized_app_are_not_retryable():
    with pytest.raises(AutoLoginRefused, match="password login: Invalid username or password"):
        _run_zerodha(kite_handler([], password_ok=False))
    with pytest.raises(AutoLoginRefused, match="authorize this Kite app"):
        _run_zerodha(kite_handler([], authorized=False))


def test_zerodha_auto_login_endpoint_and_no_retry_on_refusal(client, monkeypatch):
    calls = []

    async def fake_login(creds, http=None):
        calls.append(creds["user_id"])
        return "kite-token"

    monkeypatch.setattr(autologin, "zerodha_login", fake_login)
    acct = add_zerodha(client).json()["id"]
    r = client.post(f"/api/accounts/{acct}/auto-login", headers=AUTH)
    assert r.status_code == 200, r.text
    view = client.get(f"/api/accounts/{acct}", headers=AUTH).json()
    assert view["logged_in_today"] is True and view["auto_login"]["last"]["ok"] is True
    assert calls == ["AB1234"]

    no_details = add_zerodha(client, {"api_key": "k", "api_secret": "s"}).json()["id"]
    r = client.post(f"/api/accounts/{no_details}/auto-login", headers=AUTH)
    assert r.status_code == 422 and "Kite user id, password and TOTP secret" in r.text

    async def wrong_password(creds, http=None):
        calls.append("refused")
        raise AutoLoginRefused("password login: Invalid username or password.")

    monkeypatch.setattr(autologin, "zerodha_login", wrong_password)
    job: AutoLogin = client.app.state.auto_login
    job.retry_delay = 0
    other = add_zerodha(client).json()["id"]
    with pytest.raises(AutoLoginRefused):
        run(client, job.login, run(client, store.get_account, other))
    assert calls.count("refused") == 1  # tried once: retrying a wrong password risks a lockout
    assert job.last[other]["ok"] is False


def test_morning_run_covers_fyers_and_zerodha(client, monkeypatch):
    done = []

    async def fake_fyers(creds, redirect_uri, http=None):
        done.append("fyers")
        return "f"

    async def fake_kite(creds, http=None):
        done.append("zerodha")
        return "z"

    monkeypatch.setattr(autologin, "fyers_login", fake_fyers)
    monkeypatch.setattr(autologin, "zerodha_login", fake_kite)
    kite = add_zerodha(client).json()["id"]
    add_fyers(client)
    run(client, client.app.state.auto_login.run_once)
    assert sorted(done) == ["fyers", "zerodha"]
    assert run(client, store.token_of, run(client, store.get_account, kite)) == "z"
