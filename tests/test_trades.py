from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core import db
from app.orders.models import OrderEvent
from app.orders.schemas import Order, OrderStatus, OrderType, Side
from app.trades.ledger import TradeLedger
from app.trades.models import Fill, Position, PositionFill
from tests.conftest import TOKEN, recv_until, run

AUTH = {"Authorization": f"Bearer {TOKEN}"}
SYM = "NSE:SBIN-EQ"


def order(oid, side, qty, status=OrderStatus.FILLED, filled=None, price=None, symbol=SYM):
    return Order(
        id=oid, symbol=symbol, side=side, quantity=qty, order_type=OrderType.MARKET, status=status,
        filled_quantity=qty if filled is None and status == OrderStatus.FILLED else (filled or 0),
        filled_price=price,
    )


@pytest.fixture
def ledger(client):
    """A ledger on the test database; `feed(*orders)` applies updates in sequence."""
    led = TradeLedger("test")

    def feed(*orders):
        for o in orders:
            run(client, led.on_order, o)

    led.feed = feed
    return led


def positions(client, **where):
    async def q():
        async with db.session() as s:
            query = select(Position).order_by(Position.id)
            for k, v in where.items():
                query = query.where(getattr(Position, k) == v)
            return list(await s.scalars(query))
    return run(client, q)


def count(client, model, **where):
    async def q():
        async with db.session() as s:
            query = select(func.count()).select_from(model)
            for k, v in where.items():
                query = query.where(getattr(model, k) == v)
            return await s.scalar(query)
    return run(client, q)


# ---------- ledger rules ----------

def test_long_round_trip(client, ledger):
    ledger.feed(order("b1", Side.BUY, 10, price=100.0), order("s1", Side.SELL, 10, price=110.0))
    [p] = positions(client)
    assert (p.direction, p.status, p.quantity) == ("LONG", "CLOSED", 0)
    assert (p.avg_entry_price, p.avg_exit_price, p.realized_pnl) == (100.0, 110.0, 100.0)
    assert p.closed_at is not None


def test_short_round_trip(client, ledger):
    ledger.feed(order("s1", Side.SELL, 5, price=200.0), order("b1", Side.BUY, 5, price=190.0))
    [p] = positions(client)
    assert (p.direction, p.status, p.realized_pnl) == ("SHORT", "CLOSED", 50.0)


def test_partial_fills_and_duplicates(client, ledger):
    ledger.feed(
        order("b1", Side.BUY, 10, status=OrderStatus.OPEN),
        order("b1", Side.BUY, 10, status=OrderStatus.OPEN, filled=4, price=100.0),
        order("b1", Side.BUY, 10, status=OrderStatus.OPEN, filled=4, price=100.0),  # repeat
        order("b1", Side.BUY, 10, filled=10, price=101.0),
    )
    async def fills():
        async with db.session() as s:
            return [(f.quantity, f.price) for f in await s.scalars(select(Fill).order_by(Fill.filled_at))]
    # Second fill's price is backed out of the averages: (10*101 - 4*100) / 6.
    assert run(client, fills) == [(4, Decimal("100")), (6, Decimal("101.6667"))]  # exact
    assert count(client, OrderEvent, order_id="b1") == 3  # open, partial, filled (not the repeat)
    [p] = positions(client)
    assert (p.status, p.quantity, p.avg_entry_price) == ("OPEN", 10, 101.0)


def test_scale_in_then_partial_exit(client, ledger):
    ledger.feed(
        order("b1", Side.BUY, 10, price=100.0),
        order("b2", Side.BUY, 10, price=110.0),
        order("s1", Side.SELL, 5, price=120.0),
    )
    [p] = positions(client)
    assert (p.status, p.quantity, p.entry_quantity, p.exit_quantity) == ("OPEN", 15, 20, 5)
    assert (p.avg_entry_price, p.realized_pnl) == (105.0, 75.0)  # 5 * (120 - 105)


def test_flip_closes_and_opens(client, ledger):
    ledger.feed(order("b1", Side.BUY, 10, price=100.0), order("s1", Side.SELL, 15, price=90.0))
    closed, opened = positions(client)
    assert (closed.status, closed.realized_pnl) == ("CLOSED", -100.0)
    assert (opened.direction, opened.status, opened.quantity, opened.avg_entry_price) == (
        "SHORT", "OPEN", 5, 90.0
    )
    # The one 15-share fill is linked to both positions.
    assert count(client, PositionFill, fill_id="test:s1:15") == 2


def test_cancelled_after_partial_fill_and_missing_quantity(client, ledger):
    ledger.feed(
        order("b1", Side.BUY, 10, status=OrderStatus.CANCELLED, filled=3, price=50.0),
        Order(id="b2", symbol="NSE:TCS-EQ", side=Side.BUY, quantity=2, order_type=OrderType.MARKET,
              status=OrderStatus.FILLED, filled_quantity=0, filled_price=4000.0),
    )
    sbin, tcs = positions(client)
    assert (sbin.symbol, sbin.quantity) == (SYM, 3)
    assert (tcs.symbol, tcs.quantity) == ("NSE:TCS-EQ", 2)  # FILLED without qty -> full qty


