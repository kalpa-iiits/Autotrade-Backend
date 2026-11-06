import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.brokers.base import Candle
from app.strategies.backtest import Simulation
from app.strategies.base import Tick
from app.strategies.builtin.nifty_trailing_reversal import NiftyTrailingReversal
from app.strategies.deployment import Deployment

IST = ZoneInfo("Asia/Kolkata")
SYM = "NSE:NIFTY50-INDEX"


def run(bars, params=None, tick_size=None, timeframe="5m"):
    dep = Deployment(
        key="nifty-reversal", strategy=NiftyTrailingReversal, symbol=SYM,
        timeframe=timeframe, quantity=65, max_order_qty=130, max_position=65,
        max_orders_per_day=100, backtest_fixed_quantity=65,
        params=params or NiftyTrailingReversal.Params(),
    )
    sim = Simulation(dep, SYM, timeframe, daily=[], tick_size=tick_size)
    asyncio.run(sim.run(bars))
    return sim


def bar(at, open_, high, low, close):
    return Candle(at, open_, high, low, close)


def test_first_available_seed_stop_reversal_and_one_reversal_per_candle():
    first = datetime(2026, 11, 2, 9, 15, tzinfo=IST)
    bars = [
        # The first regular bar queues the seed; the next bar fills it at 25,000 and immediately
        # arms the 24,800 stop. Its 24,700 low therefore reverses the position in this candle.
        bar(first, 24_950, 25_000, 24_900, 24_980),
        bar(first + timedelta(minutes=5), 25_000, 25_100, 24_700, 24_800),
        # The new short's stop was crossed in its fill candle but was not retested there. It can
        # reverse once in this following candle.
        bar(first + timedelta(minutes=10), 24_800, 25_100, 24_700, 24_750),
    ]
    sim = run(bars)
    assert [(t.direction, t.entry_price, t.exit_price, t.quantity) for t in sim.trades] == [
        ("LONG", 25_000, 24_800, 65),
        # The 09:25 scheduled close tightens the new short stop to 24,800 * 1.004.
        ("SHORT", 24_800, 24_899.2, 65),
        ("LONG", 24_899.2, 24_750, 65),
    ]


def test_gap_uses_adverse_open_and_scheduled_close_ratchets_next_stop():
    first = datetime(2026, 11, 2, 9, 15, tzinfo=IST)
    bars = [
        bar(first, 24_950, 25_000, 24_900, 24_980),
        # 09:25 scheduled close raises the long stop from 24,800 to 25,050 * .996 = 24,949.8.
        bar(first + timedelta(minutes=5), 25_000, 25_100, 24_990, 25_050),
        # Next candle gaps below that stop, so the adverse open is the reversal fill.
        bar(first + timedelta(minutes=10), 24_900, 24_930, 24_800, 24_850),
    ]
    sim = run(bars)
    assert sim.trades[0].direction == "LONG"
    assert sim.trades[0].entry_price == 25_000
    assert sim.trades[0].exit_price == 24_900
    assert sim.trades[1].direction == "SHORT"


def test_only_the_defined_five_minute_timeframe_is_supported():
    dep = Deployment(key="nifty-reversal", strategy=NiftyTrailingReversal, quantity=65)
    assert dep.timeframe == "5m"
    assert NiftyTrailingReversal.capital_sizing is True
    assert NiftyTrailingReversal.timeframes == ("5m",)
    dep.check_timeframe("5m")

    with pytest.raises(ValidationError):
        NiftyTrailingReversal.Params(start_long=False)


def test_five_minute_timeframe_replays():
    start = datetime(2026, 11, 2, 9, 15, tzinfo=IST)
    minutes = [bar(start + timedelta(minutes=i), 25_000, 25_010, 24_990, 25_000)
               for i in range(240)]
    sim = run(minutes, timeframe="5m")
    assert sim.trades and sim.trades[0].direction == "LONG"


