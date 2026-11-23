"""The 13L NIFTY signal executed as a weekly synthetic future.

The index remains the only signal and stop source.  A long signal buys the ATM call and sells
the same-strike ATM put; a short signal buys the ATM put and sells the same-strike ATM call.
The pair is replaced only on a direction change or weekly-expiry rollover.
"""

from dataclasses import replace
from datetime import date, timedelta

from app.brokers.base import OptionContract
from app.strategies.builtin.nifty_trailing_reversal_itm_option_seller import (
    EXPIRY_EXIT_TIME,
    HEDGE_RETRY_DISTANCES,
    MIN_HEDGE_POINTS,
    NiftyTrailingReversalOptimizedITMOptionSeller,
    ShortWeeklyOptionLegs,
)
from app.strategies.builtin.nifty_trailing_reversal_optimized import (
    IST,
    NiftyTrailingReversalOptimized,
)


# NSE F&O circular NSE/FAOP/71777, dated 12 December 2025. Broker masters normally publish
# the exchange-adjusted expiry already; this calendar is an additional guard when they retain the
# nominal Tuesday date. Extend it when NSE publishes the next annual derivatives calendar.
NSE_FO_HOLIDAYS = frozenset({
    date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26), date(2026, 3, 31),
    date(2026, 4, 3), date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28),
    date(2026, 6, 26), date(2026, 9, 14), date(2026, 10, 2), date(2026, 10, 20),
    date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25),
})


def _previous_nse_session(day: date) -> date:
    candidate = day
    while candidate.weekday() >= 5 or candidate in NSE_FO_HOLIDAYS:
        candidate -= timedelta(days=1)
    return candidate


def _hundred_strike(strike: float) -> bool:
    return abs(strike / 100 - round(strike / 100)) < 1e-9


class StrictHundredStrikeWeeklyOptionLegs(ShortWeeklyOptionLegs):
    """Select the nearest listed 100-point strike; never use a non-100 fallback."""

    same_session_expiry_roll = True
    require_fresh_hedge_quote = True
    reset_01 = 700.0
    reset_23_low = 700.0
    reset_23_high = 700.0
    reset_47_low = 700.0
    reset_47_high = 700.0
    # A Tuesday holiday moves the expiry exit to Monday. Rolling that session into the following
    # Tuesday is eight calendar days, while an ordinary Tuesday-to-Tuesday roll is seven.
    max_reset_dte = 8
    default_hedge_points = 800.0
    # Prefer the original deeper exposure, but step toward ATM whenever its live price is not
    # economically executable. All candidates are checked before any order is submitted.
    entry_itm_candidates = (1_000.0, 900.0, 800.0)
    validate_entry_prices = True
    max_quote_age_seconds = 2.0

    def _restore(self) -> None:
        super()._restore()
        # Migrate a live checkpoint frozen under the former DTE/IV grid so an already-open leg
        # also follows the new flat ₹700 reset rule after restart.
        if self.frozen is not None and self.frozen.reset_level != self.reset_01:
            self.frozen = replace(self.frozen, reset_level=self.reset_01)

    async def _select(self, kind, now, earliest_expiry=None, exact_expiry=None):
        spot = self._ctx.last_price
        if not spot:
            return None
        contracts = [
            contract for contract in await self._contracts()
            if contract.kind == kind
            and _hundred_strike(contract.strike)
            and self._expiry_entry_eligible(contract.expiry, now)
        ]
        if exact_expiry is not None:
            contracts = [contract for contract in contracts if contract.expiry == exact_expiry]
        start = earliest_expiry or now.astimezone(IST).date()
        expiries = sorted({contract.expiry for contract in contracts if contract.expiry >= start})
        if not expiries:
            return None
        chain = [contract for contract in contracts if contract.expiry == expiries[0]]

        for depth in self.entry_itm_candidates:
            target = spot - depth if kind == "CE" else spot + depth
            deeper = 1 if kind == "CE" else -1
            contract = min(
                chain,
                key=lambda candidate: (
                    abs(candidate.strike - target), deeper * candidate.strike,
                ),
            )
            quote = await self._quote(contract.symbol, "SELL", now)
            if quote is None:
                self._ctx.log(
                    f"entry candidate {depth:g}-ITM {contract.symbol} skipped: "
                    "sell quote unavailable or stale"
                )
                continue
            _, problem = self._entry_price_guard(contract, quote, "SELL")
            if problem:
                self._ctx.log(
                    f"entry candidate {depth:g}-ITM {contract.symbol} skipped: {problem}"
                )
                continue
            self._ctx.log(
                f"entry candidate selected: {depth:g}-ITM {contract.symbol} at bid "
                f"{quote.price:g}"
            )
            return contract
        return None

    async def _select_hedge(self, kind, expiry):
        contract = await super()._select_hedge(kind, expiry)
        return contract if contract is not None and _hundred_strike(contract.strike) else None

    def _session_open(self, now):
        return (
            super()._session_open(now)
            and now.astimezone(IST).date() not in NSE_FO_HOLIDAYS
        )

    def _expiry_exit_day(self, expiry):
        return _previous_nse_session(expiry)


