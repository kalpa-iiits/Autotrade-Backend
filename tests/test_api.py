"""The /api surface a separate frontend uses: JSON auth, REST trading, market data, CORS."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.main import app
from tests.conftest import ADMIN_PASSWORD, TOKEN

FRONTEND = "http://localhost:5173"


def login(client, password=ADMIN_PASSWORD, username="admin"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


# ---------- JSON auth ----------

def test_login_me_logout(client):
    assert client.get("/api/auth/me").status_code == 401
    bad = login(client, password="nope")
    assert bad.status_code == 401 and bad.json()["detail"] == "Invalid username or password."

    r = login(client)
    assert r.status_code == 200 and r.json()["username"] == "admin"
    assert "autotrade_session" in r.headers["set-cookie"]
    assert client.get("/api/auth/me").json()["username"] == "admin"
    assert client.get("/api/strategies").status_code == 200  # cookie works for the rest

    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_json_login_shares_the_lockout(client):
    for _ in range(5):
        login(client, password="nope")
    r = login(client)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


def test_change_password(client):
    other = TestClient(app)  # a second browser, same app
    login(client)
    login(other)
    assert client.post("/api/auth/password", json={"current_password": "wrong", "new_password": "x" * 12}).status_code == 403
    assert client.post("/api/auth/password", json={"current_password": ADMIN_PASSWORD, "new_password": "short"}).status_code == 422

    r = client.post("/api/auth/password", json={"current_password": ADMIN_PASSWORD, "new_password": "a new long password"})
    assert r.status_code == 204
    assert client.get("/api/auth/me").status_code == 200  # this session stays signed in
    assert other.get("/api/auth/me").status_code == 401  # others are signed out
    assert login(client, password="a new long password").status_code == 200


# ---------- REST trading ----------

AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_place_and_cancel_orders(client):
    filled = client.post("/api/accounts/1/orders", json={"symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 3}, headers=AUTH)
    assert filled.status_code == 201 and filled.json()["status"] == "FILLED"

    limit = client.post("/api/accounts/1/orders", json={"symbol": "NSE:TCS-EQ", "side": "BUY", "quantity": 1,
                                             "order_type": "LIMIT", "limit_price": 1.0}, headers=AUTH).json()
    assert limit["status"] == "OPEN"
    cancelled = client.delete(f"/api/accounts/1/orders/{limit['id']}", headers=AUTH)
    assert cancelled.json()["status"] == "CANCELLED"
    again = client.delete(f"/api/accounts/1/orders/{limit['id']}", headers=AUTH)
    assert again.status_code == 422 and "cannot cancel" in again.json()["detail"]

    positions = client.get("/api/accounts/1/positions", headers=AUTH).json()
    assert {"symbol": "NSE:SBIN-EQ", "quantity": 3} == {k: positions[0][k] for k in ("symbol", "quantity")}
    assert client.get("/api/accounts/1/funds", headers=AUTH).json()["cash"] > 0
    assert len(client.get("/api/accounts/1/orders", headers=AUTH).json()) == 2
    assert len(client.get("/api/accounts/1/orders/history", headers=AUTH).json()) == 2


def test_order_validation(client):
    missing_price = client.post("/api/accounts/1/orders", json={"symbol": "NSE:TCS-EQ", "side": "BUY", "quantity": 1,
                                                     "order_type": "LIMIT"}, headers=AUTH)
    assert missing_price.status_code == 422
    unknown = client.post("/api/accounts/1/orders", json={"symbol": "NOPE", "side": "BUY", "quantity": 1}, headers=AUTH)
    assert unknown.status_code == 422 and "Unknown symbol" in unknown.json()["detail"]
    assert client.post("/api/accounts/1/orders", json={"symbol": "NSE:TCS-EQ", "side": "HOLD", "quantity": 1},
                       headers=AUTH).status_code == 422
    assert client.post("/api/accounts/1/orders", json={"symbol": "NSE:TCS-EQ", "side": "BUY", "quantity": 1}).status_code == 401


def test_broker_offline_is_503(client, monkeypatch):
    """A Fyers account with credentials but no login today: its routes say so."""
    import app.accounts.registry as registry
    from app.brokers import create_broker

    monkeypatch.setattr(registry, "create_broker", create_broker)  # refuses before any network: no token
    acct = client.post("/api/accounts", json={"broker": "fyers", "label": "F", "credentials": {
        "client_id": "XB12345-100", "secret_key": "s"}}, headers=AUTH).json()["id"]
    r = client.post(f"/api/accounts/{acct}/orders", json={"symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 1},
                    headers=AUTH)
    assert r.status_code == 503 and "broker-login" in r.json()["detail"]
    assert client.get(f"/api/accounts/{acct}/market/quotes?symbols=NSE:SBIN-EQ", headers=AUTH).status_code == 503
    assert client.get("/api/accounts/1/funds", headers=AUTH).status_code == 200  # other accounts unaffected


# ---------- market data ----------

def test_quotes(client):
    q = client.get("/api/accounts/1/market/quotes?symbols=NSE:SBIN-EQ, NSE:TCS-EQ,NOPE", headers=AUTH).json()
    assert set(q) == {"NSE:SBIN-EQ", "NSE:TCS-EQ"} and all(v > 0 for v in q.values())
    too_many = ",".join(f"S{i}" for i in range(51))
    assert client.get(f"/api/accounts/1/market/quotes?symbols={too_many}", headers=AUTH).status_code == 422


def test_candles(client):
    daily = client.get("/api/accounts/1/market/candles?symbol=NSE:SBIN-EQ&resolution=D&start=2026-10-01&end=2026-10-23",
                       headers=AUTH).json()
    assert len(daily) == 17  # weekdays
    assert set(daily[0]) == {"time", "open", "high", "low", "close", "volume"}
    minute = client.get("/api/accounts/1/market/candles?symbol=NSE:SBIN-EQ&resolution=1&start=2026-10-23&end=2026-10-23",
                        headers=AUTH).json()
    assert len(minute) == 375 and minute[0]["time"].startswith("2026-10-23T09:15")
    assert client.get("/api/accounts/1/market/candles?symbol=NSE:SBIN-EQ&resolution=1&start=2026-01-01&end=2026-10-23",
                      headers=AUTH).status_code == 422  # too long for 1-minute
    assert client.get("/api/accounts/1/market/candles?symbol=NOPE&resolution=D", headers=AUTH).status_code == 422


def test_candles_without_a_price_are_left_out(client, monkeypatch):
    from datetime import datetime, timezone

    from app.brokers.base import Candle
    from app.brokers.paper import PaperBroker

    async def history(self, symbol, resolution, start, end):
        at = lambda m: datetime(2026, 10, 23, 3, 45 + m, tzinfo=timezone.utc)
        return [Candle(at(0), 800, 801, 799, 800.5, None),  # no volume: 0
                Candle(at(1), 800.5, 802, 800, 801, 1200),
                Candle(at(2), 801, None, None, None, 0)]  # unfinished: no prices yet

    monkeypatch.setattr(PaperBroker, "get_history", history)
    rows = client.get("/api/accounts/1/market/candles?symbol=NSE:SBIN-EQ&resolution=1", headers=AUTH).json()
    assert [(r["close"], r["volume"]) for r in rows] == [(800.5, 0), (801, 1200)]


def test_candles_come_oldest_first_one_per_time(client, monkeypatch):
    from datetime import datetime, timezone

    from app.brokers.base import Candle
    from app.brokers.paper import PaperBroker

    async def history(self, symbol, resolution, start, end):
        at = lambda m: datetime(2026, 10, 23, 3, 45 + m, tzinfo=timezone.utc)
        return [Candle(at(0), 800, 801, 799, 800.5), Candle(at(2), 801, 802, 800, 801.5),
                Candle(at(1), 800.5, 802, 800, 801), Candle(at(2), 801, 803, 800, 802)]  # out of order, repeated

    monkeypatch.setattr(PaperBroker, "get_history", history)
    rows = client.get("/api/accounts/1/market/candles?symbol=NSE:SBIN-EQ&resolution=1", headers=AUTH).json()
    assert [(r["time"][11:16], r["close"]) for r in rows] == [("03:45", 800.5), ("03:46", 801), ("03:47", 802)]


# ---------- a frontend on another origin ----------

def test_cors_only_for_configured_origins(client, monkeypatch):
    preflight = {"Origin": FRONTEND, "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "content-type"}
    assert "access-control-allow-origin" not in client.options("/api/auth/login", headers=preflight).headers

    monkeypatch.setattr(settings, "cors_origins", [FRONTEND])
    r = client.options("/api/auth/login", headers=preflight)
    assert r.headers["access-control-allow-origin"] == FRONTEND
    assert r.headers["access-control-allow-credentials"] == "true"
    me = client.get("/api/auth/me", headers={"Origin": FRONTEND})
    assert me.headers["access-control-allow-origin"] == FRONTEND
    evil = client.get("/api/auth/me", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in evil.headers


def test_websocket_accepts_configured_frontend_origin(client, monkeypatch):
    login(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/accounts/1/ws/orders", headers={"origin": FRONTEND}) as ws:
            ws.receive_json()
    monkeypatch.setattr(settings, "cors_origins", [FRONTEND])
    with client.websocket_connect("/api/accounts/1/ws/orders", headers={"origin": FRONTEND}) as ws:
        assert ws.receive_json()["type"] == "welcome"


# ---------- broker login from a frontend ----------

class FakeSession:
    def __init__(self, **kwargs):
        self.state = kwargs.get("state")

    def generate_authcode(self):
        return f"https://api-t1.fyers.in/api/v3/generate-authcode?state={self.state}"

    def set_token(self, code):
        self.code = code

    def generate_token(self):
        return {"s": "ok", "access_token": "tok"} if self.code == "good" else {"s": "error", "message": "bad code"}


@pytest.fixture
def fyers_ready(client, monkeypatch):
    import app.auth.router as auth_router

    monkeypatch.setattr(auth_router, "SessionModel", FakeSession)
    login(client)
    acct = client.post("/api/accounts", json={"broker": "fyers", "label": "F", "credentials": {
        "client_id": "XB12345-100", "secret_key": "secret"}}).json()["id"]
    return client, acct


def test_broker_login_url_as_json_and_frontend_redirect(fyers_ready, monkeypatch):
    c, acct = fyers_ready
    url = c.get(f"/api/accounts/{acct}/broker-login?redirect=false").json()["url"]
    state = url.split("state=")[1]
    monkeypatch.setattr(settings, "frontend_url", FRONTEND + "/settings")

    ok = c.get(f"/auth/fyers/callback?auth_code=good&state={state}", follow_redirects=False)
    assert ok.status_code == 303
    assert ok.headers["location"] == f"{FRONTEND}/settings?broker_login=fyers&account_id={acct}"

    state2 = c.get(f"/api/accounts/{acct}/broker-login?redirect=false").json()["url"].split("state=")[1]
    bad = c.get(f"/api/auth/fyers/callback?auth_code=nope&state={state2}", follow_redirects=False)
    assert bad.status_code == 303 and "broker_login_error=Fyers+login+failed" in bad.headers["location"]


def test_every_api_route_needs_auth(client):
    """Nothing under /api leaks without a session or token (except signing in itself)."""
    public = {"/api/auth/login", "/api/auth/logout", "/api/auth/me", "/api/auth/signup"}
    login(client)
    schema = client.get("/api/openapi.json").json()["paths"]
    anon = TestClient(app)
    for path, methods in schema.items():
        if path in public or not path.startswith("/api/"):  # /health is public on purpose
            continue
        for method in methods:
            url = path.replace("{instance_id}", "1").replace("{order_id}", "x").replace(
                "{position_id}", "1").replace("{account_id}", "1").replace("{token_id}", "1")
            r = anon.request(method.upper(), url)
            assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code}"