def test_symbols_and_brokers_are_separate(client, ledger):
    other = TradeLedger("other")
    ledger.feed(order("b1", Side.BUY, 1, price=10.0))
    run(client, other.on_order, order("x1", Side.SELL, 1, price=10.0))
    assert [(p.broker, p.direction) for p in positions(client)] == [("test", "LONG"), ("other", "SHORT")]


# ---------- endpoints (paper broker end to end) ----------

def place(ws, side, qty, symbol=SYM):
    ws.send_json({"action": "place_order", "symbol": symbol, "side": side, "quantity": qty})
    return recv_until(ws, "ack")["order"]


def test_open_and_closed_trades(client):
    with client.websocket_connect(f"/api/ws/orders?token={TOKEN}") as ws:
        ws.receive_json()
        buy = place(ws, "BUY", 10)
        sell = place(ws, "SELL", 10)
        place(ws, "BUY", 2, symbol="NSE:TCS-EQ")

    closed = client.get("/api/trades/closed", headers=AUTH).json()
    [trade] = closed["positions"]
    expected = round((sell["filled_price"] - buy["filled_price"]) * 10, 4)
    assert trade["symbol"] == SYM and trade["realized_pnl"] == pytest.approx(expected)
    assert closed["totals"]["count"] == 1
    assert closed["totals"]["realized_pnl"] == pytest.approx(expected)

    opened = client.get("/api/trades/open", headers=AUTH).json()
    [tcs] = opened["positions"]
    assert (tcs["symbol"], tcs["direction"], tcs["quantity"]) == ("NSE:TCS-EQ", "LONG", 2)
    assert tcs["unrealized_pnl"] is None and opened["totals"]["unpriced"] == 1  # no ticks yet

    detail = client.get(f"/api/trades/{trade['id']}", headers=AUTH).json()
    assert [(f["role"], f["quantity"]) for f in detail["fills"]] == [("entry", 10), ("exit", 10)]
    assert client.get("/api/trades/999999", headers=AUTH).status_code == 404

    events = client.get(f"/api/orders/{buy['id']}/events", headers=AUTH).json()
    assert [e["status"] for e in events] == ["FILLED"]


def test_unrealized_pnl_from_ticks(client):
    with client.websocket_connect(f"/api/ws/tickers?token={TOKEN}") as tickers, \
         client.websocket_connect(f"/api/ws/orders?token={TOKEN}") as orders:
        tickers.receive_json()
        orders.receive_json()
        tickers.send_json({"action": "subscribe", "symbols": [SYM]})
        buy = place(orders, "BUY", 4)
        last = recv_until(tickers, "tick")["ltp"]

    [p] = client.get("/api/trades/open", headers=AUTH).json()["positions"]
    assert p["last_price"] is not None
    assert p["unrealized_pnl"] == pytest.approx((p["last_price"] - buy["filled_price"]) * 4, abs=0.01)
    assert last > 0


def test_closed_filters_and_totals(client, ledger):
    ledger.feed(
        order("b1", Side.BUY, 1, price=100.0), order("s1", Side.SELL, 1, price=110.0),  # +10
        order("b2", Side.BUY, 1, price=100.0), order("s2", Side.SELL, 1, price=95.0),   # -5
        order("b3", Side.BUY, 1, price=10.0, symbol="NSE:TCS-EQ"),
        order("s3", Side.SELL, 1, price=12.0, symbol="NSE:TCS-EQ"),                    # +2
    )
    body = client.get("/api/trades/closed?broker=test&limit=1", headers=AUTH).json()
    assert len(body["positions"]) == 1  # page size
    assert body["totals"] == {"count": 3, "realized_pnl": 7.0, "wins": 2, "losses": 1,
                              "win_rate": 0.6667}  # totals ignore the page size
    sbin = client.get(f"/api/trades/closed?broker=test&symbol={SYM}", headers=AUTH).json()
    assert sbin["totals"]["count"] == 2
    future = client.get("/api/trades/closed?since=2999-01-01T00:00:00Z", headers=AUTH).json()
    assert future["totals"]["count"] == 0 and future["totals"]["win_rate"] is None


def test_trades_need_login(client):
    for path in ["/api/trades/open", "/api/trades/closed", "/api/trades/1", "/api/orders/x/events"]:
        assert client.get(path).status_code == 401


def test_stale_update_does_not_undo_a_fill(client, ledger):
    """An older "OPEN, 0 filled" arriving after the fill (e.g. place_order's reply) is ignored:
    the order stays FILLED, its fill and position stay, and a restart rebuilds them correctly."""
    from app.orders.history import list_history

    filled = order("b1", Side.BUY, 10, price=100.0).model_copy(update={"client_order_id": "abc123"})
    ledger.feed(filled, order("b1", Side.BUY, 10, status=OrderStatus.OPEN, filled=0))
    [rec] = run(client, list_history, "test")
    assert (rec["status"], rec["filled_quantity"], rec["client_order_id"]) == ("FILLED", 10, "abc123")
    assert count(client, Fill) == 1 and count(client, OrderEvent, order_id="b1") == 1
    [p] = positions(client)
    assert (p.status, p.quantity) == ("OPEN", 10)

    # A real later update still applies, and keeps the client order id it doesn't carry.
    ledger.feed(order("b1", Side.BUY, 10, status=OrderStatus.CANCELLED, filled=10, price=100.0))
    [rec] = run(client, list_history, "test")
    assert (rec["status"], rec["client_order_id"]) == ("CANCELLED", "abc123")
