"""Optimized NIFTY trailing-reversal signals executed with a short weekly ITM option.

The index is the only signal/stop source. Option prices only select, classify, reset and roll
the execution leg described by the published strategy rules.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import sqrt

from pydantic import Field

from app.brokers.base import OptionContract
from app.orders.schemas import Product
from app.strategies.base import Tick
from app.strategies.builtin.intraday_breakout_options import pick_contract
from app.strategies.builtin.nifty_trailing_reversal_optimized import IST, NiftyTrailingReversalOptimized

EXPIRY_TIME = time(15, 30)
EXPIRY_EXIT_TIME = time(15, 10)
HEDGE_POINTS = 1_000.0
SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60


@dataclass(frozen=True)
class QuoteSnapshot:
    price: float
    observed_at: datetime
    source: str = "live"


@dataclass(frozen=True)
class FrozenReset:
    dte: int
    intrinsic_value: float
    time_value: float
    iv_proxy: float
    iv_regime: str
    reset_level: float
    classified_at: datetime
    expiry: date

    def as_dict(self) -> dict:
        return {
            "dte": self.dte, "intrinsic_value": self.intrinsic_value,
            "time_value": self.time_value, "iv_proxy": self.iv_proxy,
            "iv_regime": self.iv_regime, "reset_level": self.reset_level,
            "classified_at": self.classified_at.isoformat(), "expiry": self.expiry.isoformat(),
        }

    @classmethod
    def restore(cls, value) -> "FrozenReset | None":
        if not isinstance(value, dict):
            return None
        try:
            result = cls(
                int(value["dte"]), float(value["intrinsic_value"]), float(value["time_value"]),
                float(value["iv_proxy"]), str(value["iv_regime"]), float(value["reset_level"]),
                datetime.fromisoformat(value["classified_at"]), date.fromisoformat(value["expiry"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return result if result.iv_regime in {"HIGH", "LOW"} else None


def classify_reset(kind: str, strike: float, spot: float, option_price: float,
                   expiry: date, now: datetime) -> FrozenReset:
    """Calculate the rule's DTE/IV classification at entry or an expiry roll."""
    local = now.astimezone(IST)
    dte = (expiry - local.date()).days
    if not 0 <= dte <= 7:
        raise ValueError(f"current-week option has unsupported DTE {dte}")
    expiry_at = datetime.combine(expiry, EXPIRY_TIME, tzinfo=IST)
    remaining = max((expiry_at - local).total_seconds(), 1.0) / SECONDS_PER_YEAR
    intrinsic = max(strike - spot, 0.0) if kind == "PE" else max(spot - strike, 0.0)
    time_value = max(option_price - intrinsic, 0.0)
    iv_proxy = time_value / (spot * sqrt(remaining))
    threshold = 0.013692 if dte <= 1 else 0.010253 if dte <= 3 else 0.009954
    regime = "HIGH" if iv_proxy >= threshold else "LOW"
    if dte <= 1:
        reset = 850.0
    elif dte <= 3:
        reset = 700.0 if regime == "HIGH" else 900.0
    else:
        reset = 900.0 if regime == "HIGH" else 850.0
    return FrozenReset(dte, intrinsic, time_value, iv_proxy, regime, reset, local, expiry)