class LiveContext:
    def __init__(self):
        self.position = 0
        self.quantity = 65
        self.tick_size = None
        self.order_pending = False
        self.last_fill_price = None
        self.orders = []

    async def buy(self, quantity=None, reason=None):
        quantity = quantity or self.quantity
        self.position += quantity
        self.orders.append(("BUY", quantity, reason))

    async def sell(self, quantity=None, reason=None):
        quantity = quantity or self.quantity
        self.position -= quantity
        self.orders.append(("SELL", quantity, reason))


def test_live_reversal_cannot_reverse_twice_in_its_fill_candle():
    ctx = LiveContext()
    strategy = NiftyTrailingReversal(NiftyTrailingReversal.Params(), ctx, 300)
    first = datetime(2026, 11, 2, 9, 15, tzinfo=IST)

    async def replay():
        await strategy.on_start()
        await strategy.on_tick(Tick(SYM, 100, first))
        # First bar closes: seed fills synchronously at this next-bar tick.
        await strategy.on_tick(Tick(SYM, 100, first + timedelta(minutes=5)))
        await strategy.on_tick(Tick(SYM, 100, first + timedelta(minutes=9, seconds=59)))
        # The armed long stop reverses at the first tick of the 09:25 candle.
        await strategy.on_tick(Tick(SYM, 99.5, first + timedelta(minutes=10)))
        # This crosses the new short stop, but it is still the reversal fill candle.
        await strategy.on_tick(Tick(SYM, 101, first + timedelta(minutes=11)))
        assert [(side, qty) for side, qty, _ in ctx.orders] == [("BUY", 65), ("SELL", 130)]
        # It becomes actionable in the following candle.
        await strategy.on_tick(Tick(SYM, 101, first + timedelta(minutes=15)))

    asyncio.run(replay())
    assert [(side, qty) for side, qty, _ in ctx.orders] == [
        ("BUY", 65), ("SELL", 130), ("BUY", 130),
    ]


def test_trail_schedule_matches_pine_session_anchor():
    strategy = NiftyTrailingReversal(NiftyTrailingReversal.Params(), LiveContext(), 300)
    day = datetime(2026, 11, 2, tzinfo=IST)

    def starts(hour, minute):
        return day.replace(hour=hour, minute=minute)

    assert strategy._scheduled_close(starts(9, 20))   # closes 09:25
    assert not strategy._scheduled_close(starts(9, 25))  # closes 09:30
    assert strategy._scheduled_close(starts(9, 30))   # closes 09:35
    assert strategy._scheduled_close(starts(15, 20))  # closes 15:25
    assert not strategy._scheduled_close(starts(15, 25))  # 15:30 is not on the cadence


def test_live_initial_stop_uses_actual_fill_and_is_active_in_entry_candle():
    ctx = LiveContext()
    strategy = NiftyTrailingReversal(NiftyTrailingReversal.Params(), ctx, 300)
    first = datetime(2026, 11, 2, 9, 15, tzinfo=IST)

    async def replay():
        await strategy.on_start()
        await strategy.on_tick(Tick(SYM, 100, first))
        ctx.last_fill_price = 100.25
        await strategy.on_tick(Tick(SYM, 100, first + timedelta(minutes=5)))
        assert strategy.stop == 100.25 * 0.992
        await strategy.on_tick(Tick(SYM, 99, first + timedelta(minutes=6)))

    asyncio.run(replay())
    assert [(side, qty) for side, qty, _ in ctx.orders] == [("BUY", 65), ("SELL", 130)]


def test_live_reversal_resets_stop_from_actual_fill():
    ctx = LiveContext()
    strategy = NiftyTrailingReversal(NiftyTrailingReversal.Params(), ctx, 300)
    first = datetime(2026, 11, 2, 9, 15, tzinfo=IST)

    async def replay():
        await strategy.on_start()
        await strategy.on_tick(Tick(SYM, 100, first))
        ctx.last_fill_price = 100
        await strategy.on_tick(Tick(SYM, 100, first + timedelta(minutes=5)))
        ctx.last_fill_price = 98.9
        await strategy.on_tick(Tick(SYM, 99, first + timedelta(minutes=6)))

    asyncio.run(replay())
    assert strategy.stop == 98.9 * 1.008


