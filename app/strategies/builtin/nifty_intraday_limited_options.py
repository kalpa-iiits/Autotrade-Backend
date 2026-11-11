"""NIFTY limited-trade signals executed with 500-point ITM CE/PE options.

The NIFTY index remains the signal and risk-price source. Long signals buy a call, short
signals buy a put, and every index exit sells the held option. Intraday entries may use the
current day's expiry; an overnight entry always selects an expiry after the entry day.
"""

from datetime import datetime, timedelta

from pydantic import Field

from app.orders.schemas import Product
from app.strategies.builtin.intraday_breakout_options import pick_contract
from app.strategies.builtin.nifty_intraday_limited import IST, NiftyIntradayLimited


class LimitedTradeOptionLegs:
    """Translate signed NIFTY trades into long CE/PE option positions."""

    def __init__(self, ctx, itm_points: float) -> None:
        self._ctx = ctx
        self._itm = itm_points
        self.held = ctx.held_symbol

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    @property
    def position(self) -> int:
        units = self._ctx.position
        return -units if units and self.held and self.held.endswith("PE") else units

    @property
    def last_fill_price(self) -> None:
        """The strategy's stops and targets remain NIFTY-index point levels."""
        return None

    async def buy(self, quantity: int | None = None, reason: str | None = None,
                  product: Product | None = None) -> None:
        if self.position < 0:
            await self.exit(reason, product)
        elif self.position == 0:
            await self._open("CE", reason, product)

    async def sell(self, quantity: int | None = None, reason: str | None = None,
                   product: Product | None = None) -> None:
        if self.position > 0:
            await self.exit(reason, product)
        elif self.position == 0:
            await self._open("PE", reason, product)

    async def exit(self, reason: str | None = None,
                   product: Product | None = None) -> None:
        if self._ctx.position and self.held:
            await self._ctx.sell(
                self._ctx.position, reason, symbol=self.held, product=product,
            )

    async def _open(self, kind: str, reason: str | None,
                    product: Product | None = None) -> None:
        spot = self._ctx.last_price
        if not spot:
            self._ctx.log(f"skipped ({reason}): no NIFTY price available")
            return
        today = datetime.now(IST).date()
        overnight = bool(reason and "overnight" in reason.lower())
        product = Product.DELIVERY if overnight else product or Product.INTRADAY
        first_expiry = today + timedelta(days=1) if overnight else today
        contract = pick_contract(
            await self._ctx.option_contracts(), kind, spot, self._itm, first_expiry,
        )
        if contract is None:
            holding = "overnight " if overnight else ""
            self._ctx.log(
                f"skipped ({reason}): no {holding}{kind} contract found near NIFTY {spot}"
            )
            return
        quantity = self._ctx.quantity // contract.lot * contract.lot
        if quantity == 0:
            self._ctx.log(
                f"skipped ({reason}): {self._ctx.quantity} units is less than one lot "
                f"of {contract.lot}"
            )
            return
        self.held = contract.symbol
        await self._ctx.buy(
            quantity,
            f"{reason}; {kind} {contract.strike:g} {contract.expiry:%d %b}; "
            f"{self._itm:g} points ITM",
            symbol=contract.symbol,
            product=product,
        )


class NiftyIntradayLimitedOptions(NiftyIntradayLimited):
    name = "nifty_intraday_limited_options"
    description = (
        "NIFTY limited-trade intraday and overnight signals executed by buying 500-point "
        "ITM calls for long signals and puts for short signals."
    )
    tags = ("Intraday + overnight", "NIFTY", "Options", "500-point ITM", "Long CE/PE")
    capital_sizing = False
    backtest_note = (
        "Backtests replay the NIFTY index signals and report index points x quantity, not "
        "historical option-premium P&L. Live entries buy ITM options and live exits sell them."
    )

    class Params(NiftyIntradayLimited.Params):
        itm_points: float = Field(500, ge=0, description="Option distance in the money, index points")

    @classmethod
    def rules(cls, p: "NiftyIntradayLimitedOptions.Params") -> list[dict]:
        rules = super().rules(p)
        for rule in rules:
            title = rule["title"].lower()
            if rule["kind"] == "entry_long":
                rule["title"] = ("Buy overnight call (CE)" if "overnight" in title
                                 else "Buy intraday call (CE)")
                rule["conditions"].append(f"Buys a CE approximately {p.itm_points:g} points ITM")
            elif rule["kind"] == "entry_short":
                rule["title"] = ("Buy overnight put (PE)" if "overnight" in title
                                 else "Buy intraday put (PE)")
                rule["conditions"].append(f"Buys a PE approximately {p.itm_points:g} points ITM")
            elif rule["kind"] == "exit":
                rule["conditions"].append("Sells the option held for that NIFTY signal")
        return rules

    async def on_start(self) -> None:
        if hasattr(self.ctx, "option_contracts"):
            self.ctx = LimitedTradeOptionLegs(self.ctx, self.params.itm_points)
        await super().on_start()
