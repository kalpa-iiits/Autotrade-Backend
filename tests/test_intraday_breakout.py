import asyncio
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.brokers import OrderError
from app.brokers.base import Candle
from app.strategies.base import Tick
from app.strategies.builtin.intraday_breakout import IntradayBreakout
from app.strategies.deployments import DEPLOYMENTS

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 10, 26)  # a Monday
SYM = "NSE:NIFTY26NOVFUT"


class FakeCtx:
    """Stands in for the runner's context. `live=True` mimics a real broker: the position only
    changes when fill() is called, not when the order is requested."""

    def __init__(self, day_range=100.0, live=False, history_error=None):
        self.symbol, self.position, self.calls, self.logs = SYM, 0, [], []
        self.live, self.history_error, self.day_range = live, history_error, day_range
        self.history_calls = 0

    async def buy(self, quantity=None, reason=None):
        self.calls.append(("buy", reason))
        if not self.live:
            self.position += 65

    async def sell(self, quantity=None, reason=None):
        self.calls.append(("sell", reason))
        if not self.live:
            self.position -= 65

    async def exit(self, reason=None):
        self.calls.append(("exit", reason))
        if not self.live:
            self.position = 0

    async def history(self, resolution="D", days=30):
        self.history_calls += 1
        if self.history_error:
            raise OrderError(self.history_error)
        # 20 previous weekdays with range day_range, plus today's (wide) candle to be ignored
        out, d = [], DAY
        while len(out) < 20:
            d -= timedelta(days=1)
            if d.weekday() < 5:
                out.append(Candle(datetime.combine(d, time(9, 15), IST), 0, 1000 + self.day_range, 1000, 0))
        out.reverse()
        out.append(Candle(datetime.combine(DAY, time(9, 15), IST), 0, 5000, 0, 0))
        return out

    def log(self, message):
        self.logs.append(message)


def make(ctx=None, **params):
    ctx = ctx or FakeCtx()
    strat = IntradayBreakout(IntradayBreakout.Params(**params), ctx)
    asyncio.run(strat.on_start())
    return strat, ctx


def at(hhmm: str, sec: int = 0, day: date = DAY) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m, sec), IST)


def ticks(strat, *items):
    """items: (hhmm, sec, price)"""
    async def go():
        for hhmm, sec, price in items:
            await strat.on_tick(Tick(SYM, price, at(hhmm, sec)))
    asyncio.run(go())


def quiet_minutes(strat, start: str, end: str, low=1000.0, high=1001.0, day=DAY):
    """One tick at low and one at high in every minute from start to end (inclusive)."""
    t, stop = at(start, day=day), at(end, day=day)
    async def go():
        nonlocal t
        while t <= stop:
            await strat.on_tick(Tick(SYM, low, t))
            await strat.on_tick(Tick(SYM, high, t + timedelta(seconds=30)))
            t += timedelta(minutes=1)
    asyncio.run(go())


def test_average_daily_range_excludes_today():
    strat, ctx = make()
    quiet_minutes(strat, "10:00", "10:01")
    assert strat.day_range == 100.0  # today's 5000-wide candle is ignored


def test_no_entries_before_window_then_long_breakout():
    strat, ctx = make()
    quiet_minutes(strat, "10:20", "10:28")
    assert strat.levels is None  # bars closing before 10:30 never arm
    ticks(strat, ("10:29", 0, 1000.5), ("10:30", 5, 1000.5))  # the 10:29 candle closes at 10:30
    assert strat.levels == (1001.0, 1000.0, 100.0)

    ticks(strat, ("10:30", 20, 1001.5))
    assert ctx.calls[0][0] == "buy" and ctx.position == 65
    assert strat.stop == pytest.approx(1001.5 - 0.25 * 100)
    assert strat.levels is None  # the sell side is cancelled


def test_short_breakout():
    strat, ctx = make()
    quiet_minutes(strat, "10:25", "10:29")
    ticks(strat, ("10:30", 5, 1000.5), ("10:30", 9, 999.0))
    assert ctx.calls[0][0] == "sell" and ctx.position == -65
    assert strat.stop == pytest.approx(999.0 + 25)


