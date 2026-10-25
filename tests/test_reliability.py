"""Reliability guarantees: reconciliation with the broker, isolation of slow consumers, the
single-process lock, rate limiting, ledger failures that are retried instead of dropped,
exact money, and /health."""

import asyncio
import sqlite3
import threading
import time
from decimal import Decimal

import pytest
from alembic import command
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import create_engine, select

import app.main
from app.brokers import zerodha
from app.brokers.zerodha import ZerodhaBroker
from app.core import db
from app.core.config import settings
from app.core.connections import ConnectionManager
from app.core.db import alembic_config
from app.core.instance_lock import AlreadyRunning, InstanceLock, lock_path
from app.core.ratelimit import RateLimiter
from app.orders.models import OrderRecord
from app.orders.schemas import Order, OrderStatus, OrderType, Side
from app.strategies.base import Strategy, Tick
from app.trades.models import Fill
from tests.conftest import TOKEN, run
from tests.test_fyers import FakeSocket, make_broker
from tests.test_strategies import SCRIPT, SYM, deploy, dep, signals, start_strategy, tick  # noqa: F401
from tests.test_zerodha import FakeKite, FakeWS

AUTH = {"Authorization": f"Bearer {TOKEN}"}


def place_limit(client, price=1.0, qty=2):
    r = client.post("/api/accounts/1/orders", json={"symbol": SYM, "side": "BUY", "quantity": qty,
                                         "order_type": "LIMIT", "limit_price": price}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


# ---------- reconciliation ----------

def test_resync_applies_updates_the_socket_never_delivered(client, monkeypatch):
    brokers = client.app.state.accounts.get(1).service
    order = place_limit(client)  # far below the price: stays open
    # The broker fills it, but the update is lost (socket down): only its order book knows.
    paper_order = brokers.broker.orders[order["id"]]
    paper_order.status, paper_order.filled_quantity, paper_order.filled_price = OrderStatus.FILLED, 2, 1.0

    assert run(client, brokers.resync) == 1
    history = client.get("/api/accounts/1/orders/history", headers=AUTH).json()
    assert (history[0]["status"], history[0]["filled_quantity"]) == ("FILLED", 2)
    assert client.get("/api/trades/open", headers=AUTH).json()["totals"]["count"] == 1  # fill recorded
    assert run(client, brokers.resync) == 0  # nothing missed any more
    health = client.get("/api/accounts/1", headers=AUTH).json()["connection"]["broker"]
    assert health["resynced_orders"] == 1 and health["last_resync_at"] is not None


def test_resync_unblocks_a_strategy_waiting_on_a_lost_fill(deploy, monkeypatch):
    monkeypatch.setattr(settings, "strategies_live_enabled", True)
    c = deploy(dep("a", mode="live"))
    inst = start_strategy(c, "a")
    runner, brokers = c.app.state.strategy_runner, c.app.state.accounts.get(1).service
    order = place_limit(c, qty=1)
    # As if the strategy had placed it: it owns the order and waits for it to finish.
    running = runner.running[inst["id"]]
    running.pending_order_id, runner._order_owner[(1, order["id"])] = order["id"], inst["id"]
    SCRIPT.append(("buy",))
    tick(c)
    assert signals(c, inst["id"])[0]["outcome"] == "blocked"  # "order ... is still open"

    paper_order = brokers.broker.orders[order["id"]]
    paper_order.status, paper_order.filled_quantity, paper_order.filled_price = OrderStatus.FILLED, 1, 1.0
    run(c, brokers.resync)
    assert (running.pending_order_id, running.position) == (None, 1)


def test_zerodha_reconnect_triggers_resync(monkeypatch):
    monkeypatch.setattr(zerodha, "RECONNECT_DELAY", 0)

    class TwoConnections:
        def __init__(self):
            self.sockets = [FakeWS(), FakeWS()]

        def __call__(self, url):
            return self._connections()

        async def _connections(self):
            for ws in self.sockets:
                yield ws
            await asyncio.Event().wait()

    async def go():
        connect = TwoConnections()
        broker = ZerodhaBroker(api_key="k", access_token="t", kite=FakeKite(), connect=connect)
        reconnects = []

        async def on_reconnect():
            reconnects.append(True)

        broker.on_reconnect(on_reconnect)
        await broker.start()
        await asyncio.sleep(0.01)
        assert reconnects == []  # first connect: nothing missed
        await connect.sockets[0].close()  # connection drops
        for _ in range(100):
            if reconnects:
                break
            await asyncio.sleep(0.01)
        assert reconnects == [True]
        await broker.stop()

    asyncio.run(go())


def test_fyers_reconnect_triggers_resync_and_events_stay_in_order():
    async def go():
        broker, _ = make_broker()
        reconnects, seen, active = [], [], [0]

        async def on_reconnect():
            reconnects.append(True)

        async def on_tick(symbol, price):
            active[0] += 1
            assert active[0] == 1, "listeners must never run concurrently"
            await asyncio.sleep(0)
            seen.append(price)
            active[0] -= 1

        broker.on_reconnect(on_reconnect)
        broker.on_tick(on_tick)
        await broker.start()
        data_sock, order_sock = FakeSocket.instances

        def burst():  # the SDK thread delivers a burst faster than listeners handle it
            for i in range(50):
                data_sock.kwargs["on_message"]({"symbol": SYM, "ltp": float(i)})
        t = threading.Thread(target=burst)
        t.start()
        t.join()
        order_sock.kwargs["on_connect"]()  # the SDK reconnected the order socket
        await asyncio.sleep(0.2)
        assert seen == [float(i) for i in range(50)]
        assert reconnects == [True]
        await broker.stop()

    asyncio.run(go())


# ---------- slow consumers are isolated ----------

def test_slow_websocket_client_is_disconnected_not_waited_for():
    class StuckWS:
        client = "stuck"
        closed_with = None

        async def send_json(self, message):
            await asyncio.Event().wait()  # never finishes

        async def close(self, code, reason):
            self.closed_with = code

    class FastWS:
        client = "fast"

        def __init__(self):
            self.got = []

        async def send_json(self, message):
            self.got.append(message["i"])

    async def go():
        cm = ConnectionManager(max_queue=2)
        stuck, fast = StuckWS(), FastWS()
        cm.add(stuck)
        cm.add(fast)
        for i in range(6):
            await asyncio.wait_for(cm.broadcast({"i": i}), timeout=0.5)  # never blocks
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)
        assert stuck not in cm.subscriptions and cm.evicted == 1 and stuck.closed_with == 1013
        assert fast.got == list(range(6))

    asyncio.run(go())


