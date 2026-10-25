import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app.brokers.base import Candle
from app.strategies.backtest import Simulation, _aggregate, summarize
from app.strategies.base import Bar, Strategy
from app.strategies.builtin.sma_crossover import SmaCrossover
from app.strategies.builtin.intraday_breakout import IntradayBreakout
from app.strategies.deployment import Deployment
from tests.test_strategies import AUTH, SYM, deploy  # noqa: F401  (fixture)
from tests.conftest import ACCOUNT

IST = ZoneInfo("Asia/Kolkata")


class EveryBar(Strategy):
    """Test strategy: long on even bars, flat on odd ones."""
    name = "test_every_bar"

    class Params(BaseModel):
        pass

    async def on_start(self) -> None:
        self.n = 0

    async def on_bar(self, bar: Bar) -> None:
        self.n += 1
        if self.ctx.position == 0:
            await self.ctx.buy(reason="even")
        else:
            await self.ctx.exit(reason="odd")


def candles(closes, start=datetime(2026, 10, 20, 4, 0, tzinfo=timezone.utc)):
    return [Candle(start + timedelta(minutes=i), c, c, c, c) for i, c in enumerate(closes)]


def simulate(strategy=EveryBar, closes=(100, 101, 102, 103, 104, 105), capital=250, size_pct=100, **dep_fields):
    """₹250 at ~₹100 a unit: 2 units a trade."""
    dep = Deployment(**{"key": "t", "strategy": strategy, "symbol": SYM, "quantity": 1} | dep_fields)
    sim = Simulation(dep, SYM, "1m", daily=[], capital=capital, size_pct=size_pct)
    asyncio.run(sim.run(candles(closes)))
    return sim


def test_fills_at_the_triggering_price_and_closes_at_the_end():
    sim = simulate()
    # Each bar closes when the next minute's first tick arrives: the order fills at that price.
    assert [(t.entry_price, t.exit_price, t.quantity) for t in sim.trades] == [
        (101, 102, 2), (103, 104, 2), (105, 105, 2)]
    assert sim.trades[-1].exit_reason == "end of backtest"
    assert sim.position == 0


def test_size_comes_from_equity_and_compounds():
    sim = simulate(closes=(100, 100, 200, 100, 100), capital=1000)
    # 1000 buys 10 at 100; sold at 200 (+1000). The next entry at 100 has 2000: 20 units.
    assert [t.quantity for t in sim.trades][:2] == [10, 20]
    half = simulate(closes=(100, 100, 200, 200), capital=1000, size_pct=50)
    assert half.trades[0].quantity == 5
    broke = simulate(capital=50)  # can't pay for one unit
    assert broke.trades == [] and broke.blocked > 0


def test_risk_limits_apply_but_never_block_exits():
    sim = simulate(closes=range(100, 120), max_orders_per_day=3)
    assert len(sim.trades) == 2  # buy, sell, buy (3 orders), then the exit at the end
    assert sim.blocked > 0


def test_summary():
    sim = simulate(closes=(100, 101, 103, 104, 101))
    s = summarize(sim.trades, capital=1000)
    # Trades: 101 -> 103 (+4), 104 -> 101 (-6)
    assert (s["trades"], s["wins"], s["losses"], s["net_pnl"]) == (2, 1, 1, -2)
    assert s["win_rate"] == 50 and s["net_pnl_pct"] == -0.2
    assert s["max_drawdown"] == 6 and s["max_drawdown_pct"] == round(6 / 1004 * 100, 2)
    assert s["profit_factor"] == round(4 / 6, 2)


def test_trade_excursions():
    # Long at the second candle's open (101); that candle dips to 95 and spikes to 110.
    c = candles((100, 101, 101, 101))
    c[1] = Candle(c[1].start, 101, 110, 95, 101)
    dep = Deployment(key="t", strategy=EveryBar, symbol=SYM, quantity=1)
    sim = Simulation(dep, SYM, "1m", daily=[], capital=250)
    asyncio.run(sim.run(c))
    first = sim.trades[0]
    assert (first.entry_price, first.run_up, first.drawdown) == (101, 18, 12)


def test_minute_candles_aggregate_to_selected_timeframe():
    source = candles(range(100, 116), start=datetime(2026, 10, 20, 4, 0, tzinfo=timezone.utc))
    grouped = _aggregate(source, 15 * 60)
    assert len(grouped) == 2
    assert (grouped[0].open, grouped[0].high, grouped[0].low, grouped[0].close) == (100, 114, 100, 114)
    assert (grouped[1].open, grouped[1].close) == (115, 115)


def test_intraday_breakout_matches_pine_stop_fills_fixed_size_and_commission():
    day = datetime(2026, 10, 26, tzinfo=IST)
    daily = [Candle(day - timedelta(days=i), 1000, 1100, 1000, 1050) for i in range(20, 0, -1)]
    minute_bars = []
    for minute in range(25, 30):
        at = day.replace(hour=10, minute=minute)
        minute_bars.append(Candle(at, 1000.5, 1001, 1000, 1000.5))
    # The resting buy-stop fills at 1001 (not this candle's 1005 high). TradingView's
    # open->high->low path then hits the pre-placed 976 stop in the same candle.
    minute_bars.append(Candle(day.replace(hour=10, minute=30), 1000.5, 1005, 970, 980))
    dep = Deployment(
        key="pine-breakout", strategy=IntradayBreakout, symbol=SYM, timeframe="1m", quantity=65,
        backtest_fixed_quantity=65, backtest_commission_per_unit=1,
    )
    sim = Simulation(dep, SYM, "1m", daily=daily, capital=100_000)
    asyncio.run(sim.run(minute_bars))

    first = sim.trades[0]
    assert (first.direction, first.quantity, first.entry_price, first.exit_price) == ("LONG", 65, 1001, 976)
    assert first.commission == 130
    assert first.pnl == (976 - 1001) * 65 - 130


