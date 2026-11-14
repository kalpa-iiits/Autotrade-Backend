import asyncio
from datetime import datetime, time, timedelta

import pytest

from app.brokers.base import Candle, OptionContract
from app.orders.schemas import Product
from app.strategies.base import Tick
from app.strategies.builtin.nifty_trailing_reversal import IST
from app.strategies.builtin.nifty_trailing_reversal_options import (
    NiftyTrailingReversalOptions,
    ReversalOptionLegs,
    _complete_bars,
)


class FakeCtx:
    def __init__(self, history=None, quantity=65):
        self.position = 0
        self.quantity = quantity
        self.last_price = 25_000
        self.held_symbol = None
        self.tick_size = 0.05
        self.order_pending = False
        self.calls = []
        self.products = []
        self.logs = []
        self._history = history or []
        today = datetime.now(IST).date()
        self._contracts = [
            OptionContract(f"NSE:NIFTY{today + timedelta(days=1):%d%m}{strike}{kind}",
                           kind, float(strike), today + timedelta(days=1), 65)
            for strike in range(24_500, 25_551, 50) for kind in ("CE", "PE")
        ]

    async def option_contracts(self):
        return self._contracts

    async def history(self, resolution="1", days=7):
        assert resolution == "1" and days in (1, 7)
        return self._history

    async def buy(self, quantity=None, reason=None, symbol=None, product=None):
        self.calls.append(("BUY", quantity, symbol))
        self.products.append(product)
        self.position += quantity
        self.held_symbol = symbol

    async def sell(self, quantity=None, reason=None, symbol=None, product=None):
        self.calls.append(("SELL", quantity, symbol))
        self.products.append(product)
        self.position -= quantity

    def log(self, message):
        self.logs.append(message)


def test_reversal_sells_current_option_before_buying_opposite_leg():
    async def go():
        ctx = FakeCtx(quantity=130)
        legs = ReversalOptionLegs(ctx, 300)
        await legs.buy(reason="long signal")
        assert legs.position == 130
        await legs.sell(reason="reverse short")
        assert legs.position == -130
        await legs.advance()
        return ctx

    ctx = asyncio.run(go())
    assert [(side, qty) for side, qty, _ in ctx.calls] == [
        ("BUY", 130), ("SELL", 130), ("BUY", 130),
    ]
    assert ctx.calls[0][2].endswith("CE")
    assert ctx.calls[1][2] == ctx.calls[0][2]
    assert ctx.calls[2][2].endswith("PE")
    assert ctx.products == [Product.DELIVERY, Product.DELIVERY, Product.DELIVERY]


def test_async_reversal_never_buys_opposite_leg_before_exit_is_terminal():
    class AsyncCtx(FakeCtx):
        async def buy(self, quantity=None, reason=None, symbol=None, product=None):
            self.calls.append(("BUY", quantity, symbol))
            self.products.append(product)
            self.order_pending = True

        async def sell(self, quantity=None, reason=None, symbol=None, product=None):
            self.calls.append(("SELL", quantity, symbol))
            self.products.append(product)
            self.order_pending = True

    async def go():
        ctx = AsyncCtx(quantity=130)
        current = next(c for c in ctx._contracts if c.kind == "CE" and c.strike == 24_700)
        ctx.position, ctx.held_symbol = 130, current.symbol
        legs = ReversalOptionLegs(ctx, 300)

        await legs.sell(reason="reverse short")
        await legs.advance()
        assert [side for side, _, _ in ctx.calls] == ["SELL"]

        # The broker reports the CE exit terminal. Only now may the PE entry be sent.
        ctx.position, ctx.order_pending = 0, False
        await legs.advance()
        return ctx

    ctx = asyncio.run(go())
    assert [side for side, _, _ in ctx.calls] == ["SELL", "BUY"]
    assert ctx.calls[0][2].endswith("CE") and ctx.calls[1][2].endswith("PE")
    assert ctx.products == [Product.DELIVERY, Product.DELIVERY]


