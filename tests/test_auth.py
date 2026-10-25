from urllib.parse import parse_qs, urlparse

import pytest

import app.auth.router as auth_router
from app.auth.token_store import TokenStore
from app.core.config import settings
from tests.conftest import TOKEN, run


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def generate_authcode(self):
        return f"https://api-t1.fyers.in/api/v3/generate-authcode?state={self.kwargs['state']}"

    def set_token(self, code):
        self.code = code

    def generate_token(self):
        if self.code == "bad":
            return {"s": "error", "message": "invalid auth code"}
        return {"s": "ok", "access_token": "fresh-token"}


@pytest.fixture
def fyers_login(client, monkeypatch):
    monkeypatch.setattr(settings, "fyers_client_id", "XB12345-100")
    monkeypatch.setattr(settings, "fyers_secret_key", "secret")
    monkeypatch.setattr(auth_router, "SessionModel", FakeSession)
    monkeypatch.setattr(client.app.state.brokers, "name", "fyers")
    restarts = []

    async def fake_restart():
        restarts.append(await client.app.state.token_stores["fyers"].load())

    monkeypatch.setattr(client.app.state.brokers, "restart", fake_restart)
    return restarts


def start_login(client):
    r = client.get(f"/api/auth/fyers/login?token={TOKEN}", follow_redirects=False)
    assert r.status_code == 307
    return parse_qs(urlparse(r.headers["location"]).query)["state"][0]


def test_status_requires_token(client):
    assert client.get("/api/auth/status").status_code == 401
    body = client.get("/api/auth/status", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert body == {"broker": "paper", "connected": True, "reason": None,
                    "logged_in_today": {"fyers": False, "zerodha": False}}


def test_login_requires_api_token(client, fyers_login):
    assert client.get("/api/auth/fyers/login", follow_redirects=False).status_code == 401


def test_full_login_saves_token_and_reconnects(client, fyers_login):
    state = start_login(client)
    r = client.get(f"/api/auth/fyers/callback?auth_code=abc&state={state}")
    assert r.status_code == 200 and "successful" in r.text
    assert fyers_login == ["fresh-token"]  # broker restarted with the new token

    # A state can only be used once.
    assert client.get(f"/api/auth/fyers/callback?auth_code=abc&state={state}").status_code == 400


def test_callback_rejects_unknown_state_and_bad_code(client, fyers_login):
    assert client.get("/api/auth/fyers/callback?auth_code=abc&state=forged").status_code == 400
    state = start_login(client)
    assert client.get(f"/api/auth/fyers/callback?auth_code=bad&state={state}").status_code == 502
    assert fyers_login == []


def test_token_store_expires_tokens_from_another_day(client):
    from datetime import date

    from app.auth.models import BrokerToken
    from app.core import db

    store = TokenStore("fyers")
    assert run(client, store.load) is None
    run(client, store.save, "tok")
    assert run(client, store.load) == "tok"
    run(client, store.save, "tok2")  # saving again replaces today's token
    assert run(client, store.load) == "tok2"

    async def backdate():
        async with db.session() as s, s.begin():
            (await s.get(BrokerToken, "fyers")).token_date = date(2000, 1, 1)

    run(client, backdate)
    assert run(client, store.load) is None
    run(client, store.clear)
    assert run(client, store.load) is None


# ---------- Zerodha ----------

class FakeKite:
    def __init__(self, api_key, **kwargs):
        self.api_key = api_key

    def login_url(self):
        return f"https://kite.zerodha.com/connect/login?api_key={self.api_key}&v=3"

    def generate_session(self, request_token, api_secret):
        from kiteconnect.exceptions import TokenException
        if request_token == "bad":
            raise TokenException("Token is invalid or has expired.")
        assert api_secret == "zsecret"
        return {"access_token": "kite-token", "user_id": "AB1234"}


@pytest.fixture
def zerodha_login(client, monkeypatch):
    monkeypatch.setattr(settings, "zerodha_api_key", "kitekey")
    monkeypatch.setattr(settings, "zerodha_api_secret", "zsecret")
    monkeypatch.setattr(auth_router, "KiteConnect", FakeKite)
    restarts = []

    async def fake_restart():
        restarts.append(await client.app.state.token_stores["zerodha"].load())

    monkeypatch.setattr(client.app.state.brokers, "restart", fake_restart)
    return restarts


def start_zerodha_login(client):
    r = client.get(f"/api/auth/zerodha/login?token={TOKEN}", follow_redirects=False)
    assert r.status_code == 307
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["api_key"] == ["kitekey"]
    # Kite passes redirect_params back to the callback, carrying our state.
    return parse_qs(query["redirect_params"][0])["state"][0]


def test_zerodha_login_saves_token(client, zerodha_login, monkeypatch):
    monkeypatch.setattr(client.app.state.brokers, "name", "zerodha")
    state = start_zerodha_login(client)
    r = client.get(f"/api/auth/zerodha/callback?request_token=rt&action=login&status=success&state={state}")
    assert r.status_code == 200 and "Zerodha" in r.text
    assert zerodha_login == ["kite-token"]


def test_zerodha_login_for_inactive_broker_saves_without_restart(client, zerodha_login):
    state = start_zerodha_login(client)  # active broker is paper
    assert client.get(f"/api/auth/zerodha/callback?request_token=rt&status=success&state={state}").status_code == 200
    assert zerodha_login == []
    assert run(client, client.app.state.token_stores["zerodha"].load) == "kite-token"


def test_zerodha_callback_failures(client, zerodha_login):
    # A Fyers state cannot complete a Zerodha login.
    assert client.get("/api/auth/zerodha/callback?request_token=rt&state=forged").status_code == 400
    state = start_zerodha_login(client)
    assert client.get(f"/api/auth/zerodha/callback?request_token=bad&status=success&state={state}").status_code == 502
    state = start_zerodha_login(client)
    assert client.get(f"/api/auth/zerodha/callback?request_token=rt&status=cancelled&state={state}").status_code == 400
    assert zerodha_login == []


def test_logout_clears_token(client, zerodha_login):
    run(client, client.app.state.token_stores["zerodha"].save, "kite-token")
    r = client.post("/api/auth/zerodha/logout", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert run(client, client.app.state.token_stores["zerodha"].load) is None
    assert client.post("/api/auth/nope/logout", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404
