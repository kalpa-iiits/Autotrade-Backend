import asyncio
from datetime import date, datetime, time, timedelta

import pytest

from app.brokers.base import OptionContract
from app.strategies.base import Tick
from app.strategies.builtin.intraday_breakout import IST
from app.strategies.builtin.intraday_breakout_options import IntradayBreakoutOptions, OptionLegs, pick_contract

TODAY = date(2026, 10, 27)  # a weekly expiry day


def chain():
    out = []
    for expiry in (date(2026, 10, 27), date(2026, 11, 3), date(2026, 11, 9)):
        for strike in range(24500, 25600, 50):
            for kind in ("CE", "PE"):
                out.append(OptionContract(f"NSE:NIFTY{expiry:%d%m}{strike}{kind}", kind, float(strike), expiry, 65))
    return out


def test_picks_300_points_in_the_money_on_todays_expiry():
    ce = pick_contract(chain(), "CE", 25012.4, 300, TODAY)
    pe = pick_contract(chain(), "PE", 25012.4, 300, TODAY)
    assert (ce.kind, ce.strike, ce.expiry) == ("CE", 24700.0, TODAY)
    assert (pe.kind, pe.strike, pe.expiry) == ("PE", 25300.0, TODAY)
    # Halfway between strikes: the deeper in-the-money one.
    assert pick_contract(chain(), "CE", 25025.0, 300, TODAY).strike == 24700.0
    assert pick_contract(chain(), "PE", 25025.0, 300, TODAY).strike == 25350.0
    assert pick_contract(chain(), "CE", 25000, 300, date(2026, 11, 10)) is None  # nothing after the last expiry


class FakeCtx:
    """The real context as OptionLegs sees it: one net position, whatever the symbol."""

    def __init__(self, quantity=65):
        self.position, self.quantity, self.last_price = 0, quantity, 25012.4
        self.held_symbol, self.calls, self.logs = None, [], []

    async def option_contracts(self):
        return chain()

    async def buy(self, quantity=None, reason=None, symbol=None):
        self.calls.append(("BUY", quantity, symbol))
        self.position += quantity

    async def sell(self, quantity=None, reason=None, symbol=None):
        self.calls.append(("SELL", quantity, symbol))
        self.position -= quantity

    def log(self, message):
        self.logs.append(message)


def test_index_signals_become_option_buys_and_sells(monkeypatch):
    import app.strategies.builtin.intraday_breakout_options as mod

    class Now:
        @staticmethod
        def now(tz=None):
            from datetime import datetime
            return datetime(2026, 10, 28, 11, 0, tzinfo=tz)
    monkeypatch.setattr(mod, "datetime", Now)

    async def go():
        ctx = FakeCtx(quantity=140)  # 2 lots and change: trades 130
        legs = OptionLegs(ctx, 300)
        await legs.buy(reason="broke above")
        assert legs.position == 130
        await legs.sell(reason="stop")  # the long's exit sells the call
        assert legs.position == 0
        await legs.sell(reason="broke below")
        assert legs.position == -130  # holding a put = short the index
        await legs.exit("square-off")
        return ctx

    ctx = asyncio.run(go())
    assert ctx.calls == [("BUY", 130, "NSE:NIFTY031124700CE"), ("SELL", 130, "NSE:NIFTY031124700CE"),
                         ("BUY", 130, "NSE:NIFTY031125300PE"), ("SELL", 130, "NSE:NIFTY031125300PE")]


def test_less_than_a_lot_is_skipped():
    async def go():
        ctx = FakeCtx(quantity=50)
        legs = OptionLegs(ctx, 300)
        await legs.buy(reason="broke above")
        return ctx

    ctx = asyncio.run(go())
    assert ctx.calls == [] and "less than one lot" in ctx.logs[0]


def test_rules_and_settings_describe_the_option_trades():
    rules = {r["kind"]: r for r in IntradayBreakoutOptions.rules(IntradayBreakoutOptions.Params())}
    assert rules["entry_long"]["title"] == "Buy a call (CE)" and rules["entry_short"]["title"] == "Buy a put (PE)"
    assert IntradayBreakoutOptions.capital_sizing is False and IntradayBreakoutOptions.backtest_note


def test_restart_restores_exact_option_stop_and_opening_gap_exits():
    today = datetime.now(IST).date()
    params = IntradayBreakoutOptions.Params()

    class PersistentCtx(FakeCtx):
        def __init__(self):
            super().__init__()
            self.symbol = "NSE:NIFTY50-INDEX"
            self.position = 65
            self.held_symbol = "NSE:NIFTY071124700CE"
            self.tick_size = 0.05
            self.order_pending = False
            self.saved = []
            self.strategy_state = {
                "version": 1,
                "strategy": "intraday_breakout_options",
                "bar_seconds": 60,
                "params": params.model_dump(mode="json"),
                "day": today.isoformat(),
                "day_range": 100.0,
                "bars_today": [],
                "trades_today": 1,
                "levels": None,
                "direction": 1,
                "stop": 24_900.0,
                "best": 25_100.0,
                "trade_range": 100.0,
                "entry_price": 25_000.0,
                "entry_pending_close": False,
                "exit_sent_at": None,
            }

        async def save_strategy_state(self, state):
            self.strategy_state = dict(state)
            self.saved.append(dict(state))

    ctx = PersistentCtx()
    strategy = IntradayBreakoutOptions(params, ctx, 60)
    opened = datetime.combine(today, time(9, 15), tzinfo=IST)

    async def go():
        await strategy.on_start()
        assert strategy.stop == 24_900
        await strategy.on_tick(Tick(ctx.symbol, 25_000, opened))
        assert ctx.saved == []  # harmless ticks remain database-free
        await strategy.on_tick(Tick(ctx.symbol, 24_800, opened + timedelta(seconds=1)))

    asyncio.run(go())
    assert ctx.calls == [("SELL", 65, "NSE:NIFTY071124700CE")]
    assert ctx.saved[-1]["stop"] == 24_900
    assert ctx.saved[-1]["exit_sent_at"] is not None


def test_existing_option_position_without_checkpoint_refuses_unsafe_start():
    ctx = FakeCtx()
    ctx.position = 65
    ctx.held_symbol = "NSE:NIFTY071124700CE"
    strategy = IntradayBreakoutOptions(IntradayBreakoutOptions.Params(), ctx, 60)

    with pytest.raises(RuntimeError, match="no compatible intraday checkpoint"):
        asyncio.run(strategy.on_start())
