"""Multi-user broker accounts: credentials encrypted at rest and never shown, every user
isolated from every other, account lifecycle, personal API tokens and sign-up."""

import sqlite3

import pytest
from starlette.websockets import WebSocketDisconnect

from app.accounts import store
from app.accounts.models import BrokerAccount
from app.accounts.vault import Vault, VaultError, generate_key
from app.auth.models import ApiToken
from app.core import db
from app.core.config import obsolete_settings, settings
from tests.conftest import ADMIN_PASSWORD, AUTH, TOKEN, add_user, recv_until, run

FYERS = {"client_id": "XB12345-100", "secret_key": "super-secret-value"}


def add_fyers(client, headers=AUTH, label="Fyers", credentials=FYERS):
    return client.post("/api/accounts", json={"broker": "fyers", "label": label, "credentials": credentials},
                       headers=headers)


def db_file() -> str:
    return settings.database_url.split("///", 1)[1]


# ---------- the vault ----------

def test_vault_round_trip_and_binding():
    v = Vault(generate_key())
    sealed = v.encrypt(b"secret", "broker_account:1:credentials")
    assert sealed.startswith("v1:") and "secret" not in sealed
    assert v.decrypt(sealed, "broker_account:1:credentials") == b"secret"
    assert v.encrypt(b"secret", "x") != v.encrypt(b"secret", "x")  # random nonce
    # Moved onto another account or field: refuses instead of handing it over.
    with pytest.raises(VaultError, match="could not be decrypted"):
        v.decrypt(sealed, "broker_account:2:credentials")
    with pytest.raises(VaultError, match="could not be decrypted"):
        v.decrypt(sealed[:-4] + "AAAA", "broker_account:1:credentials")  # tampered


def test_vault_keys_and_rotation():
    old, new = generate_key(), generate_key()
    sealed = Vault(old).encrypt(b"s", "c")
    with pytest.raises(VaultError, match="doesn't have"):
        Vault(new).decrypt(sealed, "c")
    rotating = Vault(new, [old])
    assert rotating.decrypt(sealed, "c") == b"s" and not rotating.is_current(sealed)
    assert rotating.is_current(rotating.encrypt(b"s", "c"))
    with pytest.raises(ValueError, match="32 random bytes"):
        Vault("too-short")
    with pytest.raises(VaultError, match="CREDENTIALS_KEY"):
        Vault("").encrypt(b"s", "c")


def test_rotate_reencrypts_everything(client, monkeypatch):
    acct = add_fyers(client).json()["id"]
    run(client, store.save_token, acct, "today-token")
    old = settings.credentials_key
    monkeypatch.setattr(settings, "credentials_key", generate_key())
    monkeypatch.setattr(settings, "credentials_old_keys", [old])
    assert run(client, store.reencrypt_all) == 2  # credentials + token
    monkeypatch.setattr(settings, "credentials_old_keys", [])  # old key retired

    async def read():
        account = await store.get_account(acct)
        return store.credentials_of(account), store.token_of(account)

    assert run(client, read) == (FYERS, "today-token")


# ---------- secrets stay secret ----------

def test_credentials_are_encrypted_in_the_database(client):
    acct = add_fyers(client).json()["id"]
    run(client, store.save_token, acct, "access-token-xyz")
    raw = sqlite3.connect(db_file()).execute(
        "select credentials, access_token from broker_accounts where id = ?", (acct,)).fetchone()
    blob = " ".join(raw)
    assert "super-secret-value" not in blob and "XB12345-100" not in blob and "access-token-xyz" not in blob
    assert all(v.startswith("v1:") for v in raw)


def test_api_never_returns_secrets(client):
    created = add_fyers(client)
    assert created.status_code == 201
    for body in (created.json(), client.get(f"/api/accounts/{created.json()['id']}", headers=AUTH).json(),
                 client.get("/api/accounts", headers=AUTH).json()):
        assert "super-secret-value" not in str(body) and "XB12345-100" not in str(body)
    creds = created.json()["credentials"]
    assert creds == {"client_id": "XB12…00", "secret_key": "set"}  # masked id, secret only "set"


def test_no_key_no_real_broker_credentials(client, monkeypatch):
    monkeypatch.setattr(settings, "credentials_key", "")
    r = add_fyers(client)
    assert r.status_code == 503 and "AUTOTRADE_CREDENTIALS_KEY" in r.json()["detail"]
    assert client.post("/api/accounts", json={"broker": "paper", "label": "P2"}, headers=AUTH).status_code == 201


