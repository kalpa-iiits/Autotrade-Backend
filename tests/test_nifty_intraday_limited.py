import asyncio
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.brokers.base import Candle
from app.orders.schemas import Product
from app.strategies.base import Bar, Tick
from app.strategies.builtin.nifty_intraday_limited import NiftyIntradayLimited
from app.strategies.deployments import DEPLOYMENTS

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 10, 26)


class ReplayCtx:
    def __init__(self):
        self.symbol = "NSE:NIFTY50-INDEX"
        self.position = 0
        self.quantity = 65
        self.last_price = None
        self.average_entry_price = None
        self.entries = []
        self.exits = []
        self.events = []

    def mark(self, price, at):
        self.last_price = price

    async def buy_at(self, price, at, reason=None, quantity=None):
        self.position = quantity or self.quantity
        self.average_entry_price = price
        self.entries.append(("LONG", price, at, reason))
        self.events.append(("LONG", price, at, reason))

    async def sell_at(self, price, at, reason=None, quantity=None):
        self.position = -(quantity or self.quantity)
        self.average_entry_price = price
        self.entries.append(("SHORT", price, at, reason))
        self.events.append(("SHORT", price, at, reason))

    async def exit_at(self, price, at, reason=None):
        self.exits.append((price, at, reason))
        self.events.append(("EXIT", price, at, reason))
        self.position = 0
        self.average_entry_price = None


class LiveCtx:
    def __init__(self):
        self.symbol = "NSE:NIFTY26NOVFUT"
        self.position = 0
        self.quantity = 65
        self.last_price = None
        self.last_fill_price = None
        self.order_pending = False
        self.strategy_state = {}
        self.calls = []
        self.products = []
        self.history_candles = []
        self.logs = []
        self.entry_orders_available = True

    async def buy(self, quantity=None, reason=None, product=None):
        self.calls.append(("buy", reason))
        self.products.append(product)
        self.position += quantity or self.quantity
        self.last_fill_price = self.last_price

    async def sell(self, quantity=None, reason=None, product=None):
        self.calls.append(("sell", reason))
        self.products.append(product)
        self.position -= quantity or self.quantity
        self.last_fill_price = self.last_price

    async def exit(self, reason=None, product=None):
        self.calls.append(("exit", reason))
        self.products.append(product)
        self.position = 0

    async def save_strategy_state(self, state):
        self.strategy_state = state

    async def history(self, resolution="D", days=30):
        assert resolution == "1"
        return self.history_candles

    def log(self, message):
        self.logs.append(message)


def at(hhmm: str, day=DAY):
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), IST)


def candle(hhmm: str, close: float, *, open=None, high=None, low=None, day=DAY):
    open = close if open is None else open
    high = max(open, close) if high is None else high
    low = min(open, close) if low is None else low
    return Candle(at(hhmm, day), open, high, low, close)


def make(**params):
    ctx = ReplayCtx()
    values = {"lookback": 2, "stop_points": 10, "target_points": 20} | params
    strategy = NiftyIntradayLimited(
        NiftyIntradayLimited.Params(**values),
        ctx,
        300,
    )
    asyncio.run(strategy.on_start())
    return strategy, ctx


def replay(strategy, *candles):
    async def go():
        for value in candles:
            await strategy.on_backtest_candle(value)
    asyncio.run(go())


def minute_rows(start: datetime, closes: list[float]):
    rows = []
    previous = closes[0]
    for index, close in enumerate(closes):
        opened = previous
        rows.append(Candle(
            start + timedelta(minutes=index),
            opened,
            max(opened, close),
            min(opened, close),
            close,
        ))
        previous = close
    return rows


def warm_to_signal(strategy, signal_close):
    rows = []
    current = at("09:15")
    while current.time() < time(10, 45):
        rows.append(Candle(current, 100, 100, 100, 100))
        current += timedelta(minutes=5)
    rows.append(candle("10:45", signal_close, open=100))
    replay(strategy, *rows)


def test_long_signal_fills_next_open_and_target_uses_entry_fill():
    strategy, ctx = make()
    warm_to_signal(strategy, 110)
    assert ctx.entries == []
    assert strategy._entry_pending == 1

    replay(strategy, candle("10:50", 125, open=120, high=145, low=118))

    assert [(side, price) for side, price, *_ in ctx.entries] == [("LONG", 120)]
    assert ctx.exits[0][0] == 140
    assert "target" in ctx.exits[0][2]
    assert ctx.position == 0


