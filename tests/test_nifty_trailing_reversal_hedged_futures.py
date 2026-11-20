import asyncio
from datetime import date, datetime, time

from app.brokers.base import OptionContract
from app.orders.schemas import Product
from app.strategies.base import Tick
from app.strategies.builtin.nifty_trailing_reversal import IST
from app.strategies.builtin.nifty_trailing_reversal_hedged_futures import (
    NiftyTrailingReversalHedgedFutures,
)

FUTURE = "NSE:NIFTY26NOVFUT"
EXPIRY = date(2026, 11, 26)


class HedgeContext:
    def __init__(self, position=0):
        self.symbol = FUTURE
        self.position = position
        self.positions = {FUTURE: position} if position else {}
        self.quantity = 65
        self.last_price = 25_000
        self.last_fill_price = None
        self.tick_size = 0.05
        self.order_pending = False
        self.strategy_state = {}
        self.orders = []
        self.saved = []
        self.logs = []
        self.contracts = [
            OptionContract(f"NSE:NIFTY26NOV{strike}{kind}", kind, float(strike), EXPIRY, 65)
            for strike in range(24_000, 26_001, 100)
            for kind in ("CE", "PE")
        ] + [
            OptionContract("NSE:NIFTY24NOV24500PE", "PE", 24_500, date(2026, 11, 24), 65)
        ]

    async def option_contracts(self):
        return self.contracts

    async def buy(self, quantity=None, reason=None, symbol=None, product=None):
        symbol = symbol or self.symbol
        quantity = quantity or self.quantity
        self.orders.append(("BUY", quantity, symbol, product))
        self.positions[symbol] = self.positions.get(symbol, 0) + quantity
        if symbol == self.symbol:
            self.position += quantity
            self.last_fill_price = self.last_price

    async def sell(self, quantity=None, reason=None, symbol=None, product=None):
        symbol = symbol or self.symbol
        quantity = quantity or self.quantity
        self.orders.append(("SELL", quantity, symbol, product))
        updated = self.positions.get(symbol, 0) - quantity
        if updated:
            self.positions[symbol] = updated
        else:
            self.positions.pop(symbol, None)
        if symbol == self.symbol:
            self.position -= quantity
            self.last_fill_price = self.last_price

    async def history(self, resolution="1", days=1):
        return []

    async def save_strategy_state(self, state):
        self.strategy_state = dict(state)
        self.saved.append(dict(state))

    def log(self, message):
        self.logs.append(message)


def test_initial_long_buys_500_otm_monthly_put_before_future():
    ctx = HedgeContext()
    strategy = NiftyTrailingReversalHedgedFutures(
        NiftyTrailingReversalHedgedFutures.Params(hedge_points=500), ctx, 300,
    )
    first = datetime(2026, 11, 20, 9, 15, tzinfo=IST)

    async def go():
        await strategy.on_start()
        await strategy.on_tick(Tick(FUTURE, 25_000, first))
        await strategy.on_tick(Tick(FUTURE, 25_000, first.replace(minute=20)))

    asyncio.run(go())

    assert ctx.orders[:2] == [
        ("BUY", 65, "NSE:NIFTY26NOV24500PE", Product.DELIVERY),
        ("BUY", 65, FUTURE, None),
    ]
    assert ctx.position == 65


def test_long_to_short_buys_500_otm_call_then_reverses_future_then_sells_put():
    ctx = HedgeContext(position=65)
    ctx.positions["NSE:NIFTY26NOV24500PE"] = 65
    strategy = NiftyTrailingReversalHedgedFutures(
        NiftyTrailingReversalHedgedFutures.Params(hedge_points=500), ctx, 300,
    )
    at = datetime.combine(date(2026, 11, 20), time(10, 0), tzinfo=IST)

    async def go():
        await strategy.on_start()
        strategy.seeded = True
        strategy._direction = 1
        strategy.stop = 24_900
        ctx.last_price = 24_800
        await strategy.on_tick(Tick(FUTURE, 24_800, at))

    asyncio.run(go())

    assert ctx.orders == [
        ("BUY", 65, "NSE:NIFTY26NOV25300CE", Product.DELIVERY),
        ("SELL", 130, FUTURE, None),
        ("SELL", 65, "NSE:NIFTY26NOV24500PE", Product.DELIVERY),
    ]
    assert ctx.position == -65
    assert ctx.positions["NSE:NIFTY26NOV25300CE"] == 65
    assert "NSE:NIFTY26NOV24500PE" not in ctx.positions


def test_hedge_option_quantities_do_not_change_primary_futures_position():
    ctx = HedgeContext(position=65)
    ctx.positions["NSE:NIFTY26NOV24500PE"] = 65
    assert ctx.position == 65
    assert sum(ctx.positions.values()) == 130


def test_rules_explicitly_forbid_broker_hosted_exit_orders():
    rules = NiftyTrailingReversalHedgedFutures.rules(
        NiftyTrailingReversalHedgedFutures.Params(hedge_points=500)
    )
    filters = next(rule for rule in rules if rule["kind"] == "filter")
    assert any(
        "no broker-side bracket, stop-loss, target, GTT or Smart order" in condition
        for condition in filters["conditions"]
    )


def test_async_opposite_hedge_fill_continues_latched_reversal_on_clock():
    class AsyncHedgeContext(HedgeContext):
        async def buy(self, quantity=None, reason=None, symbol=None, product=None):
            symbol = symbol or self.symbol
            quantity = quantity or self.quantity
            if symbol.endswith("CE"):
                self.orders.append(("BUY", quantity, symbol, product))
                self.order_pending = True
                return
            await super().buy(quantity, reason, symbol, product)

    ctx = AsyncHedgeContext(position=65)
    ctx.positions["NSE:NIFTY26NOV24500PE"] = 65
    strategy = NiftyTrailingReversalHedgedFutures(
        NiftyTrailingReversalHedgedFutures.Params(hedge_points=500), ctx, 300,
    )
    at = datetime.combine(date(2026, 11, 20), time(10, 0), tzinfo=IST)

    async def go():
        await strategy.on_start()
        strategy.seeded = True
        strategy._direction = 1
        strategy.stop = 24_900
        ctx.last_price = 24_800
        await strategy.on_tick(Tick(FUTURE, 24_800, at))
        assert ctx.position == 65 and strategy._deferred_stop_tick is not None

        ctx.positions["NSE:NIFTY26NOV25300CE"] = 65
        ctx.order_pending = False
        await strategy.on_clock(at)

    asyncio.run(go())
    assert [order[:3] for order in ctx.orders] == [
        ("BUY", 65, "NSE:NIFTY26NOV25300CE"),
        ("SELL", 130, FUTURE),
        ("SELL", 65, "NSE:NIFTY26NOV24500PE"),
    ]
    assert ctx.position == -65
