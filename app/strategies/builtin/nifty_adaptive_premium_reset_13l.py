"""Experimental rule variant of the adaptive NIFTY premium-reset seller.

Signals, hedging, quote validation, premium resets, reversals and recovery use the existing
seller's execution path. Candidate-specific overrides enforce 100-point strikes, holiday-aware
expiry exits and same-session weekly rollover.
"""

from dataclasses import replace
from datetime import date, timedelta

from app.strategies.builtin.nifty_trailing_reversal_itm_option_seller import (
    NiftyTrailingReversalOptimizedITMOptionSeller,
    ShortWeeklyOptionLegs,
)
from app.strategies.builtin.nifty_trailing_reversal_optimized import IST


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


class NiftyAdaptivePremiumReset13LStrategy(NiftyTrailingReversalOptimizedITMOptionSeller):
    """The experimental 13 L candidate with a 0.10% initial NIFTY stop."""

    name = "nifty_adaptive_premium_reset_13l"
    # This is a separate rule set. Never restore a checkpoint created by the older 0.40%-stop
    # strategy, even though both variants intentionally share their execution implementation.
    legacy_checkpoint_names = ()
    description = (
        "13 L strategy: a current-week NIFTY option short selected from valid 1000/900/800-ITM "
        "prices, using a 0.10% initial "
        "index stop, 0.40% trailing stop and frozen DTE/IV premium resets. The existing "
        "editable fixed-quantity sizing and bought 800-OTM margin-hedge execution are retained."
    )
    tags = ("Experimental", "NIFTY", "Weekly options", "1000/900/800 ITM")
    initial_stop_pct = 0.1
    option_legs_class = StrictHundredStrikeWeeklyOptionLegs
    backtest_note = (
        "The published ₹12.91L replay used next-session expiry rollover. The live strategy "
        "uses the user-selected same-session 15:10 rollover rule."
    )

    @classmethod
    def rules(cls, p: "NiftyAdaptivePremiumReset13LStrategy.Params") -> list[dict]:
        rules = super().rules(p)
        display_overrides = {
            "From its fill, each position gets a stop 0.1% from the fill":
                "From its fill, each position gets a stop 0.10% from the fill",
            "Every 10 minutes from 09:15 IST, ratchet to 0.4% behind the completed close":
                "Every 10 minutes from 09:15 IST, ratchet to 0.40% behind the completed close",
            "Prefer strikes divisible by 100; fall back only if unavailable":
                "Use the nearest listed strike divisible by 100; never use a non-100 strike",
            "Reset: 0–1 DTE ₹850; 2–3 LOW ₹900/HIGH ₹700; 4–6 LOW ₹850/HIGH ₹900":
                "Reset all DTE and IV regimes at ₹700",
            "Start expiry rollover at 15:10 IST and reopen next executable session":
                "At 15:10 on expiry day, or the previous NSE session for a holiday expiry, cover and roll into the next weekly contract in the same session",
        }
        for rule in rules:
            rule["conditions"] = [
                display_overrides.get(condition, condition)
                for condition in rule["conditions"]
            ]
            if rule["kind"] == "exit":
                rule["conditions"].append(
                    "At rollover, price the hedge buy from the current ask and the short sale from the current bid"
                )
            if rule["kind"] == "filter":
                rule["conditions"].append(
                    "Before entry, check 1000-, 900- and 800-ITM quotes in that order; use "
                    "the deepest contract with a valid two-sided price and fair-value floor"
                )
                rule["conditions"].append(
                    "Fair value adds ₹3 per DTE to actual intrinsic value; the minimum short "
                    "bid is the greater of intrinsic less its tolerance and fair value less "
                    "max(₹15, 2%)"
                )
                rule["conditions"].append(
                    "Reject an overpriced hedge ask; protected limits never cross the validated "
                    "buy ceiling or short-sale floor"
                )
                rule["conditions"].append(
                    "Premium resets reuse a fully sized same-type, same-expiry hedge; replace it "
                    "only for reversal, expiry, quantity shortfall or a margin-rejection fallback"
                )
        return rules