def test_async_live_reversal_waits_for_broker_fill_price():
    class AsyncContext(LiveContext):
        async def sell(self, quantity=None, reason=None):
            quantity = quantity or self.quantity
            self.order_pending = True
            self.orders.append(("SELL", quantity, reason))

    ctx = AsyncContext()
    strategy = NiftyTrailingReversal(NiftyTrailingReversal.Params(), ctx, 300)
    first = datetime(2026, 11, 2, 9, 15, tzinfo=IST)

    async def replay():
        await strategy.on_start()
        await strategy.on_tick(Tick(SYM, 100, first))
        ctx.last_fill_price = 100
        await strategy.on_tick(Tick(SYM, 100, first + timedelta(minutes=5)))
        await strategy.on_tick(Tick(SYM, 99, first + timedelta(minutes=6)))
        assert strategy._reversal_pending

        ctx.position = -65
        ctx.order_pending = False
        ctx.last_fill_price = 98.75
        await strategy.on_tick(Tick(SYM, 98.8, first + timedelta(minutes=6, seconds=1)))

    asyncio.run(replay())
    assert strategy.stop == 98.75 * 1.008
    assert not strategy._reversal_pending


def test_direct_live_ratchet_uses_same_broker_candle_close_as_backtest():
    first = datetime(2026, 11, 2, 9, 30, tzinfo=IST)

    class HistoricalContext(LiveContext):
        def __init__(self):
            super().__init__()
            self.position = 65
            self.logs = []
            self.rows = [
                Candle(first + timedelta(minutes=i), 25_900, 26_010, 25_890, 26_000)
                for i in range(5)
            ]

        async def history(self, resolution="1", days=1):
            assert resolution == "1" and days == 1
            return self.rows

        def log(self, message):
            self.logs.append(message)

    ctx = HistoricalContext()
    strategy = NiftyTrailingReversal(NiftyTrailingReversal.Params(), ctx, 300)

    async def replay():
        await strategy.on_start()
        strategy.seeded = True
        strategy._direction = 1
        strategy.stop = 24_800
        # The tick-built bar closes at 25,000, but broker history—the backtest source—closes
        # at 26,000. The 09:35 scheduled update must therefore use 26,000.
        await strategy.on_tick(Tick(SYM, 25_000, first))
        await strategy.on_tick(Tick(SYM, 25_000, first + timedelta(minutes=4, seconds=59)))
        await strategy.on_tick(Tick(SYM, 26_000, first + timedelta(minutes=5)))

    asyncio.run(replay())
    assert strategy.stop == pytest.approx(26_000 * 0.996)


def test_restart_finishes_pending_reversal_from_recorded_actual_fill():
    class PersistentContext(LiveContext):
        def __init__(self):
            super().__init__()
            self.position = -65
            self.last_fill_price = 98.75
            self.strategy_state = {
                "version": 1,
                "strategy": "nifty_trailing_reversal",
                "bar_seconds": 300,
                "params": {},
                "seeded": True,
                "direction": -1,
                "stop": 99.792,
                "seed_pending": False,
                "reversal_pending": True,
                "pre_reversal_stop": 99.2,
                "fill_bucket": 1,
            }
            self.saved = []

        async def save_strategy_state(self, state):
            self.strategy_state = dict(state)
            self.saved.append(dict(state))

    ctx = PersistentContext()
    strategy = NiftyTrailingReversal(NiftyTrailingReversal.Params(), ctx, 300)
    at = datetime(2026, 11, 2, 10, 0, tzinfo=IST)

    async def resume():
        await strategy.on_start()
        await strategy.on_tick(Tick(SYM, 98.8, at))

    asyncio.run(resume())
    assert strategy.stop == pytest.approx(98.75 * 1.008)
    assert not strategy._reversal_pending