def test_partial_option_entry_is_topped_up_to_the_configured_quantity():
    async def go():
        ctx = FakeCtx(quantity=130)
        held = next(c for c in ctx._contracts if c.kind == "CE" and c.strike == 24_700)
        ctx.position, ctx.held_symbol = 65, held.symbol
        legs = ReversalOptionLegs(ctx, 300)
        await legs.advance(datetime.combine(datetime.now(IST).date(), time(10, 0), tzinfo=IST))
        return ctx

    ctx = asyncio.run(go())
    assert ctx.calls == [("BUY", 65, ctx.held_symbol)]
    assert ctx.position == 130 and ctx.products == [Product.DELIVERY]


def test_new_signal_uses_nearest_non_expiring_weekly_and_1000_point_itm_default():
    async def go():
        today = datetime.now(IST).date()
        current_week_expiry = today + timedelta(days=1)
        current_week = [
            OptionContract(f"NSE:NIFTYCURRENT{strike}{kind}", kind, float(strike),
                           current_week_expiry, 65)
            for strike in range(23_500, 26_551, 50) for kind in ("CE", "PE")
        ]
        contexts = []
        for direction in ("CE", "PE"):
            ctx = FakeCtx()
            ctx._contracts = current_week + ctx._contracts
            legs = ReversalOptionLegs(ctx, NiftyTrailingReversalOptions.Params().itm_points)
            await (legs.buy(reason="long signal") if direction == "CE" else
                   legs.sell(reason="short signal"))
            contexts.append(ctx)
        return contexts, current_week_expiry

    (ce_ctx, pe_ctx), current_week_expiry = asyncio.run(go())
    ce = next(c for c in ce_ctx._contracts if c.symbol == ce_ctx.calls[0][2])
    pe = next(c for c in pe_ctx._contracts if c.symbol == pe_ctx.calls[0][2])
    assert (ce.kind, ce.strike, ce.expiry) == ("CE", 24_000, current_week_expiry)
    assert (pe.kind, pe.strike, pe.expiry) == ("PE", 26_000, current_week_expiry)


def test_positional_leg_rolls_instead_of_holding_through_expiry():
    async def go():
        ctx = FakeCtx()
        today = datetime.now(IST).date()
        expiring = OptionContract("NSE:NIFTYTODAY24700CE", "CE", 24_700, today, 65)
        ctx._contracts.insert(0, expiring)
        ctx.position, ctx.held_symbol = 65, expiring.symbol
        legs = ReversalOptionLegs(ctx, 300)
        await legs.advance(datetime.combine(today, time(14, 59), tzinfo=IST))
        assert ctx.calls == []  # keep the current-week contract until the requested time
        rollover = datetime.combine(today, time(15, 0), tzinfo=IST)
        await legs.advance(rollover)  # exit today's CE
        await legs.advance(rollover)  # enter the next available CE
        return ctx

    ctx = asyncio.run(go())
    assert ctx.calls[0] == ("SELL", 65, "NSE:NIFTYTODAY24700CE")
    assert ctx.calls[1][0] == "BUY" and ctx.calls[1][2].endswith("CE")
    assert ctx.calls[1][2] != ctx.calls[0][2]
    assert ctx.products == [Product.DELIVERY, Product.DELIVERY]


def test_expiry_day_entries_switch_to_next_week_at_1500_ist():
    async def open_at(at):
        ctx = FakeCtx()
        today = at.date()
        ctx._contracts = [
            OptionContract(f"NSE:NIFTYTODAY{strike}{kind}", kind, float(strike), today, 65)
            for strike in range(24_500, 25_551, 50) for kind in ("CE", "PE")
        ] + ctx._contracts
        legs = ReversalOptionLegs(ctx, 300)
        await legs.advance(at)  # set the live market time before the signal arrives
        await legs.sell(reason="short signal")
        return ctx

    today = datetime.now(IST).date()
    before = asyncio.run(open_at(datetime.combine(today, time(14, 59), tzinfo=IST)))
    after = asyncio.run(open_at(datetime.combine(today, time(15, 0), tzinfo=IST)))

    before_contract = next(c for c in before._contracts if c.symbol == before.calls[0][2])
    after_contract = next(c for c in after._contracts if c.symbol == after.calls[0][2])
    assert before_contract.expiry == today
    assert after_contract.expiry == today + timedelta(days=1)


