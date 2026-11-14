"""NIFTY limited-trade signals executed with 700-point ITM CE/PE options.

The NIFTY index remains the signal and risk-price source. Long signals buy a call, short
signals buy a put, and every index exit sells the held option. Intraday entries may use the
current day's expiry; an overnight entry always selects an expiry after the entry day.
"""

from datetime import datetime, timedelta

from pydantic import Field

from app.brokers import OrderError
from app.brokers.base import OptionContract, OptionGreeks
from app.orders.schemas import Product
from app.strategies.builtin.intraday_breakout_options import pick_contract
from app.strategies.builtin.nifty_intraday_limited import IST, NiftyIntradayLimited


class LimitedTradeOptionLegs:
    """Translate signed NIFTY trades into long CE/PE option positions."""

    def __init__(self, ctx, itm_points: float) -> None:
        self._ctx = ctx
        self._itm = itm_points
        self.held = ctx.held_symbol

    target_abs_delta = 0.95

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
        # Contract discovery and delta selection both make broker requests. Do not perform
        # either after the runner has exhausted this instance's daily entry-order budget.
        # Exits use exit() above and remain available regardless of the cap.
        if not getattr(self._ctx, "entry_orders_available", True):
            return
        spot = self._ctx.last_price
        if not spot:
            self._ctx.log(f"skipped ({reason}): no NIFTY price available")
            return
        today = datetime.now(IST).date()
        overnight = bool(reason and "overnight" in reason.lower())
        product = Product.DELIVERY if overnight else product or Product.INTRADAY
        first_expiry = today + timedelta(days=1) if overnight else today
        contracts = await self._ctx.option_contracts()
        contract = pick_contract(contracts, kind, spot, self._itm, first_expiry)
        if contract is None:
            holding = "overnight " if overnight else ""
            self._ctx.log(
                f"skipped ({reason}): no {holding}{kind} contract found near NIFTY {spot}"
            )
            return
        greek: OptionGreeks | None = None
        if hasattr(self._ctx, "option_greeks"):
            try:
                rows = await self._ctx.option_greeks(contract.expiry)
                greek, selected = pick_contract_by_delta(
                    contracts, rows, kind, contract.expiry, self.target_abs_delta,
                )
                if selected is not None:
                    contract = selected
            except OrderError as error:
                self._ctx.log(f"delta selection unavailable ({error}); using {self._itm:g}-point ITM fallback")
        quantity = self._ctx.quantity // contract.lot * contract.lot
        if quantity == 0:
            self._ctx.log(
                f"skipped ({reason}): {self._ctx.quantity} units is less than one lot "
                f"of {contract.lot}"
            )
            return
        self.held = contract.symbol
        if greek is not None:
            values = [f"delta {greek.delta:+.3f}"]
            if greek.iv is not None:
                values.append(f"IV {greek.iv:.2f}%")
            if greek.theta is not None:
                values.append(f"theta {greek.theta:.2f}/day")
            selection = f"{', '.join(values)}; target |delta| {self.target_abs_delta:.2f}"
        else:
            selection = f"{self._itm:g} points ITM fallback"
        await self._ctx.buy(
            quantity,
            f"{reason}; {kind} {contract.strike:g} {contract.expiry:%d %b}; {selection}",
            symbol=contract.symbol,
            product=product,
        )


def pick_contract_by_delta(
    contracts: list[OptionContract], rows: list[OptionGreeks], kind: str, expiry,
    target_abs_delta: float,
) -> tuple[OptionGreeks | None, OptionContract | None]:
    """Pick the liquid listed contract whose absolute live delta is closest to the target."""
    by_symbol = {contract.symbol: contract for contract in contracts
                 if contract.kind == kind and contract.expiry == expiry}
    candidates = [row for row in rows
                  if row.kind == kind and row.expiry == expiry and row.symbol in by_symbol
                  and 0 < abs(row.delta) <= 1 and (row.ask is None or row.ask > 0)]
    if not candidates:
        return None, None

    def key(row: OptionGreeks):
        spread = ((row.ask - row.bid) / row.ask
                  if row.ask and row.bid is not None and row.bid > 0 else float("inf"))
        return abs(abs(row.delta) - target_abs_delta), spread

    greek = min(candidates, key=key)
    return greek, by_symbol[greek.symbol]


class NiftyIntradayLimitedOptions(NiftyIntradayLimited):
    name = "nifty_intraday_limited_options"
    description = (
        "NIFTY limited-trade intraday and overnight signals executed with calls and puts "
        "selected near 0.95 absolute delta, with a 700-point ITM fallback."
    )
    tags = ("Intraday + overnight", "NIFTY", "Options", "0.95 delta", "Long CE/PE")
    capital_sizing = False
    backtest_note = (
        "Backtests replay the NIFTY index signals and report index points x quantity, not "
        "historical option-premium P&L. Live entries buy ITM options and live exits sell them."
    )

    class Params(NiftyIntradayLimited.Params):
        itm_points: float = Field(700, ge=0, description="Option distance in the money, index points")

    @classmethod
    def rules(cls, p: "NiftyIntradayLimitedOptions.Params") -> list[dict]:
        rules = super().rules(p)
        for rule in rules:
            title = rule["title"].lower()
            if rule["kind"] == "entry_long":
                rule["title"] = ("Buy overnight call (CE)" if "overnight" in title
                                 else "Buy intraday call (CE)")
                rule["conditions"].append(
                    f"Targets +{LimitedTradeOptionLegs.target_abs_delta:.2f} delta; "
                    f"falls back to approximately {p.itm_points:g} points ITM"
                )
            elif rule["kind"] == "entry_short":
                rule["title"] = ("Buy overnight put (PE)" if "overnight" in title
                                 else "Buy intraday put (PE)")
                rule["conditions"].append(
                    f"Targets -{LimitedTradeOptionLegs.target_abs_delta:.2f} delta; "
                    f"falls back to approximately {p.itm_points:g} points ITM"
                )
            elif rule["kind"] == "exit":
                rule["conditions"].append("Sells the option held for that NIFTY signal")
        return rules

    async def on_start(self) -> None:
        if hasattr(self.ctx, "option_contracts"):
            self.ctx = LimitedTradeOptionLegs(self.ctx, self.params.itm_points)
        await super().on_start()
