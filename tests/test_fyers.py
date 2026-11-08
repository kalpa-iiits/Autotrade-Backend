"""FyersBroker tests with a fake REST model and fake sockets — no network, no real orders."""

import asyncio
import threading
import time
from datetime import date, timedelta

import pytest

from app.brokers import MissingCredentials, OrderError
from app.brokers.fyers import FyersBroker
from tests.conftest import AUTH, TOKEN
from app.orders.schemas import OrderStatus, OrderType, PlaceOrder, Side


class FakeModel:
    def __init__(self):
        self.placed = []
        self.cancel_ok = True
        self.modified = []
        self.history_requests = []

    def place_order(self, data):
        self.placed.append(data)
        if data["symbol"] == "NSE:BAD-EQ":
            return {"s": "error", "code": -50, "message": "Invalid symbol"}
        return {"s": "ok", "code": 1101, "id": "2410250001", "message": "Order submitted"}

    def modify_order(self, data):
        self.modified.append(data)
        if data["id"] == "done":
            return {"s": "error", "message": "Order already executed"}
        return {"s": "ok", "id": data["id"]}

    def depth(self, data):
        if data["symbol"] == "NSE:EMPTY-EQ":
            return {"s": "ok", "d": {}}
        return {"s": "ok", "d": {data["symbol"]: {
            "bids": [{"price": 799.5, "volume": 10}, {"price": 799.0, "volume": 4}],
            "ask": [{"price": 0, "volume": 0}, {"price": 800.5, "volume": 7}],  # an empty level first
        }}}

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

    def history(self, data):
        self.history_requests.append(data)
        return {"s": "ok", "candles": []}


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
        for bad in (None, 0, "812", float("nan")):  # no usable price: dropped, not passed on
            data_sock.push("on_message", {"symbol": "NSE:SBIN-EQ", "ltp": bad, "type": "sf"})
        await asyncio.sleep(0.05)
        assert ticks == [("NSE:SBIN-EQ", 812.35)]

        await broker.unsubscribe(["NSE:SBIN-EQ"])
        assert data_sock.unsubscribed[0]["symbols"] == ["NSE:SBIN-EQ"]

        positions = await broker.get_positions()
        assert [(p.symbol, p.quantity) for p in positions] == [("NSE:SBIN-EQ", 10)]
        assert (await broker.get_funds())["cash"] == 42000.0

    asyncio.run(run())


def test_long_history_ranges_are_chunked_for_fyers_limit():
    async def run():
        broker, model = make_broker()
        assert await broker.get_history("NSE:NIFTY50-INDEX", "D", date(2023, 1, 1), date(2025, 1, 5)) == []
        assert len(model.history_requests) == 3
        ranges = [(date.fromisoformat(r["range_from"]), date.fromisoformat(r["range_to"]))
                  for r in model.history_requests]
        assert ranges[0][0] == date(2023, 1, 1) and ranges[-1][1] == date(2025, 1, 5)
        assert all((end - start).days <= 365 for start, end in ranges)
        assert all(ranges[i][1] + timedelta(days=1) == ranges[i + 1][0]
                   for i in range(len(ranges) - 1))

    asyncio.run(run())