def test_expiry_roll_retries_transient_option_chain_failure_without_stopping():
    class FlakyChainCtx(FakeCtx):
        attempts = 0

        async def option_contracts(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("temporary symbol-master failure")
            return self._contracts

    async def go():
        ctx = FlakyChainCtx()
        today = datetime.now(IST).date()
        expiring = OptionContract("NSE:NIFTYTODAY24700CE", "CE", 24_700, today, 65)
        ctx._contracts.insert(0, expiring)
        ctx.position, ctx.held_symbol = 65, expiring.symbol
        legs = ReversalOptionLegs(ctx, 300)
        rollover = datetime.combine(today, time(15, 0), tzinfo=IST)

        await legs.advance(rollover)
        assert ctx.calls == [] and ctx.attempts == 1
        await legs.advance(rollover + timedelta(seconds=4))
        assert ctx.calls == [] and ctx.attempts == 1  # throttled
        await legs.advance(rollover + timedelta(seconds=5))
        assert ctx.calls[0] == ("SELL", 65, expiring.symbol)
        await legs.advance(rollover + timedelta(seconds=5))
        return ctx

    ctx = asyncio.run(go())
    assert [side for side, _, _ in ctx.calls] == ["SELL", "BUY"]
    assert ctx.calls[1][2].endswith("CE") and ctx.calls[1][2] != ctx.calls[0][2]
    assert any("retrying option chain in 5 seconds" in message for message in ctx.logs)


def test_clock_drives_1500_rollover_without_a_market_tick_and_stops_at_close():
    async def strategy_at(at):
        ctx = FakeCtx()
        expiring = OptionContract("NSE:NIFTYTODAY24700CE", "CE", 24_700, at.date(), 65)
        ctx._contracts.insert(0, expiring)
        ctx.position, ctx.held_symbol = 65, expiring.symbol
        strategy = NiftyTrailingReversalOptions(NiftyTrailingReversalOptions.Params(), ctx, 300)
        strategy._real_ctx = ctx
        strategy.ctx = ReversalOptionLegs(ctx, 300)
        await super(NiftyTrailingReversalOptions, strategy).on_start()
        strategy.seeded = strategy._history_warmed = True
        strategy._direction = strategy.ctx.target = 1
        strategy.stop = 24_000
        await strategy.on_clock(at)
        return ctx

    today = datetime.now(IST).date()
    at_roll = asyncio.run(strategy_at(datetime.combine(today, time(15, 0), tzinfo=IST)))
    after_close = asyncio.run(strategy_at(datetime.combine(today, time(15, 30), tzinfo=IST)))
    assert [side for side, _, _ in at_roll.calls] == ["SELL"]
    assert after_close.calls == []


def test_option_leg_expiry_metadata_is_saved_and_restored():
    class PersistentCtx(FakeCtx):
        def __init__(self):
            super().__init__()
            self.saved = []

        async def save_strategy_state(self, state):
            self.saved.append(dict(state))

    async def go():
        ctx = PersistentCtx()
        held = next(c for c in ctx._contracts if c.kind == "CE" and c.strike == 24_700)
        ctx.position, ctx.held_symbol = 65, held.symbol
        strategy = NiftyTrailingReversalOptions(NiftyTrailingReversalOptions.Params(), ctx, 300)
        strategy._real_ctx = ctx
        strategy.ctx = ReversalOptionLegs(ctx, 300)
        await super(NiftyTrailingReversalOptions, strategy).on_start()
        strategy.seeded = strategy._history_warmed = True
        strategy._direction = strategy.ctx.target = 1
        strategy.stop = 24_000
        await strategy._advance_option_legs(
            datetime.combine(datetime.now(IST).date(), time(10, 0), tzinfo=IST)
        )
        restored = ReversalOptionLegs(ctx, 300)
        restored.restore_checkpoint_state(ctx.saved[-1]["option_leg"])
        return held, restored

    held, restored = asyncio.run(go())
    assert restored.held_expiry == held.expiry and restored.held_lot == held.lot


def test_live_start_reconstructs_direction_and_stop_from_history():
    day = datetime.combine(datetime.now(IST).date() - timedelta(days=1), time(9, 15), tzinfo=IST)
    rows = []
    for minute in range(10):
        at = day + timedelta(minutes=minute)
        if minute < 5:
            price = 24_950 + minute
        else:
            price = 25_000 + (minute - 5) * 12.5
        rows.append(Candle(at, price, price + 5, price - 5, price))
    ctx = FakeCtx(rows)
    strategy = NiftyTrailingReversalOptions(NiftyTrailingReversalOptions.Params(), ctx, 300)

    async def go():
        await strategy.on_start()
        if not ctx.calls:  # startup outside market hours defers the order to the first live tick
            opened = datetime.combine(datetime.now(IST).date(), time(9, 15), tzinfo=IST)
            await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 25_000, opened))

    asyncio.run(go())

    assert strategy.seeded and strategy._direction == 1
    assert strategy.stop == pytest.approx(25_050 * 0.996)
    assert strategy.ctx.target == 1
    assert len(ctx.calls) == 1 and ctx.calls[0][0] == "BUY" and ctx.calls[0][2].endswith("CE")
    assert any("reconstructed LONG" in message for message in ctx.logs)