def test_intraday_breakout_rounds_stops_to_the_tick_like_pine():
    # Average range 99.9 puts the stop at 1001 - 24.975 = 976.025: off the 0.05 grid.
    day = datetime(2026, 10, 26, tzinfo=IST)
    daily = [Candle(day - timedelta(days=i), 1000, 1099.9, 1000, 1050) for i in range(20, 0, -1)]
    minute_bars = [Candle(day.replace(hour=10, minute=m), 1000.5, 1001, 1000, 1000.5) for m in range(25, 30)]
    minute_bars.append(Candle(day.replace(hour=10, minute=30), 1000.5, 1005, 970, 980))
    dep = Deployment(key="pine-breakout", strategy=IntradayBreakout, symbol=SYM, timeframe="1m", quantity=65,
                     backtest_fixed_quantity=65)

    def exit_price(tick_size):
        sim = Simulation(dep, SYM, "1m", daily=daily, capital=100_000, tick_size=tick_size)
        asyncio.run(sim.run(minute_bars))
        return sim.trades[0].exit_price

    assert exit_price(None) == 976.025  # tick unknown: unrounded
    assert exit_price(0.05) == 976.0  # long stop rounded down


def test_tick_stop_rounds_against_the_position():
    strategy = object.__new__(IntradayBreakout)
    strategy.ctx = type("Ctx", (), {"tick_size": 0.05})()
    assert strategy._tick_stop(706.852, long=True) == 706.85
    assert strategy._tick_stop(689.74, long=True) == 689.7
    assert strategy._tick_stop(685.43, long=False) == 685.45
    assert strategy._tick_stop(697.85, long=False) == 697.85  # already on the grid
    strategy.ctx.tick_size = None
    assert strategy._tick_stop(685.43, long=False) == 685.43


def test_catalog_and_performance(deploy):
    c = deploy(Deployment(key="sma", strategy=SmaCrossover, symbol=SYM, quantity=1,
                          params={"fast": 3, "slow": 8, "allow_short": True}))
    [item] = c.get("/api/strategies/catalog", headers=AUTH).json()
    assert item["key"] == "sma" and item["tags"]
    assert {r["kind"] for r in item["rules"]} >= {"entry_long", "entry_short", "exit"}
    assert {p["name"]: p["value"] for p in item["params"]} == {"fast": 3, "slow": 8, "allow_short": True}
    assert c.get("/api/strategies/catalog/nope", headers=AUTH).status_code == 404

    today = datetime.now(IST).date()
    q = f"account_id={ACCOUNT}&symbol={SYM}&start={today - timedelta(days=30)}&end={today}"
    r = c.get(f"/api/strategies/catalog/sma/performance?{q}&timeframe=15m&capital=100000", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    s = body["summary"]
    assert s["all"]["trades"] == len(body["trades"]) == s["long"]["trades"] + s["short"]["trades"] > 0
    assert body["trades"][-1]["cum_pnl"] == s["all"]["net_pnl"]
    assert body["capital"] == 100000 and body["size_pct"] == 100 and body["buy_hold"][0]["pnl"] == 0
    # Sized from the capital, not the deployment's quantity of 1.
    assert body["trades"][0]["quantity"] == int(100000 // body["trades"][0]["entry_price"])

    fixed = c.get(f"/api/strategies/catalog/sma/performance?{q}&timeframe=15m&size_mode=fixed&quantity=7", headers=AUTH)
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["fixed_quantity"] == 7 and {t["quantity"] for t in fixed.json()["trades"]} == {7}
    assert c.get(f"/api/strategies/catalog/sma/performance?{q}&timeframe=15m&size_mode=fixed", headers=AUTH).status_code == 422
    assert all(t["entry_at"] >= body["start"] for t in body["trades"])

    m = c.get(f"/api/strategies/catalog/sma/performance/matrix?{q}&capital=100000", headers=AUTH).json()
    assert set(m["timeframes"]) == set(item["timeframes"])
    assert m["timeframes"]["15m"] == s["all"]

    def status(query):
        return c.get(f"/api/strategies/catalog/sma/performance?{query}", headers=AUTH).status_code

    assert status(f"account_id={ACCOUNT}&symbol=NSE:NOPE&timeframe=1m") == 422
    assert status(f"account_id={ACCOUNT}&symbol={SYM}&timeframe=1m&start={today}&end={today - timedelta(days=1)}") == 422
    assert status(f"account_id={ACCOUNT}&symbol={SYM}&timeframe=1m&start={today - timedelta(days=900)}") == 422
    assert status(f"account_id=999&symbol={SYM}&timeframe=1m") == 404