def add_fyers_account(client):
    r = client.post("/api/accounts", json={"broker": "fyers", "label": "Fyers", "credentials": {
        "client_id": "XB12345-100", "secret_key": "secret"}}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_apps_share_the_accounts_fyers_broker(client, monkeypatch):
    monkeypatch.setattr("app.accounts.registry.create_broker", lambda *a, **kw: make_broker()[0])
    acct = add_fyers_account(client)
    base = f"/api/accounts/{acct}/ws"
    with client.websocket_connect(f"{base}/tickers?token={TOKEN}") as tickers, \
         client.websocket_connect(f"{base}/orders?token={TOKEN}") as orders:
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


def test_orders_report_not_logged_in(client, monkeypatch):
    from app.brokers import create_broker

    monkeypatch.setattr("app.accounts.registry.create_broker", create_broker)  # no token: no network
    acct = add_fyers_account(client)
    assert client.get(f"/api/accounts/{acct}", headers=AUTH).json()["connection"]["broker"]["connected"] is False
    with client.websocket_connect(f"/api/accounts/{acct}/ws/orders?token={TOKEN}") as ws:
        ws.receive_json()
        ws.send_json({"action": "get_funds"})
        assert "broker-login" in ws.receive_json()["error"]


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
    """Regression: logging in again while the account's Fyers broker runs gave a 500."""
    import app.auth.router as auth_router

    def fake_create(_settings, broker, credentials, options, access_token=None, account_id=None):
        FakeSocket.instances = []
        return FyersBroker(client_id=credentials["client_id"], access_token=access_token or "old",
                           log_dir="/tmp/autotrade-test-logs", model=FakeModel(),
                           data_socket_factory=FakeSocket, order_socket_factory=SDKOrderSocket)

    class FakeSession:
        def __init__(self, **kw): pass
        def generate_authcode(self): return "https://fyers.example/login"
        def set_token(self, code): pass
        def generate_token(self): return {"s": "ok", "access_token": "new-token"}

    monkeypatch.setattr("app.accounts.registry.create_broker", fake_create)
    monkeypatch.setattr(auth_router, "SessionModel", FakeSession)
    acct = add_fyers_account(client)
    service = client.app.state.accounts.get(acct).service
    old = service.broker
    assert old is not None  # running, like the real server after an earlier login

    login = client.get(f"/api/accounts/{acct}/broker-login", headers=AUTH, follow_redirects=False)
    assert login.status_code == 307
    state = next(iter(client.app.state.login_states))  # FakeSession doesn't put it in the URL
    r = client.get(f"/api/auth/fyers/callback?s=ok&code=200&auth_code=abc&state={state}")
    assert r.status_code == 200, r.text
    assert service.broker is not old and service.connected
    assert service.broker._socket_token.endswith(":new-token")


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


def test_stop_orders_and_product_map_to_fyers():
    from app.brokers.fyers import _order_from_fyers
    from app.orders.schemas import Product

    async def run():
        broker, model = make_broker()
        await broker.place_order(PlaceOrder(
            action="place_order", symbol="NSE:NIFTY26NOVFUT", side=Side.SELL, quantity=75,
            order_type=OrderType.SL, trigger_price=25388.0, limit_price=25380.0, product=Product.DELIVERY,
        ))
        await broker.place_order(PlaceOrder(
            action="place_order", symbol="NSE:SBIN-EQ", side=Side.SELL, quantity=1,
            order_type=OrderType.SL_M, trigger_price=790.0, product=Product.DELIVERY,
        ))
        sl, slm = model.placed
        assert (sl["type"], sl["stopPrice"], sl["limitPrice"], sl["productType"]) == (4, 25388.0, 25380.0, "MARGIN")
        assert (slm["type"], slm["stopPrice"], slm["limitPrice"], slm["productType"]) == (3, 790.0, 0, "CNC")

    asyncio.run(run())
    order = _order_from_fyers({"id": "1", "symbol": "NSE:SBIN-EQ", "qty": 1, "type": 3, "side": -1,
                               "status": 6, "limitPrice": 0, "stopPrice": 790.0})
    assert (order.order_type, order.trigger_price, order.limit_price) == (OrderType.SL_M, 790.0, None)


def test_market_depth_and_modify():
    async def run():
        broker, model = make_broker()
        await broker.start()
        depth = await broker.get_depth("NSE:SBIN-EQ")
        assert (depth.bid, depth.ask) == (799.5, 800.5)  # best of each side, skipping empty levels
        assert await broker.get_depth("NSE:EMPTY-EQ") is None

        moved = []

        async def listener(order):
            moved.append(order)

        broker.on_order_update(listener)
        order = await broker.modify_order("2410250001", 801.0)  # found via orderbook
        assert model.modified == [{"id": "2410250001", "type": 1, "limitPrice": 801.0}]
        assert order.limit_price == 801.0 and order.status == OrderStatus.OPEN
        assert [o.limit_price for o in moved] == [801.0]  # broadcast, so the history records it
        with pytest.raises(OrderError, match="already executed"):
            await broker.modify_order("done", 801.0)

    asyncio.run(run())


def test_order_socket_closes_without_sdk_attribute_error(tmp_path):
    """The SDK's close joins threads that don't exist unless keep_running() was called."""
    import pytest
    from fyers_apiv3.FyersWebsocket.order_ws import FyersOrderSocket

    from app.brokers._fyers_compat import prepare_order_socket_close

    class FakeWs:
        closed = False

        def close(self):
            self.closed = True

    def opened_socket():
        sock = FyersOrderSocket(access_token="test-app:test-token", write_to_file=False, log_path=str(tmp_path))
        ws = FakeWs()
        sock._FyersOrderSocket__ws_object = ws  # as if connected; no real network
        return sock, ws

    sock, _ = opened_socket()
    with pytest.raises(AttributeError):  # the bug, as the SDK ships it
        sock.close_connection()

    sock, ws = opened_socket()
    prepare_order_socket_close(sock)
    sock.close_connection()
    assert ws.closed and sock._FyersOrderSocket__ws_object is None


def test_socket_error_clears_when_that_socket_reconnects():
    """A transient drop (common off-hours) must not leave a stale banner after the SDK reconnects,
    and one socket coming back must not hide the other's still-open error."""
    async def run():
        broker, _ = make_broker()
        await broker.start()
        data_sock, order_sock = FakeSocket.instances

        order_sock.kwargs["on_error"]("Connection to remote host was lost.")
        data_sock.kwargs["on_error"]("Connection to remote host was lost.")
        assert "order socket" in broker.stream_error and "data socket" in broker.stream_error

        data_sock.kwargs["on_connect"]()  # data came back; order still down
        assert broker.stream_error == "Fyers order socket: Connection to remote host was lost."

        order_sock.kwargs["on_connect"]()  # the SDK reconnected the order socket
        assert broker.stream_error is None
        await broker.stop()

    asyncio.run(run())


class FlakySocket(FakeSocket):
    """FakeSocket with the SDK's connection state: starts up, can be dropped, and can be
    put in the state the SDK leaves it in after it has used up its retries."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.up = True
        self.connects = 0
        self.reconnect_attempts = 0
        self.reconnect_delay = 0
        self.max_reconnect_attempts = kwargs.get("reconnect_retry", 5)
        self.restart_flag = True

    def is_connected(self):
        return self.up

    def connect(self):
        self.connects += 1
        if self.connects > 1:
            self.up = True  # this attempt succeeds
        super().connect()


def make_flaky_broker():
    FakeSocket.instances = []
    return FyersBroker(
        client_id="XB12345-100", access_token="tok", log_dir="/tmp/autotrade-test-logs",
        model=FakeModel(), data_socket_factory=FlakySocket, order_socket_factory=FlakySocket,
        watchdog_interval=0.01,
    )


def test_sockets_ask_sdk_for_max_retries():
    async def run():
        broker = make_flaky_broker()
        await broker.start()
        assert all(s.kwargs["reconnect_retry"] == 50 for s in FakeSocket.instances)
        await broker.stop()

    asyncio.run(run())


def test_connect_callback_on_a_failed_attempt_keeps_the_error():
    async def run():
        broker = make_flaky_broker()
        broker._watchdog_interval = 3600  # keep the watchdog out of this one
        await broker.start()
        _, order_sock = FakeSocket.instances
        order_sock.up = False
        order_sock.kwargs["on_error"]("Connection to remote host was lost.")
        order_sock.kwargs["on_connect"]()  # SDK fires this ~2s into a retry that is failing
        assert broker.stream_error == "Fyers order socket: Connection to remote host was lost."
        await broker.stop()

    asyncio.run(run())


def test_watchdog_restarts_a_socket_the_sdk_gave_up_on():
    async def run():
        broker = make_flaky_broker()
        await broker.start()
        data_sock, order_sock = FakeSocket.instances
        order_sock.up = False
        order_sock.reconnect_attempts = order_sock.max_reconnect_attempts  # SDK abandoned it
        order_sock.reconnect_delay = 50

        for _ in range(200):
            await asyncio.sleep(0.01)
            if order_sock.connects > 1:
                break
        assert order_sock.connects == 2 and order_sock.reconnect_attempts == 0
        assert order_sock.reconnect_delay == 0
        assert data_sock.connects == 1  # the healthy socket is left alone

        for _ in range(100):
            await asyncio.sleep(0.01)
            if broker.stream_error is None:
                break
        assert broker.stream_error is None
        await broker.stop()

    asyncio.run(run())


def test_watchdog_reports_a_socket_still_retrying_without_restarting_it():
    async def run():
        broker = make_flaky_broker()
        await broker.start()
        _, order_sock = FakeSocket.instances
        order_sock.up = False  # down, but the SDK still has retries left
        for _ in range(100):
            await asyncio.sleep(0.01)
            if broker.stream_error:
                break
        assert broker.stream_error == "Fyers order socket: disconnected, reconnecting"
        assert order_sock.connects == 1
        await broker.stop()

    asyncio.run(run())


def test_rejected_login_stops_retrying_and_says_log_in_again():
    async def run():
        broker = make_flaky_broker()
        await broker.start()
        _, order_sock = FakeSocket.instances
        order_sock.up = False
        order_sock.kwargs["on_error"]("Handshake status 403 Forbidden -+-+- {'content-length': '0'}")
        assert order_sock.restart_flag is False  # the SDK will not retry this token
        assert "log in again" in broker.stream_error

        order_sock.reconnect_attempts = order_sock.max_reconnect_attempts  # SDK done
        await asyncio.sleep(0.1)  # many watchdog checks
        assert order_sock.connects == 1  # nor does the watchdog
        assert "log in again" in broker.stream_error
        await broker.stop()

    asyncio.run(run())


def test_order_book_checked_once_per_real_reconnect_not_per_attempt():
    async def run():
        broker = make_flaky_broker()
        broker._watchdog_interval = 3600  # keep the watchdog out of this one
        resyncs = []

        async def on_reconnect():
            resyncs.append(1)

        broker.on_reconnect(on_reconnect)
        await broker.start()
        _, order_sock = FakeSocket.instances
        order_sock.up = False
        for _ in range(5):  # failed SDK attempts each fire on_connect
            order_sock.kwargs["on_connect"]()
        await asyncio.sleep(0.05)
        assert resyncs == []

        order_sock.up = True
        order_sock.kwargs["on_connect"]()  # this attempt worked
        await asyncio.sleep(0.05)
        assert resyncs == [1]
        await broker.stop()

    asyncio.run(run())


def test_order_book_checked_often_while_order_socket_is_down_then_backs_off(monkeypatch):
    import app.brokers.fyers as fyers_mod
    monkeypatch.setattr(fyers_mod, "SOCKET_FAST_RESYNC_CHECKS", 3)

    async def run():
        broker = make_flaky_broker()
        resyncs = []

        async def on_reconnect():
            resyncs.append(1)

        broker.on_reconnect(on_reconnect)
        await broker.start()
        _, order_sock = FakeSocket.instances
        order_sock.up = False
        await asyncio.sleep(0.2)  # ~20 watchdog checks, SDK still retrying
        assert len(resyncs) == 3  # only the first few checks of the outage

        order_sock.up = True
        await asyncio.sleep(0.05)
        assert len(resyncs) == 4  # plus one when it is really back
        await broker.stop()

    asyncio.run(run())
