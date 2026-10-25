"""ZerodhaBroker tests with a fake Kite REST client and a fake WebSocket — no network."""

import asyncio
import json
import struct
import time

import pytest
from fastapi.testclient import TestClient
from kiteconnect.exceptions import InputException, OrderException

import app.main
from app.brokers import MissingCredentials, OrderError
from app.brokers.zerodha import ZerodhaBroker, parse_ticks
from app.core.config import settings
from app.orders.schemas import OrderStatus, OrderType, PlaceOrder, Side

SBIN = 779521  # NSE segment (token & 0xff == 1)


def tick_frame(*ticks):
    """Build a Kite binary frame of LTP packets: [(token, price_in_rupees)]."""
    body = b"".join(struct.pack(">HII", 8, token, round(price * 100)) for token, price in ticks)
    return struct.pack(">H", len(ticks)) + body


class FakeKite:
    def __init__(self):
        self.placed = []

    def ltp(self, symbols):
        known = {"NSE:SBIN": SBIN}
        return {s: {"instrument_token": known[s], "last_price": 800.0} for s in symbols if s in known}

    def place_order(self, **params):
        if params["tradingsymbol"] == "BAD":
            raise InputException("Invalid `tradingsymbol`.")
        self.placed.append(params)
        return "250101000000001"

    def cancel_order(self, variety, order_id):
        if order_id == "done":
            raise OrderException("Order cannot be cancelled as it is being processed.")
        return order_id

    def orders(self):
        return [{"order_id": "250101000000001", "exchange": "NSE", "tradingsymbol": "SBIN",
                 "transaction_type": "BUY", "quantity": 5, "order_type": "LIMIT", "price": 790.0,
                 "status": "OPEN", "average_price": 0, "status_message": None}]

    def positions(self):
        return {"net": [
            {"exchange": "NSE", "tradingsymbol": "SBIN", "quantity": 5, "average_price": 801.2},
            {"exchange": "NSE", "tradingsymbol": "TCS", "quantity": 0, "average_price": 0},
        ], "day": []}

    def margins(self, segment):
        return {"net": 25000.5, "available": {"cash": 30000.0, "live_balance": 25000.5}}


class FakeWS:
    def __init__(self):
        self.sent = []
        self.frames: asyncio.Queue = asyncio.Queue()

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self):
        await self.frames.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.frames.get()
        if frame is None:
            raise StopAsyncIteration
        return frame


class FakeConnect:
    """Mimics `async for ws in websockets.connect(url)`: yields one socket, then idles."""

    def __init__(self):
        self.ws = None
        self.url = None

    def __call__(self, url):
        self.url = url
        return self._gen()

    async def _gen(self):
        self.ws = FakeWS()
        yield self.ws
        await asyncio.Event().wait()


def make_broker():
    kite, connect = FakeKite(), FakeConnect()
    broker = ZerodhaBroker(api_key="kitekey", access_token="tok", kite=kite, connect=connect)
    return broker, kite, connect


async def until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def test_requires_credentials():
    with pytest.raises(MissingCredentials):
        ZerodhaBroker(api_key="", access_token="", kite=FakeKite())


def test_parse_ticks():
    assert parse_ticks(b"\x00") == []  # heartbeat
    assert parse_ticks(tick_frame((SBIN, 812.35), (256265, 24350.5))) == [
        (SBIN, 812.35), (256265, 24350.5)
    ]
    # Currency segment (cds = 3) prices are scaled by 1e7, not 100.
    cds = (1 << 8) | 3
    frame = struct.pack(">HHII", 1, 8, cds, 832_500_000)
    assert parse_ticks(frame) == [(cds, 83.25)]