class ShortWeeklyOptionLegs:
    """Map spot direction to one weekly option short plus its bought OTM margin hedge."""
    state_key = "weekly_option_leg"
    order_product = Product.DELIVERY
    same_session_expiry_roll = False
    require_fresh_hedge_quote = False

    def __init__(self, ctx, itm_points: float, hedge_points: float = HEDGE_POINTS) -> None:
        self._ctx = ctx
        self.itm_points = itm_points
        self.hedge_points = hedge_points
        self.desired_direction = self._actual_direction()
        self.replace_symbol: str | None = None
        self.pending_contract: OptionContract | None = None
        self.pending_hedge_contract: OptionContract | None = None
        self.pending_reason: str | None = None
        self.hedge_symbol: str | None = None
        self.frozen: FrozenReset | None = None
        self.last_premium: float | None = None
        self.reset_armed = False
        self.reset_pending = False
        self.roll_after: date | None = None
        self.roll_expiry_after: date | None = None
        self.pending_audit: dict | None = None
        self.contracts_by_symbol: dict[str, OptionContract] = {}
        self._restore()

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    @property
    def strategy_state(self) -> dict:
        return self._ctx.strategy_state

    async def save_strategy_state(self, state: dict) -> None:
        merged = dict(state)
        merged[self.state_key] = self._leg_state()
        await self._ctx.save_strategy_state(merged)

    @property
    def position(self) -> int:
        actual = self._actual_direction()
        if actual:
            return actual * self.option_quantity
        # While waiting to complete an expiry roll, keep the spot signal virtually in its
        # continuing direction so its stop can react before the replacement option is tradable.
        return (self.desired_direction * self.option_quantity
                if self.pending_contract or self.roll_after else 0)

    @property
    def option_quantity(self) -> int:
        """Configured units per leg; the short and its margin hedge always match."""
        return self._ctx.quantity

    @property
    def order_pending(self) -> bool:
        """Keep a breached spot reversal latched while its option quote is unavailable.

        The underlying runner lock covers real broker orders.  A desired/actual direction
        mismatch is also a pending transition from the signal engine's perspective; exposing
        it here preserves the first tradable gap tick as the reversal reference until the
        cover-and-replace sequence completes.
        """
        return self._ctx.order_pending or self.desired_direction != self._actual_direction()

    @property
    def last_fill_price(self) -> None:
        return None  # stops remain NIFTY levels, never option-premium levels

    def _short_options(self) -> dict[str, int]:
        return {s: q for s, q in self._ctx.positions.items()
                if q < 0 and s.endswith(("CE", "PE"))}

    def _long_options(self) -> dict[str, int]:
        return {s: q for s, q in self._ctx.positions.items()
                if q > 0 and s.endswith(("CE", "PE"))}

    def _actual_direction(self) -> int:
        positions = self._short_options()
        if any(s.endswith("PE") for s in positions):
            return 1
        if any(s.endswith("CE") for s in positions):
            return -1
        return 0

    @staticmethod
    def _kind(direction: int) -> str:
        return "PE" if direction > 0 else "CE"

    def _restore(self) -> None:
        state = getattr(self._ctx, "strategy_state", {}).get(self.state_key, {})
        if not isinstance(state, dict):
            return
        self.frozen = FrozenReset.restore(state.get("frozen"))
        value = state.get("last_premium")
        self.last_premium = float(value) if isinstance(value, (int, float)) else None
        self.reset_armed = bool(state.get("reset_armed"))
        self.reset_pending = bool(state.get("reset_pending"))
        try:
            self.roll_after = date.fromisoformat(state["roll_after"]) if state.get("roll_after") else None
        except (TypeError, ValueError):
            self.roll_after = None
        try:
            self.roll_expiry_after = (date.fromisoformat(state["roll_expiry_after"])
                                      if state.get("roll_expiry_after") else None)
        except (TypeError, ValueError):
            self.roll_expiry_after = None
        self.pending_audit = state.get("pending_audit") if isinstance(state.get("pending_audit"), dict) else None
        value = state.get("hedge_symbol")
        self.hedge_symbol = value if isinstance(value, str) else None

    def _leg_state(self) -> dict:
        return {
            "frozen": self.frozen.as_dict() if self.frozen else None,
            "last_premium": self.last_premium,
            "reset_armed": self.reset_armed,
            "reset_pending": self.reset_pending,
            "roll_after": self.roll_after.isoformat() if self.roll_after else None,
            "roll_expiry_after": (self.roll_expiry_after.isoformat()
                                  if self.roll_expiry_after else None),
            "pending_audit": self.pending_audit,
            "hedge_symbol": self.hedge_symbol,
        }

    async def _save_leg_state(self) -> None:
        state = dict(getattr(self._ctx, "strategy_state", {}))
        state[self.state_key] = self._leg_state()
        await self._ctx.save_strategy_state(state)

    def set_desired(self, direction: int) -> None:
        if direction not in (-1, 0, 1):
            raise ValueError(f"invalid option direction {direction}")
        if direction != self.desired_direction:
            pending_roll = self.roll_after
            pending_roll_expiry = self.roll_expiry_after
            self.replace_symbol = self.pending_contract = self.pending_hedge_contract = None
            self.pending_reason = None
            self.frozen = self.last_premium = self.roll_after = self.roll_expiry_after = None
            self.reset_armed = self.reset_pending = False
            # A spot reversal after the expiry close changes the option type while preserving the
            # pending roll's execution date and minimum eligible weekly expiry.
            self.roll_after = pending_roll
            self.roll_expiry_after = pending_roll_expiry
        self.desired_direction = direction

    async def buy(self, quantity: int | None = None, reason: str | None = None,
                  product: Product | None = None) -> None:
        self.set_desired(1)
        await self.advance(datetime.now(IST), reason or "reversal")

    async def sell(self, quantity: int | None = None, reason: str | None = None,
                   product: Product | None = None) -> None:
        self.set_desired(-1)
        await self.advance(datetime.now(IST), reason or "reversal")

    async def _buy_overnight(self, quantity: int, reason: str, symbol: str) -> None:
        """Place a carry-forward option order, independent of the account default."""
        await self._ctx.buy(
            quantity, reason, symbol=symbol, product=self.order_product,
        )

    async def _sell_overnight(self, quantity: int, reason: str, symbol: str) -> None:
        """Place a carry-forward option order, independent of the account default."""
        await self._ctx.sell(
            quantity, reason, symbol=symbol, product=self.order_product,
        )

    async def _quote(self, symbol: str, side: str, now: datetime) -> QuoteSnapshot | None:
        method = getattr(self._ctx, "executable_quote", None)
        if method is not None:
            raw = await method(symbol, side)
        else:
            price = await self._ctx.quote(symbol)
            raw = QuoteSnapshot(price, now, "test-ltp") if price and price > 0 else None
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            raw = QuoteSnapshot(float(raw), now, "ltp")
        if raw.price <= 0 or raw.observed_at.tzinfo is None:
            return None
        age = abs((now.astimezone(IST) - raw.observed_at.astimezone(IST)).total_seconds())
        return raw if age <= 15 else None

    async def _contracts(self) -> list[OptionContract]:
        contracts = await self._ctx.option_contracts()
        self.contracts_by_symbol.update((c.symbol, c) for c in contracts)
        return contracts

    async def _select(self, kind: str, now: datetime,
                      earliest_expiry: date | None = None,
                      exact_expiry: date | None = None) -> OptionContract | None:
        spot = self._ctx.last_price
        if not spot:
            return None
        contracts = await self._contracts()
        if exact_expiry is not None:
            contracts = [contract for contract in contracts if contract.expiry == exact_expiry]
        start = earliest_expiry or now.astimezone(IST).date()
        nearest = pick_contract(contracts, kind, spot, self.itm_points, start)
        if nearest is None:
            return None
        # Preserve expiry priority, then prefer the more liquid 100-point strike series. Use a
        # non-100 strike only when that selected expiry has no listed 100-point alternative.
        hundreds = [
            contract for contract in contracts
            if contract.kind == kind and contract.expiry == nearest.expiry
            and abs(contract.strike / 100 - round(contract.strike / 100)) < 1e-9
        ]
        return pick_contract(hundreds, kind, spot, self.itm_points, start) if hundreds else nearest

    async def _select_hedge(self, kind: str, expiry: date) -> OptionContract | None:
        spot = self._ctx.last_price
        if not spot:
            return None
        contracts = await self._contracts()
        target = spot - self.hedge_points if kind == "PE" else spot + self.hedge_points
        eligible = [
            contract for contract in contracts
            if contract.kind == kind and contract.expiry == expiry
            and (contract.strike < spot if kind == "PE" else contract.strike > spot)
        ]
        if not eligible:
            return None
        hundreds = [
            contract for contract in eligible
            if abs(contract.strike / 100 - round(contract.strike / 100)) < 1e-9
        ]
        choices = hundreds or eligible
        return min(choices, key=lambda contract: (abs(contract.strike - target), contract.strike))

    async def _ensure_hedge(self, kind: str, expiry: date,
                            required_quantity: int | None = None,
                            now: datetime | None = None) -> bool:
        """Make the active margin hedge exactly match the exposure it protects.

        Existing short exposure is passed explicitly while a configured size reduction is in
        progress. This keeps the larger hedge until the short is covered, then trims the hedge
        on the next pass instead of briefly leaving part of the short unhedged.
        """
        target_quantity = self.option_quantity if required_quantity is None else required_quantity
        longs = self._long_options()
        contract = self.pending_hedge_contract
        if contract is None:
            if self.hedge_symbol and longs.get(self.hedge_symbol, 0) > 0:
                held_contract = self.contracts_by_symbol.get(self.hedge_symbol)
                if held_contract is None:
                    await self._contracts()
                    held_contract = self.contracts_by_symbol.get(self.hedge_symbol)
                if (held_contract is not None and held_contract.kind == kind
                        and held_contract.expiry == expiry):
                    contract = held_contract
            if contract is None:
                contract = await self._select_hedge(kind, expiry)
        if contract is None:
            self._ctx.log(
                f"entry deferred: no {kind} {self.hedge_points:g}-point OTM margin hedge"
            )
            return False
        held = longs.get(contract.symbol, 0)
        self.hedge_symbol = contract.symbol
        await self._save_leg_state()
        if held < target_quantity:
            if self.require_fresh_hedge_quote and (
                now is None or await self._quote(contract.symbol, "BUY", now) is None
            ):
                self._ctx.log("entry deferred: current hedge ask unavailable or stale")
                return False
            await self._buy_overnight(
                target_quantity - held,
                f"margin hedge; buy {kind} {contract.strike:g} "
                f"({self.hedge_points:g} points OTM) expiring {contract.expiry:%d %b}",
                contract.symbol,
            )
        elif held > target_quantity:
            await self._sell_overnight(
                held - target_quantity,
                f"trim margin hedge to {target_quantity} units", contract.symbol,
            )
        return self._long_options().get(contract.symbol, 0) == target_quantity

    async def _remove_obsolete_hedge(self) -> None:
        obsolete = next(
            ((symbol, quantity) for symbol, quantity in self._long_options().items()
             if symbol != self.hedge_symbol),
            None,
        )
        if obsolete is None:
            return
        symbol, quantity = obsolete
        await self._sell_overnight(
            quantity, "remove obsolete 1000-point OTM margin hedge",
            symbol,
        )

    def _audit_record(self, now: datetime, contract: OptionContract, quote: QuoteSnapshot,
                      frozen: FrozenReset, reason: str) -> dict:
        record = {
            "event": "nifty_weekly_option", "timestamp": now.astimezone(IST).isoformat(),
            "spot_price": self._ctx.last_price,
            "direction": "LONG" if self.desired_direction > 0 else "SHORT",
            "option_type": contract.kind, "strike": contract.strike,
            "expiry": contract.expiry.isoformat(), "DTE": frozen.dte,
            "option_premium": quote.price, "intrinsic_value": frozen.intrinsic_value,
            "time_value": frozen.time_value, "iv_proxy": frozen.iv_proxy,
            "IV_regime": frozen.iv_regime, "frozen_reset_level": frozen.reset_level,
            "quantity": self.option_quantity, "order_fill_price": None,
            "quote_source": quote.source, "reason": reason,
            "hedge_symbol": self.hedge_symbol, "hedge_distance": self.hedge_points,
        }
        return record

    async def _complete_fill_audit(self) -> None:
        """Attach the broker's durable fill price, including after a process restart."""
        if not self.pending_audit:
            return
        latest = getattr(self._ctx, "latest_option_fill", None)
        if latest is None:
            return
        filled = await latest(
            self.pending_audit["symbol"],
            datetime.fromisoformat(self.pending_audit["submitted_at"]),
        )
        if filled is None:
            return
        record = dict(self.pending_audit["record"])
        record["order_fill_price"] = float(filled)
        record["stage"] = "filled"
        self._ctx.log(json.dumps(record, separators=(",", ":"), sort_keys=True))
        self.pending_audit = None
        await self._save_leg_state()

    async def _prepare(self, kind: str, now: datetime, reason: str,
                       earliest_expiry: date | None = None,
                       exact_expiry: date | None = None) -> bool:
        contract = await self._select(kind, now, earliest_expiry, exact_expiry)
        if contract is None:
            self._ctx.log(f"{reason} deferred: no fresh current-week {kind} contract")
            return False
        if await self._quote(contract.symbol, "SELL", now) is None:
            self._ctx.log(f"{reason} deferred: replacement quote unavailable or stale")
            return False
        hedge = await self._select_hedge(kind, contract.expiry)
        if hedge is None:
            self._ctx.log(
                f"{reason} deferred: {self.hedge_points:g}-point OTM {kind} hedge unavailable"
            )
            return False
        self.pending_contract, self.pending_reason = contract, reason
        self.pending_hedge_contract = hedge
        return True

    async def request_reset(self, now: datetime) -> None:
        kind = self._kind(self.desired_direction)
        current = next((s for s in self._short_options() if s.endswith(kind)), None)
        if (current is None or self.frozen is None
                or not await self._prepare(
                    kind, now, "reset", exact_expiry=self.frozen.expiry,
                )):
            return
        self.replace_symbol = current
        await self._save_leg_state()
        await self.advance(now, "premium reset")

    async def monitor_reset(self, now: datetime) -> None:
        if self._ctx.order_pending or not self.desired_direction or self.replace_symbol:
            return
        kind = self._kind(self.desired_direction)
        current = next((s for s in self._short_options() if s.endswith(kind)), None)
        if current is None or self.frozen is None:
            return
        # Once a crossing is observed it is durable. A missing replacement quote must delay,
        # not consume, the reset; retry it without requiring premium to cross a second time.
        if self.reset_pending:
            await self.request_reset(now)
            return
        quote = await self._quote(current, "BUY", now)
        if quote is None:
            return
        self.last_premium = quote.price
        if quote.price > self.frozen.reset_level and not self.reset_armed:
            self.reset_armed = True
            await self._save_leg_state()
        elif self.reset_armed and quote.price <= self.frozen.reset_level:
            self.reset_armed = False
            self.reset_pending = True
            await self._save_leg_state()
            await self.request_reset(now)

    async def monitor_expiry(self, now: datetime) -> None:
        # Expiry safety lives in ``advance`` so it also runs on the first tick after a restart,
        # before the periodic clock callback. Keep this method as the explicit clock hook.
        await self.advance(now, "weekly expiry exit")

    async def _expiry_due(self, symbol: str, now: datetime) -> date | None:
        contract = self.contracts_by_symbol.get(symbol)
        if contract is None:
            await self._contracts()
            contract = self.contracts_by_symbol.get(symbol)
        expiry = contract.expiry if contract else (self.frozen.expiry if self.frozen else None)
        if expiry is None:
            return None
        local = now.astimezone(IST)
        if expiry < local.date() or (expiry == local.date() and local.time() >= EXPIRY_EXIT_TIME):
            return expiry
        return None

    async def _cover_duplicate_short(self, positions: dict[str, int], wanted: str) -> bool:
        """Reduce anomalous multi-contract short exposure one contract at a time."""
        if len(positions) <= 1:
            return False
        await self._contracts()
        spot = self._ctx.last_price or 0.0
        target = spot + self.itm_points if wanted == "PE" else spot - self.itm_points

        def keep_rank(item: tuple[str, int]) -> tuple:
            symbol, quantity = item
            contract = self.contracts_by_symbol.get(symbol)
            correct_kind = symbol.endswith(wanted)
            frozen_expiry = bool(
                contract and self.frozen and contract.expiry == self.frozen.expiry
            )
            strike_distance = abs(contract.strike - target) if contract else float("inf")
            deeper_tie = (-contract.strike if wanted == "PE" else contract.strike) if contract else 0
            return (
                not correct_kind, not frozen_expiry,
                abs(abs(quantity) - self.option_quantity), strike_distance, deeper_tie, symbol,
            )

        keep_symbol = min(positions.items(), key=keep_rank)[0]
        duplicate = next(
            (item for item in sorted(positions.items()) if item[0] != keep_symbol), None,
        )
        if duplicate is None:
            return False
        symbol, quantity = duplicate
        self._ctx.log(
            f"duplicate short option exposure detected; retaining {keep_symbol} and covering {symbol}"
        )
        await self._buy_overnight(
            abs(quantity), "reconcile duplicate short option exposure", symbol,
        )
        return True

    async def advance(self, now: datetime, reason: str = "option transition") -> None:
        await self._complete_fill_audit()
        if self._ctx.order_pending:
            return
        positions = self._short_options()
        # Closing an expiring (or already expired after restart) short is unconditional. Do not
        # make that risk-reducing order depend on a next-week quote or hedge being available.
        for symbol, quantity in sorted(positions.items()):
            expiry = await self._expiry_due(symbol, now)
            if expiry is None:
                continue
            self.roll_expiry_after = expiry + timedelta(days=1)
            self.roll_after = (now.astimezone(IST).date() if self.same_session_expiry_roll
                               else self.roll_expiry_after)
            self.replace_symbol = self.pending_contract = self.pending_hedge_contract = None
            self.pending_reason = None
            self.last_premium = None
            self.reset_armed = self.reset_pending = False
            # The old hedge becomes obsolete after the short is covered. Keep ``frozen`` until
            # the short disappears so its expiry remains a restart fallback if the contract is
            # no longer returned by the broker's option chain.
            self.hedge_symbol = None
            await self._save_leg_state()
            await self._buy_overnight(
                abs(quantity), f"weekly expiry exit {expiry:%d %b} at 15:10", symbol,
            )
            return
        if not self.desired_direction:
            return
        wanted = self._kind(self.desired_direction)
        if await self._cover_duplicate_short(positions, wanted):
            return
        if self.replace_symbol and self.replace_symbol in positions:
            await self._buy_overnight(abs(positions[self.replace_symbol]), reason,
                                      self.replace_symbol)
            return
        if self.replace_symbol and self.replace_symbol not in positions:
            self.replace_symbol = None
            # Never carry a prepared strike through an overnight expiry wait. The next
            # executable session must select 900-ITM again from that session's current spot.
            if self.roll_after is not None:
                self.pending_contract = self.pending_hedge_contract = self.pending_reason = None
            positions = self._short_options()
        obsolete = next((s for s in positions if not s.endswith(wanted)), None)
        if obsolete is not None:
            if self.pending_contract is None:
                held_contract = self.contracts_by_symbol.get(obsolete)
                if held_contract is None:
                    await self._contracts()
                    held_contract = self.contracts_by_symbol.get(obsolete)
                # A reversal stays on the held weekly expiry. This matters after a same-session
                # 15:10 roll, while the just-expired contract can still appear in the chain.
                held_expiry = held_contract.expiry if held_contract is not None else None
                if not await self._prepare(
                    wanted, now, "reversal", exact_expiry=held_expiry,
                ):
                    return
            await self._buy_overnight(
                abs(positions[obsolete]), f"{reason}; cover obsolete {obsolete[-2:]}", obsolete,
            )
            return
        held = next(((s, q) for s, q in positions.items() if s.endswith(wanted)), None)
        if held is not None:
            symbol, quantity = held
            contract = self.contracts_by_symbol.get(symbol)
            if contract is None:
                await self._contracts()
                contract = self.contracts_by_symbol.get(symbol)
            if (contract is None
                    or not await self._ensure_hedge(
                        wanted, contract.expiry, required_quantity=abs(quantity), now=now,
                    )):
                return
            if self._ctx.order_pending:
                return
            if abs(quantity) > self.option_quantity:
                await self._buy_overnight(
                    abs(quantity) - self.option_quantity,
                    "reconcile option quantity above configured size", symbol,
                )
            elif abs(quantity) < self.option_quantity:
                quote = await self._quote(symbol, "SELL", now)
                if quote is not None:
                    await self._sell_overnight(
                        self.option_quantity - abs(quantity),
                        "reconcile partial option entry to configured size", symbol,
                    )
            elif not self._ctx.order_pending and self.pending_reason != "expiry":
                await self._remove_obsolete_hedge()
            return
        if self.roll_after and self.frozen is not None:
            # The expiry cover has filled. The old classification was retained only as a
            # restart fallback while that short still existed; the next weekly entry must be
            # classified afresh.
            self.frozen = None
            await self._save_leg_state()
        if self.roll_after and now.astimezone(IST).date() < self.roll_after:
            await self._remove_obsolete_hedge()
            return

        contract = self.pending_contract or await self._select(
            wanted, now,
            earliest_expiry=self.roll_expiry_after if self.roll_after else None,
        )
        if contract is None:
            self._ctx.log(f"entry deferred: no current-week {wanted} near {self.itm_points:g} ITM")
            return
        quote = await self._quote(contract.symbol, "SELL", now)
        if quote is None:
            self._ctx.log("entry deferred: executable option quote unavailable or stale")
            return
        try:
            calculated = classify_reset(contract.kind, contract.strike, self._ctx.last_price,
                                        quote.price, contract.expiry, now)
        except ValueError as exc:
            self._ctx.log(f"entry deferred: {exc}")
            return
        if not await self._ensure_hedge(wanted, contract.expiry, now=now):
            return
        if self._ctx.order_pending:
            return
        event = self.pending_reason or ("reset" if self.reset_pending else reason)
        if self.frozen is None or event in {"reversal", "expiry"} or "reverse" in event:
            self.frozen = calculated
        self.last_premium, self.roll_after, self.roll_expiry_after = quote.price, None, None
        self.reset_armed = quote.price > self.frozen.reset_level
        self.reset_pending = False
        self.pending_contract = self.pending_hedge_contract = self.pending_reason = None
        record = self._audit_record(now, contract, quote, self.frozen, event)
        submitted = dict(record)
        submitted["stage"] = "submitted"
        self._ctx.log(json.dumps(submitted, separators=(",", ":"), sort_keys=True))
        self.pending_audit = {
            "symbol": contract.symbol,
            "submitted_at": now.astimezone(IST).isoformat(),
            "record": record,
        }
        await self._save_leg_state()
        await self._sell_overnight(
            self.option_quantity,
            f"{event}; short {wanted} {contract.strike:g} {contract.expiry:%d %b}; "
            f"reset ₹{self.frozen.reset_level:g}; {self.option_quantity} units",
            contract.symbol,
        )


