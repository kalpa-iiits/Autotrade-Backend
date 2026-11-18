import asyncio
from datetime import date, datetime, timezone

import pytest

from app.brokers.base import OptionContract
from app.orders.schemas import Product
from app.strategies.base import Bar
from app.strategies.builtin.smart_money_concepts_options import (
    InternalChoch3RCalls,
    InternalChoch3RPuts,
    _Bracket,
    _Pivot,
)
from app.strategies.deployments import DEPLOYMENTS


AT = datetime(2026, 11, 18, 5, 0, tzinfo=timezone.utc)
EXPIRY = date(2026, 11, 24)


def chain():
    return [
        OptionContract(f"NSE:NIFTY{strike}{kind}", kind, float(strike), EXPIRY, 65)
        for strike in range(24_000, 26_001, 100)
        for kind in ("CE", "PE")
    ]


class LiveContext:
    symbol = "NSE:NIFTY50-INDEX"
    quantity = 130
    tick_size = 0.05
    held_symbol = None
    order_pending = False
    strategy_state = {}

    def __init__(self):
        self.position = 0
        self.last_price = 25_000
        self.orders = []
        self.logs = []

    async def option_contracts(self):
        return chain()

    async def buy(self, quantity=None, reason=None, symbol=None, product=None):
        self.orders.append(("BUY", quantity, symbol, product))
        self.position += quantity

    async def sell(self, quantity=None, reason=None, symbol=None, product=None):
        self.orders.append(("SELL", quantity, symbol, product))
        self.position -= quantity

    async def save_strategy_state(self, state):
        self.strategy_state = state

    def log(self, message):
        self.logs.append(message)


def make(strategy_type, ctx=None):
    ctx = ctx or LiveContext()
    strategy = strategy_type(strategy_type.Params(), ctx, 300)
    asyncio.run(strategy.on_start())
    return strategy, ctx


def test_call_and_put_legs_can_be_held_at_the_same_time_at_500_itm():
    calls, call_ctx = make(InternalChoch3RCalls)
    puts, put_ctx = make(InternalChoch3RPuts)
    calls._pending = [_Bracket(25_000, 24_900, 25_300, 1)]
    puts._pending = [_Bracket(25_000, 25_100, 24_700, 1)]

    asyncio.run(calls._check_entry(25_000, AT))
    asyncio.run(puts._check_entry(25_000, AT))

    assert call_ctx.position == put_ctx.position == 130
    assert call_ctx.orders == [
        ("BUY", 130, "NSE:NIFTY24500CE", Product.DELIVERY)]
    assert put_ctx.orders == [
        ("BUY", 130, "NSE:NIFTY25500PE", Product.DELIVERY)]
    assert calls._position == 130 and puts._position == -130

    # The call's target exits only its CE; the independently held PE remains untouched.
    asyncio.run(calls._check_exit(25_300, AT))
    assert call_ctx.position == 0 and put_ctx.position == 130
    assert call_ctx.orders[-1] == (
        "SELL", 130, "NSE:NIFTY24500CE", Product.DELIVERY)
    assert len(put_ctx.orders) == 1


@pytest.mark.parametrize(
    ("strategy_type", "trend", "pivot_name", "close", "expected"),
    [
        (InternalChoch3RCalls, -1, "_high", 101, (101, 90, 134)),
        (InternalChoch3RPuts, 1, "_low", 99, (99, 110, 66)),
    ],
)
def test_only_internal_choch_creates_25_percent_3r_bracket(
        strategy_type, trend, pivot_name, close, expected):
    strategy, _ = make(strategy_type)
    strategy._bar_index = 20
    strategy._trend = trend
    strategy._previous_close = 99 if strategy.direction == 1 else 101
    pivot = (_Pivot(100, 10, False, 90) if strategy.direction == 1 else
             _Pivot(100, 10, False, 110))
    setattr(strategy, pivot_name, pivot)
    # Range 4: long limit 102 - .25*4 = 101; short limit 98 + .25*4 = 99.
    bar = Bar("NSE:NIFTY50-INDEX", AT, 300, 100, 102, 98, close, 4)

    asyncio.run(strategy._process_bar(bar))

    assert len(strategy._pending) == 1
    order = strategy._pending[0]
    assert (order.entry, order.stop, order.target) == expected


def test_deployments_are_independent_500_itm_option_strategies():
    calls = next(value for value in DEPLOYMENTS if value.key == "smart-money-concepts")
    puts = next(value for value in DEPLOYMENTS if value.key == "smart-money-concepts-puts")
    assert calls.strategy is InternalChoch3RCalls
    assert puts.strategy is InternalChoch3RPuts
    assert calls.params.itm_points == puts.params.itm_points == 500
    assert calls.symbol == puts.symbol == "NSE:NIFTY50-INDEX"
    assert calls.strategy.name.endswith("_options") and puts.strategy.name.endswith("_options")