def test_short_signal_fills_next_open_and_target_uses_entry_fill():
    strategy, ctx = make()
    warm_to_signal(strategy, 90)
    assert strategy._entry_pending == -1

    replay(strategy, candle("10:50", 60, open=80, high=82, low=55))

    assert [(side, price) for side, price, *_ in ctx.entries] == [("SHORT", 80)]
    assert ctx.exits[0][0] == 60
    assert "target" in ctx.exits[0][2]


def test_ambiguous_candle_records_stop_even_when_target_is_nearer_open():
    strategy, ctx = make(stop_points=300, target_points=50)
    warm_to_signal(strategy, 110)

    replay(strategy, candle("10:50", 100, open=100, high=160, low=-210))

    assert ctx.exits[0][0] == -200
    assert "stop-first" in ctx.exits[0][2]


def test_daily_entry_cap_counts_queued_entries():
    strategy, ctx = make(max_entries_per_day=1)
    warm_to_signal(strategy, 110)
    replay(strategy, candle("10:50", 125, open=120, high=145, low=118))  # first target
    replay(strategy, candle("10:55", 50, open=125, high=125, low=50))  # strong short signal
    assert strategy._entry_pending == 0
    replay(strategy, candle("11:00", 40, open=50, high=50, low=40))
    assert len(ctx.entries) == 1


def test_1510_decision_exits_at_1515_open():
    strategy, ctx = make(stop_points=1_000, target_points=1_000)
    warm_to_signal(strategy, 110)
    replay(strategy, candle("10:50", 120, open=120, high=125, low=115))
    assert ctx.position == 65

    current = at("10:55")
    while current.time() <= time(15, 10):
        replay(strategy, Candle(current, 120, 125, 115, 120))
        current += timedelta(minutes=5)
    assert strategy._exit_pending and ctx.position == 65

    replay(strategy, candle("15:15", 131, open=130, high=132, low=129))
    assert ctx.exits[-1][0] == 130
    assert "15:15" in ctx.exits[-1][2]
    assert ctx.position == 0


def test_strong_session_move_opens_overnight_and_exits_next_session_open():
    strategy, ctx = make()
    replay(strategy, candle("09:15", 24_000, open=24_000))
    replay(strategy, candle("15:15", 24_085, open=24_085))

    assert ctx.entries[-1][:2] == ("LONG", 24_085)
    assert strategy._trade_kind == "overnight"
    assert strategy.stop is strategy.target is None

    next_day = DAY + timedelta(days=1)
    replay(strategy, candle("09:15", 24_020, open=24_020, day=next_day))
    assert ctx.exits[-1][0] == 24_020
    assert "overnight exit" in ctx.exits[-1][2]
    assert ctx.position == 0


def test_quiet_session_follows_completed_prior_overnight_gap():
    strategy, ctx = make()
    # Establish a previous 15:15 price. Its strong first-day move also opens an overnight long.
    replay(strategy, candle("09:15", 23_900, open=23_900))
    replay(strategy, candle("15:15", 24_000, open=24_000))

    next_day = DAY + timedelta(days=1)
    replay(strategy, candle("09:15", 23_950, open=23_950, day=next_day))  # G = -50
    replay(strategy, candle("15:15", 23_980, open=23_980, day=next_day))  # D = +30

    assert ctx.entries[-1][:2] == ("SHORT", 23_980)
    assert strategy._trade_kind == "overnight"


def test_zero_selected_overnight_value_stays_flat():
    strategy, ctx = make()
    strategy.previous_1515 = 24_000
    replay(strategy, candle("09:15", 24_000, open=24_000))
    replay(strategy, candle("15:15", 24_000, open=24_000))
    assert ctx.entries == []
    assert ctx.position == 0


def test_weekend_clock_does_not_start_an_overnight_trade():
    ctx = LiveCtx()
    ctx.last_price = 24_000
    strategy = NiftyIntradayLimited(
        NiftyIntradayLimited.Params(lookback=2, stop_points=10, target_points=20),
        ctx,
        300,
    )
    asyncio.run(strategy.on_start())

    saturday = datetime(2026, 11, 14, 15, 15, tzinfo=IST)
    asyncio.run(strategy.on_clock(saturday))

    assert ctx.calls == []
    assert strategy._overnight_pending == 0