class SyntheticWeeklyOptionLegs(ShortWeeklyOptionLegs):
    """Execute the signal with a same-expiry, same-strike ATM CE/PE pair."""

    state_key = "synthetic_weekly_option_legs"
    same_session_expiry_roll = True
    max_quote_age_seconds = 2.0
    default_hedge_points = 800.0

    def __init__(self, ctx, _legacy_itm_points: float = 0) -> None:
        # Keep the constructor compatible with the parent strategy while intentionally ignoring
        # the former ITM-distance parameter. Synthetic selection is always ATM.
        super().__init__(ctx, 0)
        self.active_strike: float | None = None
        self.active_expiry: date | None = None
        self.pair_ce: OptionContract | None = None
        self.pair_pe: OptionContract | None = None
        self.pair_hedge: OptionContract | None = None
        self.obsolete_hedge_symbol: str | None = None
        self.replace_pair = False
        self._restored_pair_symbols: dict[str, str] = {}
        self._restore_synthetic()

    def _option_positions(self) -> dict[str, int]:
        return {
            symbol: quantity for symbol, quantity in self._ctx.positions.items()
            if quantity and symbol.endswith(("CE", "PE"))
        }

    def _actual_direction(self) -> int:
        positions = self._option_positions()
        if (any(q > 0 and s.endswith("CE") for s, q in positions.items())
                and any(q < 0 and s.endswith("PE") for s, q in positions.items())):
            return 1
        if (any(q < 0 and s.endswith("CE") for s, q in positions.items())
                and any(q > 0 and s.endswith("PE") for s, q in positions.items())):
            return -1
        return 0

    @property
    def position(self) -> int:
        return self._actual_direction() * self.option_quantity

    def _leg_state(self) -> dict:
        return {
            "desired_direction": self.desired_direction,
            "active_strike": self.active_strike,
            "active_expiry": self.active_expiry.isoformat() if self.active_expiry else None,
            "pair_ce": self.pair_ce.symbol if self.pair_ce else None,
            "pair_pe": self.pair_pe.symbol if self.pair_pe else None,
            "pair_hedge": self.pair_hedge.symbol if self.pair_hedge else None,
            "hedge_distance": self.hedge_points,
            "obsolete_hedge_symbol": self.obsolete_hedge_symbol,
            "replace_pair": self.replace_pair,
            "roll_after": self.roll_after.isoformat() if self.roll_after else None,
            "roll_expiry_after": (
                self.roll_expiry_after.isoformat() if self.roll_expiry_after else None
            ),
        }

    def _restore_synthetic(self) -> None:
        state = getattr(self._ctx, "strategy_state", {}).get(self.state_key, {})
        if not isinstance(state, dict):
            return
        direction = state.get("desired_direction")
        if direction in (-1, 0, 1):
            self.desired_direction = int(direction)
        strike = state.get("active_strike")
        if isinstance(strike, (int, float)):
            self.active_strike = float(strike)
        distance = state.get("hedge_distance")
        if isinstance(distance, (int, float)) and MIN_HEDGE_POINTS <= distance <= self.configured_hedge_points:
            self.hedge_points = float(distance)
        obsolete = state.get("obsolete_hedge_symbol")
        if isinstance(obsolete, str):
            self.obsolete_hedge_symbol = obsolete
        self.replace_pair = bool(state.get("replace_pair"))
        self._restored_pair_symbols = {
            key: value for key, value in {
                "CE": state.get("pair_ce"),
                "PE": state.get("pair_pe"),
                "HEDGE": state.get("pair_hedge"),
            }.items() if isinstance(value, str)
        }
        for name in ("active_expiry", "roll_after", "roll_expiry_after"):
            value = state.get(name)
            if value:
                try:
                    setattr(self, name, date.fromisoformat(value))
                except (TypeError, ValueError):
                    pass

    def _session_open(self, now):
        return (
            super()._session_open(now)
            and now.astimezone(IST).date() not in NSE_FO_HOLIDAYS
        )

    def _expiry_exit_day(self, expiry):
        return _previous_nse_session(expiry)

    def set_desired(self, direction: int) -> None:
        changed = direction != self.desired_direction
        super().set_desired(direction)
        if changed and self._option_positions():
            self.replace_pair = True

    async def _select_pair(self, now, earliest_expiry=None, exact_expiry=None):
        spot = self._ctx.last_price
        if not spot:
            return None
        contracts = [
            contract for contract in await self._contracts()
            if _hundred_strike(contract.strike)
            and self._expiry_entry_eligible(contract.expiry, now)
        ]
        if exact_expiry is not None:
            contracts = [c for c in contracts if c.expiry == exact_expiry]
        start = earliest_expiry or now.astimezone(IST).date()
        expiries = sorted({c.expiry for c in contracts if c.expiry >= start})
        if not expiries:
            return None
        expiry = expiries[0]
        by_key = {(c.strike, c.kind): c for c in contracts if c.expiry == expiry}
        strikes = sorted({
            strike for strike, kind in by_key
            if (strike, "CE") in by_key and (strike, "PE") in by_key
        })
        if not strikes:
            return None
        # On an exact half-way tie, choose the lower strike deterministically.
        strike = min(strikes, key=lambda value: (abs(value - spot), value))
        return by_key[(strike, "CE")], by_key[(strike, "PE")]

    def _pair_guard(self, quote, side: str) -> tuple[float | None, str | None]:
        if quote.bid is None and quote.ask is None:
            return quote.price, None
        bid, ask = float(quote.bid or 0), float(quote.ask or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return None, f"invalid two-sided depth (bid {bid:g}, ask {ask:g})"
        midpoint = (bid + ask) / 2
        allowed = max(self.entry_spread_floor, midpoint * self.entry_spread_pct / 100)
        if ask - bid > allowed:
            return None, f"spread {ask - bid:.2f} exceeds {allowed:.2f}"
        return (bid + allowed if side == "BUY" else ask - allowed), None

    async def _validated_pair(self, now, direction, earliest_expiry=None):
        pair = await self._select_pair(now, earliest_expiry=earliest_expiry)
        if pair is None:
            self._ctx.log("entry deferred: no current-week nearest-100 ATM CE/PE pair")
            return None
        ce, pe = pair
        short = pe if direction > 0 else ce
        hedge = await self._select_hedge(short.kind, short.expiry)
        if hedge is None or not _hundred_strike(hedge.strike):
            self._ctx.log(
                f"entry deferred: no {short.kind} {self.hedge_points:g}-point OTM margin hedge"
            )
            return None
        bought = ce if direction > 0 else pe
        sides = ((bought, "BUY"), (hedge, "BUY"), (short, "SELL"))
        guards = {}
        for contract, side in sides:
            quote = await self._quote(contract.symbol, side, now)
            if quote is None:
                self._ctx.log(f"entry deferred: fresh {side.lower()} quote unavailable for {contract.symbol}")
                return None
            guard, problem = self._pair_guard(quote, side)
            if problem:
                self._ctx.log(f"entry deferred: {contract.symbol} {problem}")
                return None
            guards[contract.symbol] = guard
        return ce, pe, hedge, guards

    async def _close_one(self, reason: str) -> bool:
        positions = self._option_positions()
        # Remove short exposure first, then dispose of the long leg.
        item = next(((s, q) for s, q in sorted(positions.items()) if q < 0), None)
        if item is not None:
            await self._buy_overnight(abs(item[1]), reason, item[0])
            return True
        item = next(iter(sorted(positions.items())), None)
        if item is not None:
            await self._sell_overnight(item[1], reason, item[0])
            return True
        return False

    async def advance(self, now, reason="synthetic transition") -> None:
        if not self._session_open(now) or self._ctx.order_pending:
            return
        positions = self._option_positions()
        if positions and self.active_expiry is None:
            await self._contracts()
            held = [self.contracts_by_symbol.get(symbol) for symbol in positions]
            held = [contract for contract in held if contract is not None]
            if held:
                self.active_expiry = min(contract.expiry for contract in held)

        local = now.astimezone(IST)
        expiry_due = self.active_expiry is not None and (
            self._expiry_exit_day(self.active_expiry) < local.date()
            or (
                self._expiry_exit_day(self.active_expiry) == local.date()
                and local.time() >= EXPIRY_EXIT_TIME
            )
        )
        if expiry_due and self.roll_after is None:
            self.roll_expiry_after = self.active_expiry + timedelta(days=1)
            self.roll_after = local.date()
            await self._save_leg_state()

        actual = self._actual_direction()
        changing_direction = bool(positions) and (
            self.replace_pair or actual not in (0, self.desired_direction)
        )
        if (self.roll_after is not None or changing_direction) and positions:
            await self._close_one(
                "weekly synthetic expiry roll" if self.roll_after else reason
            )
            return
        if not self.desired_direction:
            return
        if not positions:
            rolling = self.roll_after is not None
            if self.replace_pair:
                self.pair_ce = self.pair_pe = self.pair_hedge = None
                self.active_strike = self.active_expiry = None
                self.hedge_points = self.configured_hedge_points
                self.replace_pair = False
            elif rolling:
                self.pair_ce = self.pair_pe = self.pair_hedge = None
                self.active_strike = self.active_expiry = None
                self.hedge_points = self.configured_hedge_points
                self.margin_retry_exhausted = False
            selected = await self._validated_pair(
                now, self.desired_direction,
                earliest_expiry=self.roll_expiry_after if self.roll_after else None,
            )
            if selected is None:
                return
            self.pair_ce, self.pair_pe, self.pair_hedge, guards = selected
            self.active_strike = self.pair_ce.strike
            self.active_expiry = self.pair_ce.expiry
            self.roll_after = self.roll_expiry_after = None
            await self._save_leg_state()
        else:
            guards = {}
            if self.pair_ce is None or self.pair_pe is None:
                await self._contracts()
                chain = list(self.contracts_by_symbol.values())
                self.pair_ce = self.contracts_by_symbol.get(
                    self._restored_pair_symbols.get("CE", "")
                ) or next(
                    (c for c in chain if c.kind == "CE" and c.strike == self.active_strike
                     and c.expiry == self.active_expiry), None,
                )
                self.pair_pe = self.contracts_by_symbol.get(
                    self._restored_pair_symbols.get("PE", "")
                ) or next(
                    (c for c in chain if c.kind == "PE" and c.strike == self.active_strike
                     and c.expiry == self.active_expiry), None,
                )
                self.pair_hedge = self.contracts_by_symbol.get(
                    self._restored_pair_symbols.get("HEDGE", "")
                )
        if self.pair_ce is None or self.pair_pe is None:
            return

        if self.obsolete_hedge_symbol:
            held = self._option_positions().get(self.obsolete_hedge_symbol, 0)
            if held > 0:
                await self._sell_overnight(
                    held, "replace margin hedge after margin rejection", self.obsolete_hedge_symbol,
                )
                return
            self.obsolete_hedge_symbol = None
            await self._save_leg_state()

        if self.pair_hedge is None:
            short_kind = "PE" if self.desired_direction > 0 else "CE"
            self.pair_hedge = await self._select_hedge(short_kind, self.pair_ce.expiry)
            if self.pair_hedge is None or not _hundred_strike(self.pair_hedge.strike):
                return

        quantity = self.option_quantity
        targets = (
            {
                self.pair_ce.symbol: quantity,
                self.pair_pe.symbol: -quantity,
                self.pair_hedge.symbol: quantity,
            }
            if self.desired_direction > 0 else
            {
                self.pair_ce.symbol: -quantity,
                self.pair_pe.symbol: quantity,
                self.pair_hedge.symbol: quantity,
            }
        )
        current = self._option_positions()

        # Reconcile risk-reducing changes before adding exposure, and establish the bought leg
        # before selling its synthetic counterpart.
        for symbol, held in sorted(current.items()):
            target = targets.get(symbol, 0)
            if held < target and held < 0:
                await self._buy_overnight(min(target - held, abs(held)), reason, symbol)
                return
        for symbol, held in sorted(current.items()):
            target = targets.get(symbol, 0)
            if held > target and held > 0:
                await self._sell_overnight(min(held - target, held), reason, symbol)
                return
        for symbol, target in targets.items():
            held = current.get(symbol, 0)
            if target > held and target > 0:
                guard = guards.get(symbol)
                if guard is None:
                    quote = await self._quote(symbol, "BUY", now)
                    if quote is None:
                        return
                    guard, problem = self._pair_guard(quote, "BUY")
                    if problem:
                        return
                await self._buy_overnight(target - held, reason, symbol, guard)
                return
        for symbol, target in targets.items():
            held = current.get(symbol, 0)
            if target < held and target < 0:
                if self.margin_retry_exhausted:
                    return
                guard = guards.get(symbol)
                if guard is None:
                    quote = await self._quote(symbol, "SELL", now)
                    if quote is None:
                        return
                    guard, problem = self._pair_guard(quote, "SELL")
                    if problem:
                        return
                result = await self._sell_overnight(held - target, reason, symbol, guard)
                if self._margin_rejection(result):
                    closer = next(
                        (distance for distance in HEDGE_RETRY_DISTANCES
                         if MIN_HEDGE_POINTS <= distance < self.hedge_points),
                        None,
                    )
                    if closer is None:
                        self.margin_retry_exhausted = True
                        self._ctx.log("synthetic short margin retry stopped at the hedge safety limit")
                    else:
                        self.obsolete_hedge_symbol = self.pair_hedge.symbol
                        self.hedge_points = closer
                        self.pair_hedge = None
                        self._ctx.log(
                            f"synthetic short margin rejected; retrying with {closer:g}-point OTM hedge"
                        )
                    await self._save_leg_state()
                return

    async def monitor_reset(self, now) -> None:
        # Synthetic parity tracks the underlying without premium-triggered strike resets.
        return None

    async def monitor_expiry(self, now) -> None:
        await self.advance(now, "weekly synthetic expiry roll")


class NiftyAdaptivePremiumReset13LStrategy(NiftyTrailingReversalOptimizedITMOptionSeller):
    """The 13L signal with a 0.10% initial stop and synthetic-futures execution."""

    name = "nifty_adaptive_premium_reset_13l"
    # This is a separate rule set. Never restore a checkpoint created by the older 0.40%-stop
    # strategy, even though both variants intentionally share their execution implementation.
    legacy_checkpoint_names = ()
    description = (
        "13 L strategy: current-week NIFTY synthetic futures at the nearest listed 100-point "
        "ATM strike. Long buys the ATM call and sells the ATM put; short buys the ATM put and "
        "sells the ATM call. The sold leg retains the existing bought 800-OTM margin hedge "
        "and 600-point fallback. Signals use a 0.10% initial and 0.40% trailing index stop."
    )
    tags = ("Experimental", "NIFTY", "Weekly options", "Synthetic futures", "ATM")
    initial_stop_pct = 0.1
    option_legs_class = SyntheticWeeklyOptionLegs
    backtest_note = (
        "The tester replays the unchanged NIFTY signal. Live execution uses an ATM synthetic "
        "future with its existing margin hedge and same-session 15:10 weekly rollover."
    )

    @classmethod
    def rules(cls, p: "NiftyAdaptivePremiumReset13LStrategy.Params") -> list[dict]:
        rules = NiftyTrailingReversalOptimized.rules.__func__(cls, p)
        for rule in rules:
            if rule["kind"] == "filter":
                rule["conditions"] += [
                    "Signals and stops use NIFTY index prices only",
                    "Use the current-week nearest listed 100-point ATM strike for both option legs",
                    "Buy the same-expiry option 800 points OTM on the sold-leg side as a margin hedge",
                    "If margin rejects the short, replace the hedge at 600 points OTM and retry",
                    "Validate fresh two-sided ATM and hedge quotes before starting an entry",
                ]
            elif rule["kind"] == "entry_long":
                rule["title"] = "LONG signal: buy ATM CE + sell ATM PE"
                rule["conditions"].append(
                    "Buy the ATM call and PE margin hedge before selling the same-strike ATM put"
                )
            elif rule["kind"] == "entry_short":
                rule["title"] = "SHORT signal: buy ATM PE + sell ATM CE"
                rule["conditions"].append(
                    "Buy the ATM put and CE margin hedge before selling the same-strike ATM call"
                )
            elif rule["kind"] == "risk":
                rule["conditions"] = [
                    condition.replace("0.1%", "0.10%").replace("0.4%", "0.40%")
                    for condition in rule["conditions"]
                ]
                rule["conditions"] += [
                    "There is no option-premium reset for a live synthetic pair",
                    "Keep the pair's strike and expiry until reversal or weekly rollover",
                    "The bought OTM hedge always matches the configured synthetic quantity",
                ]
            elif rule["kind"] == "exit":
                rule["conditions"] += [
                    "On reversal, cover the sold leg before closing bought legs, then select a fresh ATM pair",
                    "At 15:10 on expiry day, or the previous NSE session for a holiday expiry, close and roll into the next weekly pair in the same session",
                ]
        return rules
