import asyncio
from datetime import date

from app.brokers.base import OptionContract
from app.strategies.builtin.intraday_breakout_options import IntradayBreakoutOptions, OptionLegs, pick_contract

TODAY = date(2026, 10, 27)  # a weekly expiry day


def chain():
    out = []
    for expiry in (date(2026, 10, 27), date(2026, 11, 3), date(2026, 11, 9)):
        for strike in range(24500, 25600, 50):
            for kind in ("CE", "PE"):
                out.append(OptionContract(f"NSE:NIFTY{expiry:%d%m}{strike}{kind}", kind, float(strike), expiry, 65))
    return out


def test_picks_300_points_in_the_money_on_the_next_expiry():
    ce = pick_contract(chain(), "CE", 25012.4, 300, TODAY)
    pe = pick_contract(chain(), "PE", 25012.4, 300, TODAY)
    assert (ce.kind, ce.strike, ce.expiry) == ("CE", 24700.0, date(2026, 11, 3))  # not today's expiry
    assert (pe.kind, pe.strike, pe.expiry) == ("PE", 25300.0, date(2026, 11, 3))
    # Halfway between strikes: the deeper in-the-money one.
    assert pick_contract(chain(), "CE", 25025.0, 300, TODAY).strike == 24700.0
    assert pick_contract(chain(), "PE", 25025.0, 300, TODAY).strike == 25350.0
    assert pick_contract(chain(), "CE", 25000, 300, date(2026, 11, 9)) is None  # nothing after the last expiry


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
