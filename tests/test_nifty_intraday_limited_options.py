import asyncio
from datetime import date

from app.brokers.base import OptionContract
from app.strategies.builtin.nifty_intraday_limited_options import (
    LimitedTradeOptionLegs,
    NiftyIntradayLimitedOptions,
)
from app.strategies.deployments import DEPLOYMENTS

TODAY = date(2026, 10, 27)
NEXT_EXPIRY = date(2026, 11, 3)


def chain():
    return [
        OptionContract(
            f"NSE:NIFTY{expiry:%d%m}{strike}{kind}", kind, float(strike), expiry, 65,
        )
        for expiry in (TODAY, NEXT_EXPIRY)
        for strike in range(24_000, 26_001, 50)
        for kind in ("CE", "PE")
    ]


class FakeCtx:
    def __init__(self):
        self.position = 0
        self.quantity = 65
        self.last_price = 25_000
        self.held_symbol = None
        self.order_pending = False
        self.calls = []
        self.logs = []

    async def option_contracts(self):
        return chain()

    async def buy(self, quantity=None, reason=None, symbol=None):
        self.calls.append(("BUY", quantity, symbol, reason))
        self.position += quantity
        self.held_symbol = symbol

    async def sell(self, quantity=None, reason=None, symbol=None):
        self.calls.append(("SELL", quantity, symbol, reason))
        self.position -= quantity

    def log(self, message):
        self.logs.append(message)


def fixed_now(monkeypatch):
    import app.strategies.builtin.nifty_intraday_limited_options as module

    class Now:
        @staticmethod
        def now(tz=None):
            from datetime import datetime
            return datetime(2026, 10, 27, 12, 0, tzinfo=tz)

    monkeypatch.setattr(module, "datetime", Now)


def test_intraday_long_and_short_buy_500_point_itm_options(monkeypatch):
    fixed_now(monkeypatch)

    async def go():
        ctx = FakeCtx()
        legs = LimitedTradeOptionLegs(ctx, 500)
        await legs.buy(reason="long z-score/EMA signal")
        await legs.exit("target")
        await legs.sell(reason="short z-score/EMA signal")
        await legs.exit("stop")
        return ctx

    ctx = asyncio.run(go())
    assert [(side, symbol) for side, _, symbol, _ in ctx.calls] == [
        ("BUY", "NSE:NIFTY271024500CE"),
        ("SELL", "NSE:NIFTY271024500CE"),
        ("BUY", "NSE:NIFTY271025500PE"),
        ("SELL", "NSE:NIFTY271025500PE"),
    ]
    assert all(quantity == 65 for _, quantity, _, _ in ctx.calls)


def test_overnight_option_skips_same_day_expiry(monkeypatch):
    fixed_now(monkeypatch)

    async def go():
        ctx = FakeCtx()
        legs = LimitedTradeOptionLegs(ctx, 500)
        await legs.buy(reason="overnight long at 15:15")
        return ctx

    ctx = asyncio.run(go())
    assert ctx.calls[0][2] == "NSE:NIFTY031124500CE"
    assert "500 points ITM" in ctx.calls[0][3]


def test_option_adapter_keeps_stops_in_nifty_points(monkeypatch):
    fixed_now(monkeypatch)
    ctx = FakeCtx()
    legs = LimitedTradeOptionLegs(ctx, 500)
    assert legs.last_fill_price is None


def test_limited_trade_deployment_is_the_500_point_itm_option_version():
    deployment = next(d for d in DEPLOYMENTS if d.key == "nifty-intraday-limited")
    assert deployment.strategy is NiftyIntradayLimitedOptions
    assert deployment.symbol == "NSE:NIFTY50-INDEX"
    assert deployment.params.itm_points == 500
    assert deployment.quantity == deployment.max_order_qty == deployment.max_position == 65
    assert deployment.backtest_fixed_quantity == 65


def test_rules_describe_500_point_itm_ce_and_pe_execution():
    rules = NiftyIntradayLimitedOptions.rules(NiftyIntradayLimitedOptions.Params())
    entries = [rule for rule in rules if rule["kind"] in ("entry_long", "entry_short")]
    assert any("intraday call" in rule["title"].lower() for rule in entries)
    assert any("overnight call" in rule["title"].lower() for rule in entries)
    assert any("intraday put" in rule["title"].lower() for rule in entries)
    assert any("overnight put" in rule["title"].lower() for rule in entries)
    assert all(any("500" in condition for condition in rule["conditions"]) for rule in entries)