GATE: dict = {}


class Blocking(Strategy):
    """Test strategy whose tick handling waits until GATE["open"] is set."""
    name = "test_blocking"

    class Params(BaseModel):
        pass

    async def on_tick(self, tick: Tick) -> None:
        await GATE["open"].wait()


def test_slow_strategy_does_not_stall_the_feed(deploy, monkeypatch):
    monkeypatch.setattr(settings, "strategy_queue_size", 2)
    c = deploy(dep("slow", strategy=Blocking))
    inst = start_strategy(c, "slow")
    runner = c.app.state.strategy_runner

    async def feed():
        GATE["open"] = asyncio.Event()
        await runner.on_tick(1, SYM, 1.0)
        await asyncio.sleep(0.01)  # the worker takes tick 1 and blocks on it
        for price in (2.0, 3.0, 4.0, 5.0):
            await asyncio.wait_for(runner.on_tick(1, SYM, price), timeout=0.5)  # returns at once
        return runner.running[inst["id"]].dropped_ticks

    assert run(c, feed) == 2  # queue holds 2: ticks 2 and 3 were dropped for 4 and 5
    assert c.get("/health").json()["strategies"]["dropped_ticks"] == 2
    # Stopping a stuck strategy still works.
    assert c.post(f"/api/strategies/{inst['id']}/stop", headers=AUTH).json()["status"] == "stopped"


# ---------- single process ----------

def test_second_lock_holder_is_refused(tmp_path):
    first, second = InstanceLock(tmp_path / "a.lock"), InstanceLock(tmp_path / "a.lock")
    first.acquire()
    with pytest.raises(AlreadyRunning, match="already running"):
        second.acquire()
    first.release()
    second.acquire()  # free again once the first one is gone
    second.release()


def test_server_refuses_to_start_twice():
    holder = InstanceLock(lock_path(settings.lock_file, settings.database_url))
    holder.acquire()
    try:
        with pytest.raises(AlreadyRunning):
            with TestClient(app.main.app):
                pass
    finally:
        holder.release()


def test_lock_file_defaults_next_to_the_sqlite_file(tmp_path):
    assert lock_path("", f"sqlite+aiosqlite:///{tmp_path}/x.db") == tmp_path / "x.lock"
    assert lock_path("/run/at.lock", "postgresql+asyncpg://h/db").as_posix() == "/run/at.lock"


# ---------- rate limit ----------

