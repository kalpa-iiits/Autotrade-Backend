"""Experimental rule variant of the adaptive NIFTY premium-reset seller.

Only the published signal rules differ. Option selection, hedging, quote validation,
premium resets, reversals, expiry handling and recovery all stay on the existing seller's
execution path.
"""

from app.strategies.builtin.nifty_trailing_reversal_itm_option_seller import (
    NiftyTrailingReversalOptimizedITMOptionSeller,
    ShortWeeklyOptionLegs,
)


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
        }
        for rule in rules:
            rule["conditions"] = [
                display_overrides.get(condition, condition)
                for condition in rule["conditions"]
            ]
        return rules