def test_live_restart_restores_exact_stop_without_history_and_handles_opening_gap():
    class PersistentCtx(FakeCtx):
        def __init__(self):
            super().__init__(history=[])
            self.position = 65
            self.held_symbol = "NSE:NIFTY25110724700CE"
            params = NiftyTrailingReversalOptions.Params()
            self.strategy_state = {
                "version": 1,
                "strategy": "nifty_trailing_reversal_options",
                "bar_seconds": 300,
                "params": params.model_dump(mode="json"),
                "seeded": True,
                "direction": 1,
                "stop": 24_900.0,
                "seed_pending": False,
                "fill_bucket": None,
            }
            self.saved = []
            self.history_calls = 0

        async def history(self, resolution="1", days=7):
            self.history_calls += 1
            return []

        async def save_strategy_state(self, state):
            self.strategy_state = dict(state)
            self.saved.append(dict(state))

    ctx = PersistentCtx()
    strategy = NiftyTrailingReversalOptions(NiftyTrailingReversalOptions.Params(), ctx, 300)
    opened = datetime.combine(datetime.now(IST).date(), time(9, 15), tzinfo=IST)

    async def go():
        await strategy.on_start()
        assert strategy.stop == 24_900
        await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 24_800, opened))

    asyncio.run(go())

    assert ctx.history_calls == 0
    assert [side for side, _, _ in ctx.calls] == ["SELL", "BUY"]
    assert ctx.calls[0][2].endswith("CE") and ctx.calls[1][2].endswith("PE")
    assert ctx.saved[-1]["direction"] == -1
    assert ctx.saved[-1]["stop"] == pytest.approx(24_800 * 1.008)


def test_ordinary_non_triggering_tick_does_not_write_checkpoint():
    class PersistentCtx(FakeCtx):
        def __init__(self):
            super().__init__()
            self.position = 65
            self.held_symbol = "NSE:NIFTY25110724700CE"
            params = NiftyTrailingReversalOptions.Params()
            self.strategy_state = {
                "version": 1,
                "strategy": "nifty_trailing_reversal_options",
                "bar_seconds": 300,
                "params": params.model_dump(mode="json"),
                "seeded": True,
                "direction": 1,
                "stop": 24_000.0,
                "seed_pending": False,
                "fill_bucket": None,
            }
            self.saved = []

        async def save_strategy_state(self, state):
            self.saved.append(dict(state))

    ctx = PersistentCtx()
    strategy = NiftyTrailingReversalOptions(NiftyTrailingReversalOptions.Params(), ctx, 300)
    at = datetime.combine(datetime.now(IST).date(), time(9, 16), tzinfo=IST)

    async def go():
        await strategy.on_start()
        await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 25_000, at))
        await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 25_001, at + timedelta(seconds=1)))

    asyncio.run(go())
    assert ctx.saved == []


def test_history_aggregation_ignores_partial_five_minute_bars():
    start = datetime.now(IST).replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % 5 + 10)
    rows = [Candle(start + timedelta(minutes=i), 100, 101, 99, 100) for i in range(7)]
    bars = _complete_bars(rows, start + timedelta(minutes=7), 300)
    assert len(bars) == 1 and bars[0].start == start