class NiftyTrailingReversalOptimizedITMOptionSeller(NiftyTrailingReversalOptimized):
    name = "nifty_adaptive_premium_reset_seller"
    legacy_checkpoint_names = ("nifty_trailing_reversal_optimized_itm_option_seller",)
    description = ("NIFTY Adaptive Premium Reset Seller: unchanged optimized NIFTY signals "
                   "executed by selling the configured quantity of a current-week 900-ITM option "
                   "with frozen DTE/IV premium resets and a bought 1000-OTM margin hedge.")
    tags = ("Positional", "NIFTY", "Weekly options", "900 ITM", "1000 OTM hedge")
    capital_sizing = False
    primary_symbol_only = True
    reconcile_option_positions = True
    # The signal and reset evidence is specific to completed five-minute NIFTY bars. The same
    # capability list controls live instance creation, so unsupported tester intervals must not
    # be exposed here.
    timeframes = ("5m",)
    default_timeframe = "5m"
    backtest_note = ("The tester replays the unchanged NIFTY signal engine. Live option "
                     "execution uses current-week quotes and the frozen DTE/IV reset table.")
    option_legs_class = ShortWeeklyOptionLegs

    class Params(NiftyTrailingReversalOptimized.Params):
        itm_points: float = Field(900, gt=0, description="Weekly option distance in the money")

    @classmethod
    def rules(cls, p: "NiftyTrailingReversalOptimizedITMOptionSeller.Params") -> list[dict]:
        rules = super().rules(p)
        for rule in rules:
            if rule["kind"] == "filter":
                rule["conditions"] += ["Signals/stops use NIFTY index prices only",
                                       "The configured quantity of one nearest-expiry option is short",
                                       "Buy the same quantity of the same-expiry option 1000 points OTM as a margin hedge",
                                       "Prefer strikes divisible by 100; fall back only if unavailable"]
            elif rule["kind"] == "entry_long":
                rule["title"] = "LONG signal: short 900-ITM put"
            elif rule["kind"] == "entry_short":
                rule["title"] = "SHORT signal: short 900-ITM call"
            elif rule["kind"] == "risk":
                rule["conditions"] += [
                    "Freeze DTE, IV regime and reset level until reversal or expiry roll",
                    "Reset: 0–1 DTE ₹850; 2–3 LOW ₹900/HIGH ₹700; 4–6 LOW ₹850/HIGH ₹900",
                    "Reset only on a downward crossing with a fresh executable replacement quote",
                ]
            elif rule["kind"] == "exit":
                rule["conditions"] += ["Buy the OTM hedge before its protected short option",
                                       "Cover before replacement; never retain both short directions",
                                       "Start expiry rollover at 15:10 IST and reopen next executable session"]
        return rules

    async def on_start(self) -> None:
        if hasattr(self.ctx, "option_contracts"):
            self.ctx = self.option_legs_class(self.ctx, self.params.itm_points)
        await super().on_start()
        if isinstance(self.ctx, ShortWeeklyOptionLegs) and self._direction:
            self.ctx.set_desired(self._direction)
            # A rolled position waits for the first live spot tick, so an opening gap can update
            # the continuing signal before the new weekly strike/type is selected.
            if self.ctx.roll_after is None or self.ctx._short_options():
                await self.ctx.advance(datetime.now(IST), "restore optimized option direction")

    async def on_tick(self, tick: Tick) -> None:
        waiting_roll = (isinstance(self.ctx, ShortWeeklyOptionLegs)
                        and self.ctx.roll_after is not None
                        and not self.ctx._short_options())
        if isinstance(self.ctx, ShortWeeklyOptionLegs) and not waiting_roll:
            await self.ctx.advance(tick.at)
        await super().on_tick(tick)
        if isinstance(self.ctx, ShortWeeklyOptionLegs):
            await self.ctx.advance(tick.at)

    async def on_clock(self, now: datetime) -> None:
        waiting_roll = (isinstance(self.ctx, ShortWeeklyOptionLegs)
                        and self.ctx.roll_after is not None
                        and not self.ctx._short_options())
        if isinstance(self.ctx, ShortWeeklyOptionLegs) and not waiting_roll:
            await self.ctx.advance(now)
        await super().on_clock(now)
        if isinstance(self.ctx, ShortWeeklyOptionLegs):
            if not waiting_roll:
                await self.ctx.advance(now)
            await self.ctx.monitor_expiry(now)
            await self.ctx.monitor_reset(now)