def test_trailing_stop_exits():
    strat, ctx = make()
    quiet_minutes(strat, "10:25", "10:29")
    ticks(strat, ("10:30", 5, 1000.5), ("10:30", 10, 1002.0))  # long at 1002, stop 977
    ticks(strat, ("10:31", 0, 1100.0))   # best 1100 -> trail 1060
    assert strat.stop == pytest.approx(1060.0)
    ticks(strat, ("10:31", 5, 1061.0))
    assert ctx.position == 65
    ticks(strat, ("10:31", 10, 1059.0))
    assert ctx.calls[-1][0] == "exit" and ctx.position == 0
    assert "stop" in ctx.calls[-1][1]


def test_initial_stop_exits():
    strat, ctx = make(trail_k=0)
    quiet_minutes(strat, "10:25", "10:29")
    ticks(strat, ("10:30", 5, 1000.5), ("10:30", 10, 1002.0), ("10:30", 20, 977.5))
    assert ctx.position == 65
    ticks(strat, ("10:30", 30, 976.9))
    assert ctx.calls[-1][0] == "exit"


def test_reentry_after_stop_out_and_daily_cap():
    strat, ctx = make(max_trades_per_day=2, trail_k=0)
    quiet_minutes(strat, "10:25", "10:29")
    ticks(strat, ("10:30", 5, 1000.5), ("10:30", 10, 1002.0), ("10:30", 20, 970.0))  # long, stopped
    # Flat again: the next candles re-arm. A touch of the low counts (like Pine's sell-stop),
    # so the 10:32 tick at 970 re-enters short.
    quiet_minutes(strat, "10:31", "10:36", 970, 971)
    assert [c[0] for c in ctx.calls] == ["buy", "exit", "sell"]
    assert strat.trades_today == 2
    ticks(strat, ("10:37", 0, 996.0))  # short stopped out (stop 995)
    quiet_minutes(strat, "10:38", "10:50", 996, 997)
    assert strat.levels is None  # cap of 2 reached: no more arming today
    assert [c[0] for c in ctx.calls] == ["buy", "exit", "sell", "exit"]


def test_wide_lookback_range_is_skipped():
    strat, ctx = make(max_width=0.4)  # 0.4 x 100 = 40
    quiet_minutes(strat, "10:25", "10:29", low=1000, high=1050)
    ticks(strat, ("10:30", 5, 1020.0))
    assert strat.levels is None
    strat2, _ = make(max_width=0)  # filter off
    quiet_minutes(strat2, "10:25", "10:29", low=1000, high=1050)
    ticks(strat2, ("10:30", 5, 1020.0))
    assert strat2.levels == (1050, 1000, 100.0)


def test_entry_window_and_square_off():
    strat, ctx = make()
    quiet_minutes(strat, "14:40", "14:44")
    ticks(strat, ("14:45", 5, 1000.5))
    assert strat.levels is not None  # candle closing at 14:45 still arms
    ticks(strat, ("14:45", 10, 1001.5))  # long
    quiet_minutes(strat, "14:46", "15:14", 1100, 1101)  # trending up, no stop-out
    assert ctx.position == 65
    ticks(strat, ("15:15", 0, 1100.5))
    assert ctx.calls[-1] == ("exit", "square-off at 15:15") and ctx.position == 0

    late, ctx2 = make()
    quiet_minutes(late, "14:41", "14:45")
    ticks(late, ("14:46", 5, 1000.5))
    assert late.levels is None  # closes after 14:45: no new entries


def test_clock_squares_off_without_ticks():
    ctx = FakeCtx(live=True)
    strat, _ = make(ctx)
    quiet_minutes(strat, "10:25", "10:29")
    ticks(strat, ("10:30", 5, 1000.5), ("10:30", 9, 999.0))  # short; then the feed goes quiet
    ctx.position = -65  # filled

    asyncio.run(strat.on_clock(at("15:14", 55)))
    assert ctx.calls[-1][0] == "sell"  # before square-off time: nothing
    asyncio.run(strat.on_clock(at("15:15", 2)))
    assert ctx.calls[-1] == ("exit", "square-off at 15:15")
    asyncio.run(strat.on_clock(at("15:15", 4)))
    assert len(ctx.calls) == 2  # not repeated while the exit is being filled
    asyncio.run(strat.on_clock(at("15:15", 8)))
    assert ctx.calls[-1] == ("exit", "square-off at 15:15")  # still open: asked again

    ctx.position = 0  # filled
    asyncio.run(strat.on_clock(at("15:15", 20)))
    assert len(ctx.calls) == 3


