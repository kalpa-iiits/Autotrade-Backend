"""FyersBroker tests with a fake REST model and fake sockets — no network, no real orders."""

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app.main
from app.brokers import MissingCredentials, OrderError
from app.brokers.fyers import FyersBroker
from app.core.config import settings
from app.orders.schemas import OrderStatus, OrderType, PlaceOrder, Side


class FakeModel:
    def __init__(self):
        self.placed = []
        self.cancel_ok = True

    def place_order(self, data):
        self.placed.append(data)
        if data["symbol"] == "NSE:BAD-EQ":
            return {"s": "error", "code": -50, "message": "Invalid symbol"}
        return {"s": "ok", "code": 1101, "id": "2410250001", "message": "Order submitted"}

    def cancel_order(self, data):
        if self.cancel_ok:
            return {"s": "ok", "id": data["id"]}
        return {"s": "error", "message": "Order already executed"}

    def orderbook(self):
        return {"s": "ok", "orderBook": [{
            "id": "2410250001", "symbol": "NSE:SBIN-EQ", "qty": 10, "type": 1, "side": 1,
            "status": 6, "limitPrice": 800.0, "tradedPrice": 0, "message": "",
        }]}

    def positions(self):
        return {"s": "ok", "netPositions": [
            {"symbol": "NSE:SBIN-EQ", "netQty": 10, "netAvg": 801.5},
            {"symbol": "NSE:TCS-EQ", "netQty": 0, "netAvg": 0},
        ]}

    def funds(self):
        return {"s": "ok", "fund_limit": [
            {"title": "Total Balance", "equityAmount": 50000.0},
            {"title": "Available Balance", "equityAmount": 42000.0},
        ]}


