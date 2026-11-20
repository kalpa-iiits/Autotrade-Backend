"""Optimized NIFTY trailing-reversal signals executed with one short weekly ITM option.

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
OPTION_QUANTITY = 65
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
    if not 0 <= dte <= 6:
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
    """Map optimized spot direction to exactly one 65-unit current-week option short."""
    state_key = "weekly_option_leg"

    def __init__(self, ctx, itm_points: float) -> None:
        self._ctx = ctx
        self.itm_points = itm_points
        self.desired_direction = self._actual_direction()
        self.replace_symbol: str | None = None
        self.pending_contract: OptionContract | None = None
        self.pending_reason: str | None = None
        self.frozen: FrozenReset | None = None
        self.last_premium: float | None = None
        self.reset_armed = False
        self.reset_pending = False
        self.roll_after: date | None = None
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
            return actual * OPTION_QUANTITY
        # While waiting for the next session after expiry, keep the spot signal virtually in
        # its continuing direction so its overnight stop can react to the first 09:15 tick.
        return (self.desired_direction * OPTION_QUANTITY
                if self.pending_contract or self.roll_after else 0)

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
        self.pending_audit = state.get("pending_audit") if isinstance(state.get("pending_audit"), dict) else None

    def _leg_state(self) -> dict:
        return {
            "frozen": self.frozen.as_dict() if self.frozen else None,
            "last_premium": self.last_premium,
            "reset_armed": self.reset_armed,
            "reset_pending": self.reset_pending,
            "roll_after": self.roll_after.isoformat() if self.roll_after else None,
            "pending_audit": self.pending_audit,
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
            self.replace_symbol = self.pending_contract = self.pending_reason = None
            self.frozen = self.last_premium = self.roll_after = None
            self.reset_armed = self.reset_pending = False
            # A spot reversal after the expiry close changes the option type, not the rule that
            # no new weekly option opens until the next executable session.
            self.roll_after = pending_roll
        self.desired_direction = direction

    async def buy(self, quantity: int | None = None, reason: str | None = None,
                  product: Product | None = None) -> None:
        self.set_desired(1)
        await self.advance(datetime.now(IST), reason or "reversal")

    async def sell(self, quantity: int | None = None, reason: str | None = None,
                   product: Product | None = None) -> None:
        self.set_desired(-1)
        await self.advance(datetime.now(IST), reason or "reversal")

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
            "quantity": OPTION_QUANTITY, "order_fill_price": None,
            "quote_source": quote.source, "reason": reason,
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
        self.pending_contract, self.pending_reason = contract, reason
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
        if self._ctx.order_pending or not self.desired_direction or self.replace_symbol:
            return
        local = now.astimezone(IST)
        current = next(iter(self._short_options()), None)
        if current is None:
            return
        contract = self.contracts_by_symbol.get(current)
        if contract is None:
            await self._contracts()
            contract = self.contracts_by_symbol.get(current)
        if contract and contract.expiry == local.date() and local.time() >= EXPIRY_EXIT_TIME:
            next_day = local.date() + timedelta(days=1)
            if not await self._prepare(self._kind(self.desired_direction), now, "expiry", next_day):
                return
            self.replace_symbol, self.roll_after = current, next_day
            self.frozen = self.last_premium = None
            self.reset_armed = self.reset_pending = False
            await self._save_leg_state()
            await self.advance(now, "weekly expiry roll")

    async def advance(self, now: datetime, reason: str = "option transition") -> None:
        await self._complete_fill_audit()
        if self._ctx.order_pending or not self.desired_direction:
            return
        positions, wanted = self._short_options(), self._kind(self.desired_direction)
        if self.replace_symbol and self.replace_symbol in positions:
            await self._ctx.buy(abs(positions[self.replace_symbol]), reason,
                                symbol=self.replace_symbol, product=Product.DELIVERY)
            return
        if self.replace_symbol and self.replace_symbol not in positions:
            self.replace_symbol = None
            # The next-week contract was identified before the expiry close as a safety
            # prerequisite. Do not carry yesterday's strike through an overnight gap: the next
            # executable session must select 900-ITM again from that session's current spot.
            if self.roll_after is not None:
                self.pending_contract = self.pending_reason = None
            positions = self._short_options()
        obsolete = next((s for s in positions if not s.endswith(wanted)), None)
        if obsolete is not None:
            if self.pending_contract is None and not await self._prepare(wanted, now, "reversal"):
                return
            await self._ctx.buy(abs(positions[obsolete]), f"{reason}; cover obsolete {obsolete[-2:]}",
                                symbol=obsolete, product=Product.DELIVERY)
            return
        held = next(((s, q) for s, q in positions.items() if s.endswith(wanted)), None)
        if held is not None:
            symbol, quantity = held
            if abs(quantity) > OPTION_QUANTITY:
                await self._ctx.buy(abs(quantity) - OPTION_QUANTITY,
                                    "reconcile option quantity above 65", symbol=symbol,
                                    product=Product.DELIVERY)
            elif abs(quantity) < OPTION_QUANTITY:
                quote = await self._quote(symbol, "SELL", now)
                if quote is not None:
                    await self._ctx.sell(OPTION_QUANTITY - abs(quantity),
                                         "reconcile partial option entry to 65", symbol=symbol,
                                         product=Product.DELIVERY)
            return
        if self.roll_after and now.astimezone(IST).date() < self.roll_after:
            return

        contract = self.pending_contract or await self._select(wanted, now)
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
        event = self.pending_reason or ("reset" if self.reset_pending else reason)
        if self.frozen is None or event in {"reversal", "expiry"} or "reverse" in event:
            self.frozen = calculated
        self.last_premium, self.roll_after = quote.price, None
        self.reset_armed = quote.price > self.frozen.reset_level
        self.reset_pending = False
        self.pending_contract = self.pending_reason = None
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
        await self._ctx.sell(
            OPTION_QUANTITY,
            f"{event}; short {wanted} {contract.strike:g} {contract.expiry:%d %b}; "
            f"reset ₹{self.frozen.reset_level:g}; one lot (65 units)",
            symbol=contract.symbol, product=Product.DELIVERY,
        )


class NiftyTrailingReversalOptimizedITMOptionSeller(NiftyTrailingReversalOptimized):
    name = "nifty_trailing_reversal_optimized_itm_option_seller"
    description = ("Unchanged optimized NIFTY signals executed by selling exactly 65 units "
                   "of a current-week 900-ITM option with frozen DTE/IV premium resets.")
    tags = ("Positional", "NIFTY", "Weekly options", "900 ITM", "65 units")
    capital_sizing = False
    primary_symbol_only = True
    backtest_note = ("The tester replays the unchanged NIFTY signal engine. Live option "
                     "execution uses current-week quotes and the frozen DTE/IV reset table.")

    class Params(NiftyTrailingReversalOptimized.Params):
        itm_points: float = Field(900, gt=0, description="Weekly option distance in the money")

    @classmethod
    def rules(cls, p: "NiftyTrailingReversalOptimizedITMOptionSeller.Params") -> list[dict]:
        rules = super().rules(p)
        for rule in rules:
            if rule["kind"] == "filter":
                rule["conditions"] += ["Signals/stops use NIFTY index prices only",
                                       "Exactly 65 units of one nearest-expiry option are short",
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
                rule["conditions"] += ["Cover before replacement; never retain both directions",
                                       "Start expiry rollover at 15:10 IST and reopen next executable session"]
        return rules

    async def on_start(self) -> None:
        if hasattr(self.ctx, "option_contracts"):
            self.ctx = ShortWeeklyOptionLegs(self.ctx, self.params.itm_points)
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
