import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.brokers.base import Candle
from app.strategies.backtest import Simulation
from app.strategies.builtin.nifty_trailing_reversal import NiftyTrailingReversal
from app.strategies.deployment import Deployment

IST = ZoneInfo("Asia/Kolkata")
SYM = "NSE:NIFTY50-INDEX"


def run(bars):
    dep = Deployment(
        key="nifty-reversal", strategy=NiftyTrailingReversal, symbol=SYM,
        timeframe="5m", quantity=65, max_order_qty=130, max_position=65,
        max_orders_per_day=100, backtest_fixed_quantity=65,
    )
    sim = Simulation(dep, SYM, "5m", daily=[])
    asyncio.run(sim.run(bars))
    return sim


def bar(at, open_, high, low, close):
    return Candle(at, open_, high, low, close)


def test_seed_stop_reversal_and_one_reversal_per_candle():
    seed = datetime(2021, 11, 8, 9, 20, tzinfo=IST)
    bars = [
        # Seed long at 25,000. Its 24,800 stop is reached; reverse short there. This candle's
        # 25,100 high would hit the new 24,998.40 short stop, but a second reversal is forbidden.
        bar(seed, 25_000, 25_100, 24_700, 24_800),
        bar(seed + timedelta(minutes=5), 24_800, 24_850, 24_700, 24_750),
    ]
    sim = run(bars)
    assert [(t.direction, t.entry_price, t.exit_price, t.quantity) for t in sim.trades] == [
        ("LONG", 25_000, 24_800, 65),
        ("SHORT", 24_800, 24_750, 65),
    ]


def test_gap_uses_adverse_open_and_scheduled_close_ratchets_next_stop():
    seed = datetime(2021, 11, 8, 9, 20, tzinfo=IST)
    bars = [
        # 09:25 scheduled close raises the long stop from 24,800 to 25,050 * .996 = 24,949.8.
        bar(seed, 25_000, 25_100, 24_990, 25_050),
        # Next candle gaps below that stop, so the adverse open is the reversal fill.
        bar(seed + timedelta(minutes=5), 24_900, 24_930, 24_800, 24_850),
    ]
    sim = run(bars)
    assert sim.trades[0].direction == "LONG"
    assert sim.trades[0].entry_price == 25_000
    assert sim.trades[0].exit_price == 24_900
    assert sim.trades[1].direction == "SHORT"


def test_no_seed_when_history_does_not_include_fixed_start():
    later = datetime(2022, 1, 3, 9, 20, tzinfo=IST)
    sim = run([bar(later, 20_000, 20_100, 19_900, 20_050)])
    assert sim.trades == []


def test_only_five_minute_timeframe_is_allowed():
    dep = Deployment(key="nifty-reversal", strategy=NiftyTrailingReversal, quantity=65)
    assert dep.timeframe == "5m"
    assert NiftyTrailingReversal.capital_sizing is False
    try:
        dep.check_timeframe("1m")
    except ValueError as exc:
        assert "supports 5m" in str(exc)
    else:
        raise AssertionError("1m should not be accepted")
