"""Intraday breakout on an index, traded with in-the-money options.

The signals are Intraday Breakout's, computed on the index itself (NIFTY 50): its levels,
stops, trailing stop and 15:15 square-off are all in index points. Only the instrument traded
changes:
  - long signal  -> buy a call (CE) `itm_points` below the index (in the money)
  - short signal -> buy a put (PE) `itm_points` above the index (in the money)
  - the index strategy's exit (stop, trailing stop or square-off) -> sell that same option.
Options are only ever bought, never written. The contract is the nearest weekly expiry; on its
expiry day the next one is used, so it never holds a contract expiring that afternoon.

Strategy Tester replays the index signals and reports index points x quantity: option premiums
of past contracts aren't available, so the option P&L itself isn't simulated.
"""

from datetime import datetime

from pydantic import Field

from app.brokers.base import OptionContract
from app.strategies.builtin.intraday_breakout import IST, IntradayBreakout


class OptionLegs:
    """What Intraday Breakout's live logic sees as its context: buy/sell/exit on the index
    become buying and selling options. Its position is signed like the index trade (+ while
    holding a call, - while holding a put); everything else is the real context's."""

    def __init__(self, ctx, itm_points: float) -> None:
        self._ctx = ctx
        self._itm = itm_points
        self.held = ctx.held_symbol  # after a restart: the option it still holds

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    @property
    def position(self) -> int:
        units = self._ctx.position
        return -units if units and self.held and self.held.endswith("PE") else units

    async def buy(self, quantity: int | None = None, reason: str | None = None) -> None:
        if self.position < 0:
            await self.exit(reason)  # covering the short = selling the put
        elif self.position == 0:
            await self._open("CE", reason)

    async def sell(self, quantity: int | None = None, reason: str | None = None) -> None:
        if self.position > 0:
            await self.exit(reason)
        elif self.position == 0:
            await self._open("PE", reason)

    async def exit(self, reason: str | None = None) -> None:
        if self._ctx.position and self.held:
            await self._ctx.sell(self._ctx.position, reason, symbol=self.held)

    async def _open(self, kind: str, reason: str | None) -> None:
        spot = self._ctx.last_price
        contract = pick_contract(await self._ctx.option_contracts(), kind, spot, self._itm,
                                 datetime.now(IST).date()) if spot else None
        if contract is None:
            self._ctx.log(f"skipped ({reason}): no {kind} contract found near {spot}")
            return
        quantity = self._ctx.quantity // contract.lot * contract.lot  # whole lots
        if quantity == 0:
            self._ctx.log(f"skipped ({reason}): {self._ctx.quantity} units is less than one lot of {contract.lot}")
            return
        self.held = contract.symbol
        await self._ctx.buy(quantity, f"{reason}; {kind} {contract.strike:g} {contract.expiry:%d %b}",
                            symbol=contract.symbol)


def pick_contract(contracts: list[OptionContract], kind: str, spot: float, itm_points: float,
                  today) -> OptionContract | None:
    """The `kind` option `itm_points` in the money (strike below the index for a call, above it
    for a put), on the nearest expiry after today. The listed strike closest to that target;
    a tie goes to the deeper in-the-money one."""
    expiries = sorted({c.expiry for c in contracts if c.kind == kind and c.expiry > today})
    if not expiries:
        return None
    chain = [c for c in contracts if c.kind == kind and c.expiry == expiries[0]]
    target = spot - itm_points if kind == "CE" else spot + itm_points
    deeper = 1 if kind == "CE" else -1  # lower strikes are deeper for calls, higher for puts
    return min(chain, key=lambda c: (abs(c.strike - target), deeper * c.strike))


class IntradayBreakoutOptions(IntradayBreakout):
    name = "intraday_breakout_options"
    description = (
        "Intraday Breakout's signals on the index, traded by buying in-the-money options: a call "
        "on a long signal, a put on a short one, sold when the index trade exits."
    )
    tags = ("Intraday", "Breakout", "Options", "Long & short", "Trailing stop")
    backtest_note = ("Backtests replay the signals on the index and show index points × quantity, "
                     "not option premiums: past option prices aren't available.")
    capital_sizing = False  # sized in option lots, not from the index price

    class Params(IntradayBreakout.Params):
        itm_points: float = Field(300, ge=0, description="How far in the money (index points)")

    @classmethod
    def rules(cls, p: "IntradayBreakoutOptions.Params") -> list[dict]:
        rules = super().rules(p)
        itm = f"{p.itm_points:g} points in the money, nearest weekly expiry (the next one on expiry day)"
        for rule in rules:
            if rule["kind"] == "entry_long":
                rule["title"] = "Buy a call (CE)"
                rule["conditions"].append(f"Buys the CE {itm}")
            elif rule["kind"] == "entry_short":
                rule["title"] = "Buy a put (PE)"
                rule["conditions"].append(f"Buys the PE {itm}")
            elif rule["kind"] == "exit":
                rule["conditions"].append("Exits sell the option bought at entry; signals come from the index only")
        return rules

    async def on_start(self) -> None:
        await super().on_start()
        if hasattr(self.ctx, "option_contracts"):  # live / signal mode; backtests trade the index
            self.ctx = OptionLegs(self.ctx, self.params.itm_points)