def test_credentials_are_validated(client):
    assert add_fyers(client, credentials={"client_id": "x"}).status_code == 422  # secret missing
    assert add_fyers(client, credentials=FYERS | {"extra": "x"}).status_code == 422  # unknown field
    assert client.post("/api/accounts", json={"broker": "paper", "label": "P", "credentials": {"a": "b"}},
                       headers=AUTH).status_code == 422
    assert client.post("/api/accounts", json={"broker": "ib", "label": "X"}, headers=AUTH).status_code == 422
    r = client.post("/api/accounts", json={"broker": "zerodha", "label": "Z",
                                           "credentials": {"api_key": "k", "api_secret": "s"},
                                           "options": {"product": "BAD"}}, headers=AUTH)
    assert r.status_code == 422 and "product" in r.json()["detail"]


# ---------- isolation between users ----------

def test_users_cannot_see_or_touch_each_others_accounts(client):
    bob, bob_acct = add_user(client, "bob")
    # Alice (admin) has account 1; Bob has his own.
    assert [a["id"] for a in client.get("/api/accounts", headers=bob).json()] == [bob_acct]
    for method, path in [("get", "/api/accounts/1"), ("patch", "/api/accounts/1"), ("delete", "/api/accounts/1"),
                         ("get", "/api/accounts/1/orders"), ("get", "/api/accounts/1/funds"),
                         ("get", "/api/accounts/1/orders/history"), ("get", "/api/accounts/1/market/quotes?symbols=X"),
                         ("post", "/api/accounts/1/reconnect")]:
        kwargs = {"json": {}} if method == "patch" else {}
        assert getattr(client, method)(path, headers=bob, **kwargs).status_code == 404, path
    r = client.post("/api/accounts/1/orders", json={"symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 1},
                    headers=bob)
    assert r.status_code == 404
    assert client.get("/api/accounts/1/orders", headers=AUTH).json() == []  # nothing placed for Alice


def test_websockets_only_for_the_owner(client):
    bob, _ = add_user(client, "bob", token="bob-token")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/accounts/1/ws/orders?token=bob-token") as ws:
            ws.receive_json()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/accounts/999/ws/orders?token={TOKEN}") as ws:
            ws.receive_json()


def test_order_updates_go_only_to_their_account(client):
    bob, bob_acct = add_user(client, "bob", token="bob-token")
    with client.websocket_connect(f"/api/accounts/1/ws/orders?token={TOKEN}") as alice_ws, \
         client.websocket_connect(f"/api/accounts/{bob_acct}/ws/orders?token=bob-token") as bob_ws:
        assert alice_ws.receive_json()["account_id"] == 1
        assert bob_ws.receive_json()["account_id"] == bob_acct
        bob_ws.send_json({"action": "place_order", "symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 2})
        assert recv_until(bob_ws, "order_update")["account_id"] == bob_acct
        alice_ws.send_json({"action": "ping", "request_id": "p"})
        # Alice's socket got nothing about Bob's order before her pong.
        assert alice_ws.receive_json() == {"type": "pong", "request_id": "p"}
    assert client.get("/api/accounts/1/orders/history", headers=AUTH).json() == []
    assert len(client.get(f"/api/accounts/{bob_acct}/orders/history", headers=bob).json()) == 1
    assert client.get("/api/trades/open", headers=AUTH).json()["totals"]["count"] == 0
    assert client.get("/api/trades/open", headers=bob).json()["totals"]["count"] == 1
    bob_trade = client.get("/api/trades/open", headers=bob).json()["positions"][0]["id"]
    assert client.get(f"/api/trades/{bob_trade}", headers=AUTH).status_code == 404
    assert client.get(f"/api/trades/open?account_id={bob_acct}", headers=AUTH).json()["totals"]["count"] == 0


def test_strategies_are_per_user(client, monkeypatch):
    from app.strategies import deployments
    from app.strategies.builtin.sma_crossover import SmaCrossover
    from app.strategies.deployment import Deployment

    runner = client.app.state.strategy_runner
    runner.deployments = {"sma": Deployment(key="sma", strategy=SmaCrossover, symbol="NSE:SBIN-EQ", quantity=1)}
    bob, bob_acct = add_user(client, "bob")
    body = {"deployment_key": "sma", "symbol": "NSE:SBIN-EQ", "timeframe": "1m"}
    assert client.post("/api/strategies", json=body | {"account_id": 1}, headers=bob).status_code == 404
    mine = client.post("/api/strategies", json=body | {"account_id": 1}, headers=AUTH).json()
    bobs = client.post("/api/strategies", json=body | {"account_id": bob_acct}, headers=bob).json()
    assert client.post(f"/api/strategies/{mine['id']}/start", headers=AUTH).status_code == 200
    assert client.post(f"/api/strategies/{bobs['id']}/start", headers=bob).status_code == 200

    assert [i["id"] for i in client.get("/api/strategies", headers=bob).json()["strategies"]] == [bobs["id"]]
    for path in [f"/api/strategies/{mine['id']}", f"/api/strategies/{mine['id']}/signals"]:
        assert client.get(path, headers=bob).status_code == 404
    assert client.post(f"/api/strategies/{mine['id']}/stop", headers=bob).status_code == 404
    # Bob's kill switch stops only Bob's.
    assert client.post("/api/strategies/stop-all", headers=bob).json()["stopped"] == [bobs["id"]]
    assert mine["id"] in runner.running


# ---------- account lifecycle ----------

def test_update_credentials_merges_and_drops_todays_login(client):
    acct = add_fyers(client).json()["id"]
    run(client, store.save_token, acct, "tok")
    r = client.patch(f"/api/accounts/{acct}", json={"credentials": {"secret_key": "rotated"}, "label": "Main"},
                     headers=AUTH)
    assert r.status_code == 200 and r.json()["label"] == "Main"

    async def read():
        account = await store.get_account(acct)
        return store.credentials_of(account), store.token_of(account)

    assert run(client, read) == ({"client_id": "XB12345-100", "secret_key": "rotated"}, None)


def test_labels_unique_per_user_and_account_limit(client, monkeypatch):
    assert client.post("/api/accounts", json={"broker": "paper", "label": "Paper"}, headers=AUTH).status_code == 422
    bob, _ = add_user(client, "bob")  # Bob's "Paper" is fine: labels are per user
    monkeypatch.setattr(settings, "max_accounts_per_user", 2)
    assert client.post("/api/accounts", json={"broker": "paper", "label": "Two"}, headers=AUTH).status_code == 201
    r = client.post("/api/accounts", json={"broker": "paper", "label": "Three"}, headers=AUTH)
    assert r.status_code == 422 and "At most 2" in r.json()["detail"]


def test_delete_wipes_secrets_keeps_history(client):
    acct = add_fyers(client).json()["id"]
    paper = client.post("/api/accounts", json={"broker": "paper", "label": "Doomed"}, headers=AUTH).json()["id"]
    client.post(f"/api/accounts/{paper}/orders", json={"symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 1},
                headers=AUTH)
    assert client.delete(f"/api/accounts/{acct}", headers=AUTH).status_code == 204
    assert client.delete(f"/api/accounts/{paper}", headers=AUTH).status_code == 204
    assert [a["id"] for a in client.get("/api/accounts", headers=AUTH).json()] == [1]
    assert client.get(f"/api/accounts/{acct}", headers=AUTH).status_code == 404
    assert client.app.state.accounts.get(acct) is None  # disconnected
    raw = sqlite3.connect(db_file()).execute(
        "select credentials, access_token, status from broker_accounts where id = ?", (acct,)).fetchone()
    assert raw == (None, None, "deleted")
    assert client.get("/api/trades/open", headers=AUTH).json()["totals"]["count"] == 1  # history still yours


def test_delete_refuses_while_strategies_run(client):
    from app.strategies.builtin.sma_crossover import SmaCrossover
    from app.strategies.deployment import Deployment

    client.app.state.strategy_runner.deployments = {
        "sma": Deployment(key="sma", strategy=SmaCrossover, symbol="NSE:SBIN-EQ", quantity=1)}
    inst = client.post("/api/strategies", json={"deployment_key": "sma", "account_id": 1, "symbol": "NSE:SBIN-EQ",
                                                "timeframe": "1m"}, headers=AUTH).json()
    client.post(f"/api/strategies/{inst['id']}/start", headers=AUTH)
    assert client.delete("/api/accounts/1", headers=AUTH).status_code == 409
    client.post(f"/api/strategies/{inst['id']}/stop", headers=AUTH)
    assert client.delete("/api/accounts/1", headers=AUTH).status_code == 204
    assert client.get(f"/api/strategies/{inst['id']}", headers=AUTH).json()["status"] == "archived"


def test_paste_access_token(client, monkeypatch):
    acct = add_fyers(client).json()["id"]
    r = client.post(f"/api/accounts/{acct}/access-token", json={"access_token": "pasted"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["connected"] is False  # tests never reach real Fyers
    assert client.get(f"/api/accounts/{acct}", headers=AUTH).json()["logged_in_today"] is True
    assert client.post("/api/accounts/1/access-token", json={"access_token": "x"}, headers=AUTH).status_code == 422


def test_accounts_survive_restart(client):
    acct = add_fyers(client).json()["id"]
    client.__exit__(None, None, None)
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as fresh:
        assert {a["id"] for a in fresh.get("/api/accounts", headers=AUTH).json()} == {1, acct}
        assert fresh.app.state.accounts.get(1).service.connected  # paper reconnects at startup


def test_supported_brokers(client):
    body = client.get("/api/accounts/brokers", headers=AUTH).json()
    assert body["fyers"]["credentials"] == ["client_id", "secret_key"]
    assert body["zerodha"]["credentials"] == ["api_key", "api_secret"] and body["paper"]["credentials"] == []


# ---------- API tokens and sign-up ----------

def test_personal_api_tokens(client):
    created = client.post("/api/auth/tokens", json={"name": "bot"}, headers=AUTH)
    assert created.status_code == 201
    token = created.json()["token"]
    mine = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/accounts/1/funds", headers=mine).status_code == 200  # acts as admin
    listed = client.get("/api/auth/tokens", headers=AUTH).json()
    assert [t["name"] for t in listed] == ["test", "bot"] and all("token" not in t for t in listed)

    async def hashes():
        async with db.session() as s:
            from sqlalchemy import select
            return list(await s.scalars(select(ApiToken.token_hash)))
    assert token not in run(client, hashes)  # only the hash is stored

    bob, _ = add_user(client, "bob")
    assert client.delete(f"/api/auth/tokens/{created.json()['id']}", headers=bob).status_code == 404
    assert client.delete(f"/api/auth/tokens/{created.json()['id']}", headers=AUTH).status_code == 204
    assert client.get("/api/accounts/1/funds", headers=mine).status_code == 401  # revoked


def test_disabled_users_tokens_stop_working(client):
    from app.auth.users import set_active

    run(client, set_active, "admin", False)
    assert client.get("/api/accounts", headers=AUTH).status_code == 401


def test_signup(client, monkeypatch):
    body = {"username": " New.Bie@Example.com ", "password": "a long enough password"}
    assert client.post("/api/auth/signup", json=body).status_code == 404  # off by default
    monkeypatch.setattr(settings, "allow_signup", True)
    r = client.post("/api/auth/signup", json=body)
    assert r.status_code == 201 and r.json()["username"] == "new.bie@example.com"  # stored lowercase
    assert client.get("/api/accounts").json() == []  # signed in, no accounts yet
    r = client.post("/api/auth/signup", json={**body, "username": "new.bie@EXAMPLE.com"})
    assert r.status_code == 422 and "already exists" in r.json()["detail"]  # taken, whatever the case
    r = client.post("/api/auth/signup", json={"username": "newbie", "password": "a long enough password"})
    assert r.status_code == 422 and "email" in r.json()["detail"]  # not an email
    assert client.post("/api/auth/signup", json={"username": "x@y.in", "password": "short"}).status_code == 422


def test_login_by_email_ignores_case_and_old_usernames_still_work(client, monkeypatch):
    monkeypatch.setattr(settings, "allow_signup", True)
    pw = "a long enough password"
    assert client.post("/api/auth/signup", json={"username": "trader@example.com", "password": pw}).status_code == 201
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"username": "  Trader@Example.COM", "password": pw})
    assert r.status_code == 200 and r.json()["username"] == "trader@example.com"
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"username": "TRADER@example.com", "password": "wrong password!"})
    assert r.status_code == 401 and r.json()["detail"] == "Invalid email or password."
    # An account made before emails (here the test admin) signs in with its username as before.
    assert client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}).status_code == 200


def test_obsolete_settings_are_reported_by_name(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AUTOTRADE_BROKER=fyers\nAUTOTRADE_FYERS_SECRET_KEY=hunter2\nAUTOTRADE_TICK_INTERVAL=1\n")
    names = obsolete_settings(str(env))
    assert names == ["AUTOTRADE_BROKER", "AUTOTRADE_FYERS_SECRET_KEY"] and "hunter2" not in str(names)


def test_deleted_or_foreign_account_rows(client):
    """The ORM row for a deleted account is still there (history); routes treat it as gone."""
    acct = add_fyers(client).json()["id"]
    client.delete(f"/api/accounts/{acct}", headers=AUTH)

    async def status():
        async with db.session() as s:
            return (await s.get(BrokerAccount, acct)).status
    assert run(client, status) == "deleted"
    assert client.post(f"/api/accounts/{acct}/reconnect", headers=AUTH).status_code == 404