def test_no_history_means_no_trades_and_retries():
    ctx = FakeCtx(history_error="Broker not connected")
    strat, _ = make(ctx)
    quiet_minutes(strat, "10:25", "10:30")
    assert strat.levels is None and ctx.calls == []
    # 12 ticks, 5 closed candles: retried at most once a minute, never per tick
    assert ctx.history_calls == 5
    ctx.history_error = None  # broker connected again
    quiet_minutes(strat, "10:31", "10:31")
    assert strat.day_range == 100.0
    assert len(ctx.calls) == 1  # trading resumed (that tick sat on the low: short entry)


def test_new_day_resets_lookback_and_count():
    strat, ctx = make(max_trades_per_day=1)
    quiet_minutes(strat, "10:25", "10:29")
    ticks(strat, ("10:30", 5, 1000.5), ("10:30", 10, 1002.0), ("15:15", 0, 1002.0))  # trade, square-off
    assert strat.trades_today == 1
    next_day = DAY + timedelta(days=1)
    quiet_minutes(strat, "09:15", "09:18", day=next_day)  # only 3 candles of the new day closed
    assert strat.trades_today == 0 and len(strat.bars_today) == 3


def test_exit_not_repeated_while_live_fill_is_pending():
    ctx = FakeCtx(live=True)
    strat, _ = make(ctx, trail_k=0)
    quiet_minutes(strat, "10:25", "10:29")
    ticks(strat, ("10:30", 5, 1000.5), ("10:30", 10, 1002.0))
    ctx.position = 65  # the buy filled
    ticks(strat, ("10:30", 20, 970.0), ("10:30", 21, 969.0), ("10:30", 22, 968.0))
    assert [c[0] for c in ctx.calls] == ["buy", "exit"]  # one exit while waiting
    ticks(strat, ("10:30", 30, 967.0))
    assert [c[0] for c in ctx.calls] == ["buy", "exit", "exit"]  # retried after 5 s


def test_params_validation():
    with pytest.raises(ValueError, match="entries_from"):
        IntradayBreakout.Params(entries_from="15:00", entries_until="14:00")


def test_deployment_matches_the_tradingview_inputs():
    dep = next(d for d in DEPLOYMENTS if d.key == "nifty-1m-breakout")
    p = dep.params
    assert (p.lookback, p.max_width, p.avg_days, p.stop_k, p.trail_k, p.max_trades_per_day) == (
        5, 0.4, 14, 0.25, 0.40, 8)
    assert (p.entries_from, p.entries_until, p.square_off) == (time(10, 30), time(14, 45), time(15, 15))
    assert dep.quantity == 65


def test_history_through_the_real_runner(tmp_path, monkeypatch):
    """ctx.history() reaches the broker (paper here) and the strategy gets its day range."""
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app
    from app.strategies import deployments
    from app.strategies.deployment import Deployment
    from tests.conftest import AUTH, TOKEN, add_user, run

    monkeypatch.setattr(settings, "tick_interval", 3600)
    monkeypatch.setattr(deployments, "DEPLOYMENTS", [
        Deployment(key="brk", strategy=IntradayBreakout, symbol="NSE:SBIN-EQ", quantity=1)])
    with TestClient(app) as c:
        _, account_id = add_user(c, "admin", token=TOKEN)
        inst = c.post("/api/strategies", json={"deployment_key": "brk", "account_id": account_id,
                                               "symbol": "NSE:SBIN-EQ", "timeframe": "1m"}, headers=AUTH).json()
        c.post(f"/api/strategies/{inst['id']}/start", headers=AUTH)
        running = c.app.state.strategy_runner.running[inst["id"]]
        candles = run(c, running.strategy.ctx.history, "D", 30)
        assert len(candles) >= 14 and all(c_.high > c_.low for c_ in candles)
