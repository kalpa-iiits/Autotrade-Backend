"""Fyers morning auto-login: TOTP, the stored login details, the headless flow and scheduling."""

import base64
import json
from datetime import datetime

import httpx
import pytest

import app.accounts.autologin as autologin
from app.accounts import store
from app.accounts.autologin import AutoLoginError, FyersAutoLogin, fyers_login, totp
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
        body = json.loads(request.content)
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
        assert request.headers["Authorization"] == "Bearer trade"
        assert body["app_id"] == "XB12345" and body["appType"] == "100"
        return httpx.Response(308, json={"s": "ok", "Url": "https://x/cb?auth_code=CODE&state=autologin"})

    monkeypatch.setattr(autologin, "_exchange_auth_code", lambda creds, uri, code: f"token-for-{code}")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await fyers_login(FYERS_AUTO, "https://x/cb", http)

    import asyncio
    assert asyncio.run(go()) == "token-for-CODE"
    assert len(seen) == 4


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
    job: FyersAutoLogin = client.app.state.auto_login
    job.retry_delay = 0
    run(client, job.run_once)
    assert attempts == ["XB12345-100", "XB12345-100"]
    assert run(client, store.token_of, run(client, store.get_account, due)) == "tok"

    run(client, job.run_once)  # already logged in today: nothing to do
    assert len(attempts) == 2


def test_schedule_time(client):
    job: FyersAutoLogin = client.app.state.auto_login
    now = datetime(2026, 11, 9, 7, 0, tzinfo=IST)
    assert job._today_at(now) == datetime(2026, 11, 9, 8, 45, tzinfo=IST)
