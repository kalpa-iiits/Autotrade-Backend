import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
        # The first regular bar queues the seed; the next bar fills it at 25,000. Pine does not
        # arm the 24,800 stop until this fill candle closes, so its 24,700 low cannot reverse it.
        bar(first, 24_950, 25_000, 24_900, 24_980),
        bar(first + timedelta(minutes=5), 25_000, 25_100, 24_700, 24_800),
        # Now the stop is active. The new short's stop is also crossed by this candle's high,
        # but Pine processes at most one reversal in a candle.
        bar(first + timedelta(minutes=10), 24_800, 25_100, 24_700, 24_750),
    ]
    sim = run(bars)
    assert [(t.direction, t.entry_price, t.exit_price, t.quantity) for t in sim.trades] == [
        ("LONG", 25_000, 24_800, 65),
        ("SHORT", 24_800, 24_750, 65),
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


def test_exact_seed_requires_the_prior_candle_and_fills_at_seed_open():
    params = NiftyTrailingReversal.Params(initialization="exact_seed_time")
    prior = datetime(2021, 11, 8, 9, 15, tzinfo=IST)
    sim = run([
        bar(prior, 24_950, 25_000, 24_900, 24_980),
        bar(prior + timedelta(minutes=5), 25_000, 25_100, 24_700, 24_800),
    ], params)
    assert sim.trades[0].direction == "LONG"
    assert sim.trades[0].entry_price == 25_000

    # Loading from the seed candle itself is too late: Pine needed the prior close to submit.
    sim = run([bar(prior + timedelta(minutes=5), 25_000, 25_100, 24_700, 24_800)], params)
    assert sim.trades == []


def test_can_seed_short_and_round_stops_to_tick():
    first = datetime(2026, 11, 2, 9, 15, tzinfo=IST)
    params = NiftyTrailingReversal.Params(start_long=False, round_to_tick=True)
    sim = run([
        bar(first, 100, 101, 99, 100),
        bar(first + timedelta(minutes=5), 100.03, 101, 99.5, 101),
        # Raw initial stop 100.83024 rounds to 100.85 on a 0.05 grid.
        bar(first + timedelta(minutes=10), 100.5, 100.84, 100, 100.4),
        bar(first + timedelta(minutes=15), 100.5, 100.86, 100, 100.4),
    ], params, tick_size=0.05)
    assert sim.trades[0].direction == "SHORT"
    assert sim.trades[0].entry_price == 100.03
    assert sim.trades[0].exit_price == 100.85


def test_all_platform_timeframes_are_supported_with_five_minutes_default():
    dep = Deployment(key="nifty-reversal", strategy=NiftyTrailingReversal, quantity=65)
    assert dep.timeframe == "5m"
    assert NiftyTrailingReversal.capital_sizing is True
    assert NiftyTrailingReversal.timeframes == ("1m", "3m", "5m", "10m", "15m", "30m", "60m")
    for timeframe in NiftyTrailingReversal.timeframes:
        dep.check_timeframe(timeframe)


def test_every_supported_timeframe_replays_without_changing_five_minute_default():
    start = datetime(2026, 11, 2, 9, 15, tzinfo=IST)
    minutes = [bar(start + timedelta(minutes=i), 25_000, 25_010, 24_990, 25_000)
               for i in range(240)]
    for timeframe in NiftyTrailingReversal.timeframes:
        sim = run(minutes, timeframe=timeframe)
        assert sim.trades and sim.trades[0].direction == "LONG", timeframe


def test_non_five_minute_bars_update_when_they_cross_a_cadence_boundary():
    params = NiftyTrailingReversal.Params()
    ctx = LiveContext()
    day = datetime(2026, 11, 2, tzinfo=IST)
    one_minute = NiftyTrailingReversal(params, ctx, 60)
    assert one_minute._scheduled_close(day.replace(hour=9, minute=24))
    assert not one_minute._scheduled_close(day.replace(hour=9, minute=25))
    fifteen_minute = NiftyTrailingReversal(params, ctx, 900)
    assert fifteen_minute._scheduled_close(day.replace(hour=9, minute=15))


class LiveContext:
    def __init__(self):
        self.position = 0
        self.quantity = 65
        self.tick_size = None
        self.order_pending = False
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
    assert strategy._scheduled_close(starts(15, 25))  # explicit final 15:30 update