def test_orders_and_order_updates():
    async def run():
        broker, kite, connect = make_broker()
        updates = []

        async def on_update(order):
            updates.append(order)

        broker.on_order_update(on_update)
        await broker.start()
        await until(lambda: connect.ws is not None)
        assert "api_key=kitekey" in connect.url and "access_token=tok" in connect.url

        order = await broker.place_order(PlaceOrder(
            action="place_order", symbol="NSE:SBIN", side=Side.BUY, quantity=5,
            order_type=OrderType.LIMIT, limit_price=790.0,
        ))
        assert order.id == "250101000000001" and order.status == OrderStatus.OPEN
        sent = kite.placed[0]
        assert (sent["exchange"], sent["tradingsymbol"], sent["transaction_type"]) == ("NSE", "SBIN", "BUY")
        assert (sent["order_type"], sent["price"], sent["product"]) == ("LIMIT", 790.0, "MIS")

        assert "tag" not in sent  # no client order id given to the broker directly

        await broker.place_order(PlaceOrder(
            action="place_order", symbol="NSE:SBIN", side=Side.SELL, quantity=1,
            client_order_id="abc123",
        ))
        assert kite.placed[1]["market_protection"] == -1 and "price" not in kite.placed[1]
        assert kite.placed[1]["tag"] == "abc123"

        fill = json.dumps({"type": "order", "data": {
            "order_id": "250101000000001", "exchange": "NSE", "tradingsymbol": "SBIN",
            "transaction_type": "BUY", "quantity": 5, "order_type": "LIMIT", "price": 790.0,
            "status": "COMPLETE", "average_price": 789.95, "status_message": None,
        }})
        await connect.ws.frames.put(fill)
        await connect.ws.frames.put(fill)  # duplicate must not re-broadcast
        await until(lambda: updates[-1].status == OrderStatus.FILLED)
        await asyncio.sleep(0.02)
        assert [u.status for u in updates if u.id == "250101000000001"] == [
            OrderStatus.OPEN, OrderStatus.FILLED
        ]
        assert updates[-1].filled_price == 789.95

        await broker.stop()

    asyncio.run(run())


def test_rejections_cancel_and_account():
    async def run():
        broker, kite, _ = make_broker()
        await broker.start()

        bad = await broker.place_order(PlaceOrder(
            action="place_order", symbol="NSE:BAD", side=Side.BUY, quantity=1,
        ))
        assert bad.status == OrderStatus.REJECTED and "tradingsymbol" in bad.reason

        with pytest.raises(OrderError, match="EXCHANGE:TRADINGSYMBOL"):
            await broker.place_order(PlaceOrder(
                action="place_order", symbol="SBIN", side=Side.BUY, quantity=1,
            ))

        cancelled = await broker.cancel_order("250101000000001")  # found via orders()
        assert cancelled.status == OrderStatus.CANCELLED
        with pytest.raises(OrderError, match="being processed"):
            await broker.cancel_order("done")

        positions = await broker.get_positions()
        assert [(p.symbol, p.quantity, p.avg_price) for p in positions] == [("NSE:SBIN", 5, 801.2)]
        assert (await broker.get_funds())["cash"] == 25000.5
        await broker.stop()

    asyncio.run(run())


def test_ticks_and_subscriptions():
    async def run():
        broker, _, connect = make_broker()
        ticks = []

        async def on_tick(symbol, price):
            ticks.append((symbol, price))

        broker.on_tick(on_tick)
        await broker.start()
        await until(lambda: connect.ws is not None)

        await broker.subscribe(["NSE:SBIN"])
        await broker.subscribe(["NSE:SBIN"])  # already subscribed: nothing sent
        assert connect.ws.sent == [{"a": "subscribe", "v": [SBIN]}, {"a": "mode", "v": ["ltp", [SBIN]]}]

        with pytest.raises(OrderError, match="Unknown symbol"):
            await broker.subscribe(["NSE:NOPE"])

        await connect.ws.frames.put(tick_frame((SBIN, 812.35), (123, 1.0)))  # 123 not subscribed
        await until(lambda: ticks)
        assert ticks == [("NSE:SBIN", 812.35)]

        await broker.unsubscribe(["NSE:SBIN"])
        assert connect.ws.sent[-1] == {"a": "unsubscribe", "v": [SBIN]}
        await broker.stop()

    asyncio.run(run())


def test_apps_share_the_zerodha_broker(monkeypatch):
    holder = {}

    def fake_create(*a, **kw):
        holder["broker"], _, holder["connect"] = make_broker()
        return holder["broker"]

    monkeypatch.setattr(settings, "broker", "zerodha")
    monkeypatch.setattr("app.core.broker_service.create_broker", fake_create)
    with TestClient(app.main.app) as client:
        with client.websocket_connect(f"/api/ws/tickers?token={settings.api_token}") as tickers, \
             client.websocket_connect(f"/api/ws/orders?token={settings.api_token}") as orders:
            assert tickers.receive_json()["broker"] == "zerodha"
            orders.receive_json()

            tickers.send_json({"action": "subscribe", "symbols": ["NSE:SBIN"]})
            assert tickers.receive_json()["symbols"] == ["NSE:SBIN"]

            # Push a tick through the fake Kite socket on the app's event loop.
            ws = holder["connect"].ws
            client.portal.call(ws.frames.put, tick_frame((SBIN, 805.5)))
            assert tickers.receive_json() == {"type": "tick", "symbol": "NSE:SBIN", "ltp": 805.5}

            orders.send_json({"action": "place_order", "symbol": "NSE:SBIN", "side": "BUY",
                              "quantity": 1})
            msgs = [orders.receive_json(), orders.receive_json()]
            assert {m["type"] for m in msgs} == {"order_update", "ack"}