def test_exhausted_order_budget_abandons_pending_overnight_entry():
    ctx = LiveCtx()
    ctx.entry_orders_available = False
    ctx.last_price = 24_000
    strategy = NiftyIntradayLimited(
        NiftyIntradayLimited.Params(lookback=2, stop_points=10, target_points=20),
        ctx,
        300,
    )
    asyncio.run(strategy.on_start())
    strategy.day = DAY
    strategy.session_open = 23_000
    strategy._overnight_pending = 1

    asyncio.run(strategy._maybe_open_overnight(at("15:15")))
    asyncio.run(strategy._maybe_open_overnight(at("15:16")))

    assert ctx.calls == []
    assert strategy._overnight_pending == 0
    assert ctx.logs == ["overnight entry skipped: daily order limit reached"]


def test_live_sequence_exits_intraday_before_opening_overnight():
    ctx = LiveCtx()
    strategy = NiftyIntradayLimited(NiftyIntradayLimited.Params(), ctx, 300)
    asyncio.run(strategy.on_start())
    strategy._new_session(DAY)
    strategy.session_open = 24_000
    strategy._install_bracket(24_050, 1)
    ctx.position = 65
    ctx.last_price = 24_085

    asyncio.run(strategy._begin_overnight(24_085, at("15:15")))

    assert [call[0] for call in ctx.calls] == ["exit", "buy"]
    assert ctx.products == [Product.INTRADAY, Product.DELIVERY]
    assert ctx.position == 65
    assert strategy._trade_kind == "overnight"
    assert strategy.stop is strategy.target is None

    next_open = at("09:15", DAY + timedelta(days=1))
    ctx.last_price = 24_020
    asyncio.run(strategy.on_tick(Tick(ctx.symbol, 24_020, next_open)))
    assert ctx.calls[-1][0] == "exit"
    assert ctx.products[-1] == Product.DELIVERY
    assert ctx.position == 0


def test_live_signal_uses_completed_broker_ohlc_not_streamed_bar():
    ctx = LiveCtx()
    strategy = NiftyIntradayLimited(NiftyIntradayLimited.Params(), ctx, 300)
    asyncio.run(strategy.on_start())
    strategy._new_session(DAY)
    strategy.session_open = 100
    strategy.closes.extend([100] * 17)
    strategy.ema4 = strategy.ema10 = 100
    ctx.last_price = 120
    ctx.history_candles = minute_rows(at("10:45"), [100, 100, 100, 100, 110])

    streamed = Bar(ctx.symbol, at("10:45"), 300, 100, 100, 50, 50, 2)
    asyncio.run(strategy.on_bar(streamed))

    assert ctx.calls == [("buy", "long z-score/EMA signal")]
    assert ctx.position == 65
    assert strategy.stop == 70 and strategy.target == 420
    assert strategy.closes[-1] == 110


def test_live_waits_and_retries_when_completed_history_is_not_published():
    ctx = LiveCtx()
    strategy = NiftyIntradayLimited(NiftyIntradayLimited.Params(), ctx, 300)
    asyncio.run(strategy.on_start())
    strategy._new_session(DAY)
    strategy.session_open = 100
    strategy.closes.extend([100] * 17)
    strategy.ema4 = strategy.ema10 = 100
    ctx.last_price = 120
    rows = minute_rows(at("10:45"), [100, 100, 100, 100, 110])
    ctx.history_candles = rows[:4]

    streamed = Bar(ctx.symbol, at("10:45"), 300, 100, 110, 100, 110, 4)
    asyncio.run(strategy.on_bar(streamed))
    assert ctx.calls == []
    assert strategy._pending_bar_closes == [at("10:50")]

    ctx.history_candles = rows
    asyncio.run(strategy._retry_pending_bars(at("10:50") + timedelta(seconds=6)))
    assert ctx.calls == [("buy", "long z-score/EMA signal")]
    assert strategy._pending_bar_closes == []


def test_new_deployment_matches_supplied_pine_inputs():
    deployment = next(d for d in DEPLOYMENTS if d.key == "nifty-intraday-limited")
    params = deployment.params
    assert deployment.timeframe == "5m"
    assert deployment.quantity == deployment.backtest_fixed_quantity == 65
    assert deployment.max_order_qty == deployment.max_position == 65
    assert deployment.strategy.capital_sizing is False
    assert deployment.backtest_initial_capital == 500_000
    assert (
        params.lookback,
        params.z_threshold,
        params.stop_points,
        params.target_points,
        params.max_entries_per_day,
    ) == (18, 1.0, 50, 300, 3)


def test_parameter_validation():
    with pytest.raises(ValueError):
        NiftyIntradayLimited.Params(max_entries_per_day=4)
