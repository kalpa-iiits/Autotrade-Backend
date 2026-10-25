"""Daily broker login per broker account (Fyers, Zerodha), with each account's own app
credentials, and the stored token's lifetime."""

from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest

import app.auth.router as auth_router
from app.accounts import store
from app.accounts.models import BrokerAccount
from app.core import db
from tests.conftest import AUTH, add_user, run


class FakeSession:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeSession.instances.append(self)

    def generate_authcode(self):
        return f"https://api-t1.fyers.in/api/v3/generate-authcode?state={self.kwargs['state']}"

    def set_token(self, code):
        self.code = code

    def generate_token(self):
        if self.code == "bad":
            return {"s": "error", "message": "invalid auth code"}
        return {"s": "ok", "access_token": "fresh-token"}


def add_account(client, broker, credentials, headers=AUTH, label=None):
    r = client.post("/api/accounts", json={"broker": broker, "label": label or broker.capitalize(),
                                           "credentials": credentials}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def fyers(client, monkeypatch):
    """A Fyers account (id returned) whose reconnects are recorded instead of connecting."""
    FakeSession.instances = []
    monkeypatch.setattr(auth_router, "SessionModel", FakeSession)
    account_id = add_account(client, "fyers", {"client_id": "XB12345-100", "secret_key": "fsecret"})
    reconnects = []

    async def fake_reconnect(acct):
        account = await store.get_account(acct)
        reconnects.append((acct, store.token_of(account)))

    monkeypatch.setattr(client.app.state.accounts, "reconnect", fake_reconnect)
    return account_id, reconnects


def start_login(client, account_id, headers=AUTH):
    r = client.get(f"/api/accounts/{account_id}/broker-login", headers=headers, follow_redirects=False)
    assert r.status_code == 307, r.text
    return parse_qs(urlparse(r.headers["location"]).query)["state"][0]


def test_login_requires_the_owner(client, fyers):
    account_id, _ = fyers
    assert client.get(f"/api/accounts/{account_id}/broker-login", follow_redirects=False).status_code == 401
    other, _ = add_user(client, "mallory")
    assert client.get(f"/api/accounts/{account_id}/broker-login", headers=other,
                      follow_redirects=False).status_code == 404


def test_full_login_uses_the_accounts_own_app_and_saves_token(client, fyers):
    account_id, reconnects = fyers
    state = start_login(client, account_id)
    assert (FakeSession.instances[-1].kwargs["client_id"], FakeSession.instances[-1].kwargs["secret_key"]) == (
        "XB12345-100", "fsecret")  # decrypted for this account only
    r = client.get(f"/api/auth/fyers/callback?auth_code=abc&state={state}")
    assert r.status_code == 200 and "successful" in r.text
    assert reconnects == [(account_id, "fresh-token")]
    assert client.get(f"/api/accounts/{account_id}", headers=AUTH).json()["logged_in_today"] is True

    # A state can only be used once.
    assert client.get(f"/api/auth/fyers/callback?auth_code=abc&state={state}").status_code == 400


def test_callback_rejects_unknown_state_and_bad_code(client, fyers):
    account_id, reconnects = fyers
    assert client.get("/api/auth/fyers/callback?auth_code=abc&state=forged").status_code == 400
    state = start_login(client, account_id)
    assert client.get(f"/api/auth/fyers/callback?auth_code=bad&state={state}").status_code == 502
    assert reconnects == []


def test_paper_accounts_need_no_login(client):
    assert client.get("/api/accounts/1/broker-login", headers=AUTH).status_code == 422


def test_token_expires_with_the_day(client, fyers):
    account_id, _ = fyers
    run(client, store.save_token, account_id, "tok")

    async def token():
        return store.token_of(await store.get_account(account_id))

    assert run(client, token) == "tok"

    async def backdate():
        async with db.session() as s, s.begin():
            (await s.get(BrokerAccount, account_id)).token_date = date(2000, 1, 1)

    run(client, backdate)
    assert run(client, token) is None
    run(client, store.clear_token, account_id)
    assert run(client, token) is None


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
def zerodha(client, monkeypatch):
    monkeypatch.setattr(auth_router, "KiteConnect", FakeKite)
    account_id = add_account(client, "zerodha", {"api_key": "kitekey", "api_secret": "zsecret"})
    reconnects = []

    async def fake_reconnect(acct):
        reconnects.append((acct, store.token_of(await store.get_account(acct))))

    monkeypatch.setattr(client.app.state.accounts, "reconnect", fake_reconnect)
    return account_id, reconnects


def start_zerodha_login(client, account_id):
    r = client.get(f"/api/accounts/{account_id}/broker-login", headers=AUTH, follow_redirects=False)
    assert r.status_code == 307
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["api_key"] == ["kitekey"]
    # Kite passes redirect_params back to the callback, carrying our state.
    return parse_qs(query["redirect_params"][0])["state"][0]


def test_zerodha_login_saves_token(client, zerodha):
    account_id, reconnects = zerodha
    state = start_zerodha_login(client, account_id)
    r = client.get(f"/api/auth/zerodha/callback?request_token=rt&action=login&status=success&state={state}")
    assert r.status_code == 200 and "Zerodha" in r.text
    assert reconnects == [(account_id, "kite-token")]


def test_zerodha_callback_failures(client, zerodha):
    account_id, reconnects = zerodha
    assert client.get("/api/auth/zerodha/callback?request_token=rt&state=forged").status_code == 400
    state = start_zerodha_login(client, account_id)
    assert client.get(f"/api/auth/zerodha/callback?request_token=bad&status=success&state={state}").status_code == 502
    state = start_zerodha_login(client, account_id)
    assert client.get(f"/api/auth/zerodha/callback?request_token=rt&status=cancelled&state={state}").status_code == 400
    assert reconnects == []


def test_a_fyers_state_cannot_finish_a_zerodha_login(client, fyers, zerodha):
    state = start_login(client, fyers[0])
    assert client.get(f"/api/auth/zerodha/callback?request_token=rt&state={state}").status_code == 400


def test_broker_logout_clears_token(client, zerodha):
    account_id, _ = zerodha
    run(client, store.save_token, account_id, "kite-token")
    r = client.post(f"/api/accounts/{account_id}/broker-logout", headers=AUTH)
    assert r.status_code == 200
    assert client.get(f"/api/accounts/{account_id}", headers=AUTH).json()["logged_in_today"] is False