def test_history_aggregation_rejects_duplicate_and_missing_minutes():
    start = datetime.now(IST).replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % 5 + 10)
    starts = [start, start + timedelta(minutes=1), start + timedelta(minutes=1),
              start + timedelta(minutes=3), start + timedelta(minutes=4)]
    rows = [Candle(at, 100, 101, 99, 100) for at in starts]
    assert _complete_bars(rows, start + timedelta(minutes=5), 300) == []


def test_history_warmup_retries_after_temporary_broker_failure():
    day = datetime.combine(datetime.now(IST).date() - timedelta(days=1), time(9, 15), tzinfo=IST)
    rows = [Candle(day + timedelta(minutes=i), 25_000, 25_010, 24_990, 25_000)
            for i in range(10)]

    class FlakyCtx(FakeCtx):
        attempts = 0

        async def history(self, resolution="1", days=7):
            from app.brokers import OrderError
            self.attempts += 1
            if self.attempts == 1:
                raise OrderError("temporarily disconnected")
            return rows

    ctx = FlakyCtx()
    strategy = NiftyTrailingReversalOptions(NiftyTrailingReversalOptions.Params(), ctx, 300)

    async def go():
        await strategy.on_start()
        assert not strategy._history_warmed and ctx.calls == []
        retry = strategy._warmup_retry_at + timedelta(seconds=1)
        # on_tick intentionally ignores timestamps outside the continuous session. If the test
        # runs after the close, the retry belongs to the next session rather than 09:15 earlier
        # on the same day.
        if retry.time() < time(9, 15):
            retry = datetime.combine(retry.date(), time(9, 15), tzinfo=IST)
        elif retry.time() >= time(15, 30):
            retry = datetime.combine(retry.date() + timedelta(days=1), time(9, 15), tzinfo=IST)
        await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 25_000, retry))

    asyncio.run(go())
    assert strategy._history_warmed and ctx.calls[0][0] == "BUY"


def test_live_ratchet_uses_broker_candle_not_streaming_tick_close():
    prior = datetime.combine(datetime.now(IST).date() - timedelta(days=1), time(9, 15), tzinfo=IST)
    warmup = [Candle(prior + timedelta(minutes=i), 25_000, 25_010, 24_990, 25_000)
              for i in range(10)]
    ctx = FakeCtx(warmup)
    strategy = NiftyTrailingReversalOptions(NiftyTrailingReversalOptions.Params(), ctx, 300)

    async def go():
        await strategy.on_start()
        live = prior + timedelta(minutes=15)  # 09:30 bar; its 09:35 close is scheduled
        # Broker OHLC says this bar closed at 26,000, though the observed tick stream closes 25,000.
        ctx._history = warmup + [
            Candle(live + timedelta(minutes=i), 25_900, 26_010, 25_890, 26_000)
            for i in range(5)
        ]
        await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 25_000, live))
        await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 25_000, live + timedelta(minutes=4, seconds=59)))
        await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 26_000, live + timedelta(minutes=5)))

    asyncio.run(go())
    assert strategy.stop == pytest.approx(26_000 * 0.996)


def test_direct_and_options_versions_have_separate_catalog_entries():
    from app.strategies.deployments import DEPLOYMENTS

    direct = next(d for d in DEPLOYMENTS if d.key == "nifty-percentage-reversal")
    options = next(d for d in DEPLOYMENTS if d.key == "nifty-percentage-reversal-options")
    assert direct.strategy.__name__ == "NiftyTrailingReversal"
    assert direct.strategy.capital_sizing and direct.backtest_fixed_quantity is None
    assert (direct.max_order_qty, direct.max_position) == (10_000, 5_000)
    assert options.strategy is NiftyTrailingReversalOptions
    assert direct.strategy.timeframes == ("5m",)
    assert options.strategy.timeframes == ("5m", "15m", "30m", "60m")
    for timeframe in options.strategy.timeframes:
        options.check_timeframe(timeframe)
    assert not options.strategy.capital_sizing and options.backtest_fixed_quantity == 65
    assert options.params.itm_points == 1000
    assert options.symbol == "NSE:NIFTY50-INDEX"

