"""Experimental rule variant of the adaptive NIFTY premium-reset seller.

Only the published signal rules differ. Option selection, hedging, quote validation,
premium resets, reversals, expiry handling and recovery all stay on the existing seller's
execution path.
"""

from datetime import date, timedelta

from app.strategies.builtin.nifty_trailing_reversal_itm_option_seller import (
    EXPIRY_EXIT_TIME,
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

    async def _select(self, kind, now, earliest_expiry=None, exact_expiry=None):
        contract = await super()._select(kind, now, earliest_expiry, exact_expiry)
        return contract if contract is not None and _hundred_strike(contract.strike) else None

    async def _select_hedge(self, kind, expiry):
        contract = await super()._select_hedge(kind, expiry)
        return contract if contract is not None and _hundred_strike(contract.strike) else None

    async def _expiry_due(self, symbol, now):
        due = await super()._expiry_due(symbol, now)
        if due is not None:
            return due
        contract = self.contracts_by_symbol.get(symbol)
        expiry = contract.expiry if contract else (self.frozen.expiry if self.frozen else None)
        if expiry is None:
            return None
        exit_day = _previous_nse_session(expiry)
        local = now.astimezone(IST)
        if exit_day < expiry and (
            exit_day < local.date()
            or (exit_day == local.date() and local.time() >= EXPIRY_EXIT_TIME)
        ):
            return expiry
        return None


class NiftyAdaptivePremiumReset13LStrategy(NiftyTrailingReversalOptimizedITMOptionSeller):
    """The experimental 13 L candidate with a 0.10% initial NIFTY stop."""

    name = "nifty_adaptive_premium_reset_13l"
    # This is a separate rule set. Never restore a checkpoint created by the older 0.40%-stop
    # strategy, even though both variants intentionally share their execution implementation.
    legacy_checkpoint_names = ()
    description = (
        "13 L strategy: a current-week 900-ITM NIFTY option short, using a 0.10% initial "
        "index stop, 0.40% trailing stop and frozen DTE/IV premium resets. The existing "
        "editable fixed-quantity sizing and bought 1000-OTM margin-hedge execution are retained."
    )
    tags = ("Experimental", "NIFTY", "Weekly options", "900 ITM")
    initial_stop_pct = 0.1
    option_legs_class = StrictHundredStrikeWeeklyOptionLegs
    backtest_note = (
        "The tester replays the experimental 0.10% initial-stop NIFTY signal engine. Live "
        "option execution is inherited unchanged from NIFTY Adaptive Premium Reset Seller."
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
            "Start expiry rollover at 15:10 IST and reopen next executable session":
                "Exit at 15:10 on expiry day, or the previous NSE session when expiry is a holiday; reopen next executable session",
        }
        for rule in rules:
            rule["conditions"] = [
                display_overrides.get(condition, condition)
                for condition in rule["conditions"]
            ]
        return rules
