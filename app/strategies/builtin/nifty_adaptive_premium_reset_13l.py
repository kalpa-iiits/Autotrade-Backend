"""Experimental rule variant of the adaptive NIFTY premium-reset seller.

Signals, hedging, quote validation, premium resets, reversals and recovery use the existing
seller's execution path. Candidate-specific overrides enforce 100-point strikes, holiday-aware
expiry exits and same-session weekly rollover.
"""

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
    reset_01 = 800.0
    reset_23_low = 800.0
    reset_47_low = 800.0
    reset_47_high = 800.0
    # A Tuesday holiday moves the expiry exit to Monday. Rolling that session into the following
    # Tuesday is eight calendar days, while an ordinary Tuesday-to-Tuesday roll is seven.
    max_reset_dte = 8
    default_hedge_points = 800.0

    async def _select(self, kind, now, earliest_expiry=None, exact_expiry=None):
        contract = await super()._select(kind, now, earliest_expiry, exact_expiry)
        return contract if contract is not None and _hundred_strike(contract.strike) else None

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
        "13 L strategy: a current-week 900-ITM NIFTY option short, using a 0.10% initial "
        "index stop, 0.40% trailing stop and frozen DTE/IV premium resets. The existing "
        "editable fixed-quantity sizing and bought 800-OTM margin-hedge execution are retained."
    )
    tags = ("Experimental", "NIFTY", "Weekly options", "900 ITM")
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
                "Reset: 0–1 DTE ₹800; 2–3 LOW ₹800/HIGH ₹700; 4–8 LOW/HIGH ₹800",
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
        return rules