def test_rate_limiter_spaces_out_calls():
    async def go():
        limiter = RateLimiter(rate=20, burst=1)
        start = time.monotonic()
        for _ in range(5):
            await limiter.acquire()
        return time.monotonic() - start

    assert asyncio.run(go()) >= 0.18  # 4 waits of 1/20 s


# ---------- ledger failures ----------

def test_ledger_failure_is_retried_and_pauses_live_trading(deploy, monkeypatch):
    monkeypatch.setattr(settings, "strategies_live_enabled", True)
    c = deploy(dep("a", mode="live"))
    inst = start_strategy(c, "a")
    ledger, brokers = c.app.state.accounts.get(1).ledger, c.app.state.accounts.get(1).service
    order = Order(id="lost1", symbol=SYM, side=Side.BUY, quantity=3, order_type=OrderType.MARKET,
                  status=OrderStatus.FILLED, filled_quantity=3, filled_price=100.5)

    real_apply = ledger._apply_order

    async def broken(s, o):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ledger, "_apply_order", broken)
    run(c, ledger.on_order, order)
    r = c.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "failing"
    assert r.json()["accounts"]["ledger_unhealthy"] == 1  # public: a count, no details
    detail = c.get("/api/accounts/1", headers=AUTH).json()["connection"]["ledger"]
    assert detail == {"healthy": False, "unsaved_orders": 1, "last_error": "RuntimeError: disk full"}
    assert brokers.records_reliable() is False

    SCRIPT.append(("buy",))
    tick(c)
    blocked = signals(c, inst["id"])[0]
    assert blocked["outcome"] == "blocked" and "ledger" in blocked["message"]

    monkeypatch.setattr(ledger, "_apply_order", real_apply)  # the database is back
    run(c, ledger.retry)
    assert ledger.healthy and c.get("/health").status_code == 200

    async def saved():
        async with db.session() as s:
            return (await s.get(OrderRecord, (1, "lost1"))).filled_quantity
    assert run(c, saved) == 3  # the update was not lost


# ---------- money ----------

def test_money_is_exact(client):
    ledger = client.app.state.accounts.get(1).ledger
    for i, (side, price) in enumerate([(Side.BUY, 0.1), (Side.BUY, 0.2), (Side.SELL, 0.3)]):
        run(client, ledger.on_order, Order(
            id=f"m{i}", symbol="NSE:X", side=side, quantity=1 if side == Side.BUY else 2,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED,
            filled_quantity=1 if side == Side.BUY else 2, filled_price=price))
    closed = client.get("/api/trades/closed", headers=AUTH).json()
    # (0.3 - 0.15) * 2 with floats is 0.29999999999999993.
    assert closed["positions"][0]["realized_pnl"] == 0.3

    async def prices():
        async with db.session() as s:
            return [f.price for f in await s.scalars(select(Fill).order_by(Fill.id))]
    assert run(client, prices) == [Decimal("0.1"), Decimal("0.2"), Decimal("0.3")]


def test_money_migration_keeps_values(tmp_path):
    path = tmp_path / "old.db"
    with create_engine(f"sqlite:///{path}").begin() as conn:
        command.upgrade(alembic_config(conn), "900261617dfe")
        conn.exec_driver_sql(
            "insert into fills (id, broker, order_id, symbol, side, quantity, price, filled_at) "
            "values ('f', 'paper', 'o', 'NSE:X', 'BUY', 1, 101.1, '2026-01-01')")

    async def go():
        await db.init(f"sqlite+aiosqlite:///{path}")
        try:
            async with db.session() as s:
                return (await s.get(Fill, "f")).price
        finally:
            await db.close()

    assert asyncio.run(go()) == Decimal("101.1")
    assert sqlite3.connect(path).execute("select price from fills").fetchone() == (1011000,)


# ---------- auth ----------

def test_rest_api_token_only_in_the_header(client):
    assert client.get(f"/api/accounts/1/funds?token={TOKEN}").status_code == 401  # would leak into logs
    assert client.get("/api/accounts/1/funds", headers=AUTH).status_code == 200


# ---------- health ----------

def test_health_reports_components(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["database"] is True
    assert body["accounts"]["active"] == body["accounts"]["connected"] == 1
    assert body["accounts"]["ledger_unhealthy"] == 0
    assert "admin" not in str(body) and "Paper" not in str(body)  # public: nothing per user
    detail = client.get("/api/accounts/1", headers=AUTH).json()["connection"]
    assert {"ticks_received", "stream_error", "last_resync_at"} <= set(detail["broker"])
    assert set(detail["websockets"]) == {"orders", "tickers"}