class FakeSocket:
    """Stands in for FyersDataSocket and FyersOrderSocket; records calls, fires callbacks."""

    instances: list["FakeSocket"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.subscribed = []
        self.unsubscribed = []
        self.closed = False
        FakeSocket.instances.append(self)

    def connect(self):
        self.kwargs["on_connect"]()

    def close_connection(self):
        self.closed = True

    def subscribe(self, **kwargs):
        self.subscribed.append(kwargs)

    def unsubscribe(self, **kwargs):
        self.unsubscribed.append(kwargs)

    def push(self, callback_name, message):
        # Real SDK callbacks arrive on a background thread.
        t = threading.Thread(target=self.kwargs[callback_name], args=(message,))
        t.start()
        t.join()


def make_broker():
    FakeSocket.instances = []
    model = FakeModel()
    broker = FyersBroker(
        client_id="XB12345-100", access_token="tok", log_dir="/tmp/autotrade-test-logs",
        model=model, data_socket_factory=FakeSocket, order_socket_factory=FakeSocket,
    )
    return broker, model


def test_requires_credentials():
    with pytest.raises(MissingCredentials):
        FyersBroker(client_id="", access_token="", model=FakeModel())


def test_place_order_maps_payload_and_tracks_socket_updates():
    async def run():
        broker, model = make_broker()
        updates = []

        async def on_update(order):
            updates.append(order)

        broker.on_order_update(on_update)
        await broker.start()
        data_sock, order_sock = FakeSocket.instances
        assert data_sock.kwargs["access_token"] == "XB12345-100:tok"
        assert order_sock.subscribed == [{"data_type": "OnOrders"}]

        order = await broker.place_order(PlaceOrder(
            action="place_order", symbol="NSE:SBIN-EQ", side=Side.BUY, quantity=10,
            order_type=OrderType.LIMIT, limit_price=800.0,
        ))
        assert order.id == "2410250001" and order.status == OrderStatus.OPEN
        sent = model.placed[0]
        assert (sent["type"], sent["side"], sent["qty"], sent["limitPrice"]) == (1, 1, 10, 800.0)
        assert sent["productType"] == "INTRADAY"

        fill = {"s": "ok", "orders": {
            "id": "2410250001", "symbol": "NSE:SBIN-EQ", "qty": 10, "type": 1, "side": 1,
            "status": 2, "limitPrice": 800.0, "tradedPrice": 799.9, "message": "TRADE CONFIRMED",
        }}
        order_sock.push("on_orders", fill)
        order_sock.push("on_orders", fill)  # duplicate message must not re-broadcast
        await asyncio.sleep(0.05)

        assert [u.status for u in updates] == [OrderStatus.OPEN, OrderStatus.FILLED]
        assert updates[-1].filled_price == 799.9

        await broker.stop()
        assert data_sock.closed and order_sock.closed

    asyncio.run(run())


def test_rejected_and_cancel():
    async def run():
        broker, model = make_broker()
        await broker.start()

        bad = await broker.place_order(PlaceOrder(
            action="place_order", symbol="NSE:BAD-EQ", side=Side.SELL, quantity=1,
        ))
        assert bad.status == OrderStatus.REJECTED and bad.reason == "Invalid symbol"

        cancelled = await broker.cancel_order("2410250001")  # found via orderbook
        assert cancelled.status == OrderStatus.CANCELLED

        model.cancel_ok = False
        with pytest.raises(OrderError, match="already executed"):
            await broker.cancel_order("2410250001")

    asyncio.run(run())


def test_ticks_subscriptions_and_account_data():
    async def run():
        broker, _ = make_broker()
        ticks = []

        async def on_tick(symbol, price):
            ticks.append((symbol, price))

        broker.on_tick(on_tick)
        await broker.start()
        data_sock = FakeSocket.instances[0]

        await broker.subscribe(["NSE:SBIN-EQ"])
        await broker.subscribe(["NSE:SBIN-EQ"])  # already subscribed: no second call
        assert data_sock.subscribed == [{"symbols": ["NSE:SBIN-EQ"], "data_type": "SymbolUpdate"}]

        data_sock.push("on_message", {"symbol": "NSE:SBIN-EQ", "ltp": 812.35, "type": "sf"})
        data_sock.push("on_message", {"type": "cn", "message": "Authentication done"})
        await asyncio.sleep(0.05)
        assert ticks == [("NSE:SBIN-EQ", 812.35)]

        await broker.unsubscribe(["NSE:SBIN-EQ"])
        assert data_sock.unsubscribed[0]["symbols"] == ["NSE:SBIN-EQ"]

        positions = await broker.get_positions()
        assert [(p.symbol, p.quantity) for p in positions] == [("NSE:SBIN-EQ", 10)]
        assert (await broker.get_funds())["cash"] == 42000.0

    asyncio.run(run())


def test_apps_share_the_fyers_broker(monkeypatch):
    monkeypatch.setattr(settings, "broker", "fyers")
    monkeypatch.setattr(
        "app.core.broker_service.create_broker", lambda *a, **kw: make_broker()[0]
    )
    with TestClient(app.main.app) as client:
        with client.websocket_connect(f"/api/ws/tickers?token={settings.api_token}") as tickers, \
             client.websocket_connect(f"/api/ws/orders?token={settings.api_token}") as orders:
            assert tickers.receive_json()["broker"] == "fyers"
            orders.receive_json()

            tickers.send_json({"action": "subscribe", "symbols": ["NSE:SBIN-EQ"]})
            assert tickers.receive_json()["symbols"] == ["NSE:SBIN-EQ"]

            orders.send_json({"action": "place_order", "symbol": "NSE:SBIN-EQ", "side": "BUY",
                              "quantity": 1})
            msgs = [orders.receive_json(), orders.receive_json()]
            assert {m["type"] for m in msgs} == {"order_update", "ack"}

            # Last ticker client leaves, so the upstream Fyers subscription is dropped.
            tickers.close()
            data_sock = FakeSocket.instances[0]
            deadline = time.monotonic() + 2
            while not data_sock.unsubscribed and time.monotonic() < deadline:
                time.sleep(0.01)  # server-side cleanup runs on the app's loop
            assert data_sock.unsubscribed[0]["symbols"] == ["NSE:SBIN-EQ"]


def test_orders_report_not_logged_in(monkeypatch):
    monkeypatch.setattr(settings, "broker", "fyers")
    monkeypatch.setattr(settings, "fyers_client_id", "")
    with TestClient(app.main.app) as client:
        assert client.get("/health").json()["connected"] is False
        with client.websocket_connect(f"/api/ws/orders?token={settings.api_token}") as ws:
            ws.receive_json()
            ws.send_json({"action": "get_funds"})
            assert "/api/auth/fyers/login" in ws.receive_json()["error"]


class SDKOrderSocket(FakeSocket):
    """Like the real FyersOrderSocket: close_connection closes the socket, then joins a
    thread that only exists if keep_running() was called."""

    def close_connection(self):
        self.closed = True
        None.join()  # AttributeError, as in the SDK


def test_stop_survives_sdk_close_error():
    async def run():
        FakeSocket.instances = []
        broker = FyersBroker(client_id="XB12345-100", access_token="tok", log_dir="/tmp/autotrade-test-logs",
                             model=FakeModel(), data_socket_factory=FakeSocket,
                             order_socket_factory=SDKOrderSocket)
        await broker.start()
        await broker.stop()  # must not raise
        data_sock, order_sock = FakeSocket.instances
        assert data_sock.closed and order_sock.closed

    asyncio.run(run())


def test_login_callback_restarts_running_fyers_broker(client, monkeypatch):
    """Regression: logging in again while Fyers is the active broker gave a 500."""
    import app.auth.router as auth_router
    from app.core.broker_service import BrokerService

    built = []

    def fake_create(_settings, access_token=None):
        FakeSocket.instances = []
        b = FyersBroker(client_id="XB12345-100", access_token=access_token or "old",
                        log_dir="/tmp/autotrade-test-logs", model=FakeModel(),
                        data_socket_factory=FakeSocket, order_socket_factory=SDKOrderSocket)
        built.append(b)
        return b

    class FakeSession:
        def __init__(self, **kw): pass
        def generate_authcode(self): return "https://fyers.example/login"
        def set_token(self, code): pass
        def generate_token(self): return {"s": "ok", "access_token": "new-token"}

    monkeypatch.setattr(settings, "broker", "fyers")
    monkeypatch.setattr(settings, "fyers_client_id", "XB12345-100")
    monkeypatch.setattr(settings, "fyers_secret_key", "secret")
    monkeypatch.setattr(settings, "fyers_access_token", "old")
    monkeypatch.setattr("app.core.broker_service.create_broker", fake_create)
    monkeypatch.setattr(auth_router, "SessionModel", FakeSession)

    brokers: BrokerService = client.app.state.brokers
    monkeypatch.setattr(brokers, "name", "fyers")
    client.portal.call(brokers.restart)  # now running a Fyers broker, like the real server
    old = brokers.broker

    login = client.get("/api/auth/fyers/login", headers={"Authorization": f"Bearer {settings.api_token}"},
                       follow_redirects=False)
    assert login.status_code == 307
    state = next(iter(client.app.state.login_states))  # FakeSession doesn't put it in the URL
    r = client.get(f"/api/auth/fyers/callback?s=ok&code=200&auth_code=abc&state={state}")
    assert r.status_code == 200, r.text
    assert brokers.broker is not old and brokers.connected
    assert brokers.broker._socket_token.endswith(":new-token")


def test_fill_reported_before_place_order_returns_is_not_undone():
    """A market order can fill (order socket) before Fyers answers the REST call. The reply's
    "OPEN, nothing filled" is older news and must not overwrite the fill."""
    async def run():
        broker, model = make_broker()
        updates = []

        async def on_update(order):
            updates.append(order)

        broker.on_order_update(on_update)
        await broker.start()
        order_sock = FakeSocket.instances[1]
        place = model.place_order

        def place_and_fill_first(data):
            order_sock.kwargs["on_orders"]({"s": "ok", "orders": {
                "id": "2410250001", "symbol": "NSE:SBIN-EQ", "qty": 10, "type": 2, "side": 1,
                "status": 2, "filledQty": 10, "tradedPrice": 801.0, "orderTag": "1:abc123",
            }})
            time.sleep(0.05)  # let the event loop handle the socket message first
            return place(data)

        model.place_order = place_and_fill_first
        order = await broker.place_order(PlaceOrder(
            action="place_order", symbol="NSE:SBIN-EQ", side=Side.BUY, quantity=10,
            client_order_id="abc123",
        ))
        assert (order.status, order.filled_quantity, order.client_order_id) == (OrderStatus.FILLED, 10, "abc123")
        assert [(u.status, u.filled_quantity) for u in updates] == [(OrderStatus.FILLED, 10)]
        assert model.placed[0]["orderTag"] == "abc123"

        # Once finished, an order never goes back to open.
        order_sock.push("on_orders", {"s": "ok", "orders": {
            "id": "2410250001", "symbol": "NSE:SBIN-EQ", "qty": 10, "type": 2, "side": 1,
            "status": 6, "filledQty": 0,
        }})
        await asyncio.sleep(0.05)
        assert len(updates) == 1
        await broker.stop()

    asyncio.run(run())


def test_order_tag_maps_to_client_order_id():
    from app.brokers.fyers import _order_from_fyers

    row = {"id": "1", "symbol": "NSE:SBIN-EQ", "qty": 1, "side": 1, "status": 6}
    assert _order_from_fyers(row | {"orderTag": "1:abc123"}).client_order_id == "abc123"
    assert _order_from_fyers(row | {"orderTag": "abc123"}).client_order_id == "abc123"
    assert _order_from_fyers(row | {"orderTag": "2:Untagged"}).client_order_id is None
    assert _order_from_fyers(row).client_order_id is None
