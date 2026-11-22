"""NIFTY percentage-reversal futures with a long 500-point OTM option hedge.

The configured current-month future remains the strategy's directional position. Before a
long future is opened, the strategy buys a current-month OTM put; before a short future is
opened, it buys a current-month OTM call. On reversal the new hedge is acquired first, the
future reverses second, and the obsolete hedge is sold last.
"""

import re
from datetime import datetime, time, timedelta

from pydantic import Field

from app.brokers.base import OptionContract
from app.orders.schemas import Product
from app.strategies.base import Tick, before_market_open
from app.strategies.builtin.nifty_trailing_reversal import IST, NiftyTrailingReversal
from app.strategies.builtin.nifty_trailing_reversal_optimized import NiftyTrailingReversalOptimized

HEDGE_RETRY = timedelta(seconds=5)
FUTURE_MONTH = re.compile(
    r"(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)FUT$"
)
MONTHS = {name: month for month, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
    start=1,
)}


class _HedgedFuturesExecution:
    primary_symbol_only = True
    reconcile_option_positions = True

    async def on_start(self) -> None:
        await super().on_start()
        self._hedge_retry_at: datetime | None = None
        self._deferred_stop_tick: Tick | None = None

    def _option_positions(self) -> dict[str, int]:
        return {
            symbol: quantity
            for symbol, quantity in self.ctx.positions.items()
            if quantity > 0 and symbol.endswith(("CE", "PE"))
        }

    @staticmethod
    def _hedge_kind(direction: int) -> str:
        return "PE" if direction > 0 else "CE"

    def _has_hedge(self, direction: int) -> bool:
        kind = self._hedge_kind(direction)
        hedged = sum(
            quantity for symbol, quantity in self._option_positions().items()
            if symbol.endswith(kind)
        )
        return hedged >= max(abs(self.ctx.position), self.ctx.quantity)

    async def _select_hedge(self, direction: int, now: datetime) -> OptionContract | None:
        if self._hedge_retry_at is not None and now < self._hedge_retry_at:
            return None
        try:
            contracts = await self.ctx.option_contracts()
        except Exception as error:
            self.ctx.log(f"hedge option chain unavailable ({error}); retrying")
            self._hedge_retry_at = now + HEDGE_RETRY
            return None

        kind = self._hedge_kind(direction)
        local_day = now.astimezone(IST).date()
        future_month = FUTURE_MONTH.search(self.ctx.symbol)
        expiry_year, expiry_month = (
            (2000 + int(future_month.group(1)), MONTHS[future_month.group(2)])
            if future_month
            else (local_day.year, local_day.month)
        )
        expiries = sorted({
            contract.expiry
            for contract in contracts
            if contract.kind == kind
            and (contract.expiry.year, contract.expiry.month) == (expiry_year, expiry_month)
        })
        if not expiries:
            self.ctx.log(f"no current-month {kind} is available for the futures hedge; retrying")
            self._hedge_retry_at = now + HEDGE_RETRY
            return None

        expiry = expiries[-1]
        spot = self.ctx.last_price
        if not spot:
            return None
        target = spot - self.params.hedge_points if kind == "PE" else spot + self.params.hedge_points
        eligible = [
            contract for contract in contracts
            if contract.kind == kind and contract.expiry == expiry
        ]
        if not eligible:
            self._hedge_retry_at = now + HEDGE_RETRY
            return None
        self._hedge_retry_at = None
        return min(eligible, key=lambda contract: (abs(contract.strike - target), contract.strike))

    async def _buy_hedge(self, direction: int, now: datetime) -> bool:
        if self._has_hedge(direction):
            return True
        contract = await self._select_hedge(direction, now)
        if contract is None:
            return False
        future_quantity = max(abs(self.ctx.position), self.ctx.quantity)
        already_hedged = sum(
            quantity for symbol, quantity in self._option_positions().items()
            if symbol.endswith(contract.kind)
        )
        shortfall = max(future_quantity - already_hedged, 0)
        quantity = ((shortfall + contract.lot - 1) // contract.lot) * contract.lot
        if quantity <= 0:
            self.ctx.log(
                f"cannot hedge {future_quantity} futures units with an option lot of {contract.lot}"
            )
            return False
        await self.ctx.buy(
            quantity,
            f"margin hedge for {'LONG' if direction > 0 else 'SHORT'} future; "
            f"buy {contract.kind} {contract.strike:g} ({self.params.hedge_points:g} points OTM) "
            f"expiring {contract.expiry:%d %b}",
            symbol=contract.symbol,
            product=Product.DELIVERY,
        )
        return self._has_hedge(direction)

    async def _remove_obsolete_hedge(self, direction: int) -> None:
        wanted_kind = self._hedge_kind(direction)
        obsolete = next(
            ((symbol, quantity) for symbol, quantity in self._option_positions().items()
             if not symbol.endswith(wanted_kind)),
            None,
        )
        if obsolete is None:
            return
        symbol, quantity = obsolete
        await self.ctx.sell(
            quantity,
            f"remove obsolete futures margin hedge after reversal to "
            f"{'LONG' if direction > 0 else 'SHORT'}",
            symbol=symbol,
            product=Product.DELIVERY,
        )

    def _desired_direction(self, tick: Tick) -> int:
        if self._deferred_stop_tick is not None:
            return -1 if self.ctx.position > 0 else 1
        if self.ctx.position > 0 and self.stop is not None and tick.price <= self.stop:
            self._deferred_stop_tick = tick
            return -1
        if self.ctx.position < 0 and self.stop is not None and tick.price >= self.stop:
            self._deferred_stop_tick = tick
            return 1
        if self.ctx.position:
            return 1 if self.ctx.position > 0 else -1
        return 1  # the base strategy's initial seed is always long

    async def on_tick(self, tick: Tick) -> None:
        if before_market_open(tick.at):
            await super().on_tick(tick)
            return
        if self.ctx.order_pending:
            return

        direction = self._desired_direction(tick)
        if not self._has_hedge(direction):
            if not await self._buy_hedge(direction, tick.at):
                return
            if self.ctx.order_pending:
                return

        trigger = self._deferred_stop_tick or tick
        self._deferred_stop_tick = None
        await super().on_tick(trigger)
        if self.ctx.order_pending:
            return

        actual_direction = 1 if self.ctx.position > 0 else -1 if self.ctx.position < 0 else 0
        if actual_direction:
            await self._remove_obsolete_hedge(actual_direction)
        if trigger is not tick and not self.ctx.order_pending:
            await super().on_tick(tick)

    async def on_clock(self, now: datetime) -> None:
        local_time = now.astimezone(IST).time()
        if not time(9, 15) <= local_time < time(15, 30):
            await super().on_clock(now)
            return
        # A real option limit may fill between futures ticks. Continue the latched stop on the
        # clock rather than mistakenly treating the newly bought opposite hedge as obsolete.
        if self._deferred_stop_tick is not None and not self.ctx.order_pending:
            await self.on_tick(self._deferred_stop_tick)
            return
        await super().on_clock(now)
        if self.ctx.order_pending or not self.ctx.position:
            return
        direction = 1 if self.ctx.position > 0 else -1
        if not self._has_hedge(direction):
            await self._buy_hedge(direction, now)
        if not self.ctx.order_pending:
            await self._remove_obsolete_hedge(direction)

    @classmethod
    def rules(cls, p) -> list[dict]:
        rules = super().rules(p)
        for rule in rules:
            if rule["kind"] == "filter":
                rule["conditions"].append(
                    "All trailing stops and reversals are managed in code; no broker-side "
                    "bracket, stop-loss, target, GTT or Smart order is placed"
                )
            elif rule["kind"] == "entry_long":
                rule["conditions"].append(
                    f"Before buying the future, buys a current-month PE about {p.hedge_points:g} points OTM"
                )
            elif rule["kind"] == "entry_short":
                rule["conditions"].append(
                    f"Before selling the future, buys a current-month CE about {p.hedge_points:g} points OTM"
                )
            elif rule["kind"] == "exit":
                rule["conditions"].append(
                    "On reversal, buys the new hedge first and removes the old hedge after the future reverses"
                )
        return rules


class NiftyTrailingReversalHedgedFutures(_HedgedFuturesExecution, NiftyTrailingReversal):
    name = "nifty_trailing_reversal_hedged_futures"
    description = (
        "Current-month NIFTY futures reversal with a bought 500-point OTM PE for long futures "
        "and a bought 500-point OTM CE for short futures to reduce margin."
    )
    tags = ("Positional", "NIFTY Futures", "500-point OTM hedge", "Trailing stop")
    backtest_note = (
        "Backtests report the current-month futures leg only. Live trading also buys the "
        "configured OTM option hedge; its premium, spread, and P&L are not included."
    )

    class Params(NiftyTrailingReversal.Params):
        hedge_points: float = Field(500, gt=0, description="OTM distance of the margin hedge")


class NiftyTrailingReversalOptimizedHedgedFutures(
    _HedgedFuturesExecution, NiftyTrailingReversalOptimized
):
    name = "nifty_trailing_reversal_optimized_hedged_futures"
    description = (
        "Optimized current-month NIFTY futures reversal with a bought 500-point OTM PE for "
        "long futures and a bought 500-point OTM CE for short futures to reduce margin."
    )
    tags = ("Positional", "NIFTY Futures", "500-point OTM hedge", "0.4% initial stop")
    backtest_note = NiftyTrailingReversalHedgedFutures.backtest_note

    class Params(NiftyTrailingReversalOptimized.Params):
        hedge_points: float = Field(500, gt=0, description="OTM distance of the margin hedge")
