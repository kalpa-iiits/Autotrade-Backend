import pytest

from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
    NiftyAdaptivePremiumReset13LStrategy,
)
from app.strategies.builtin.nifty_trailing_reversal_itm_option_seller import (
    NiftyTrailingReversalOptimizedITMOptionSeller,
)
from app.strategies.deployments import DEPLOYMENTS


def deployment(key):
    return next(value for value in DEPLOYMENTS if value.key == key)


def test_13l_strategy_changes_only_rule_metadata_and_initial_stop():
    old = NiftyTrailingReversalOptimizedITMOptionSeller
    new = NiftyAdaptivePremiumReset13LStrategy

    assert new.name == "nifty_adaptive_premium_reset_13l"
    assert new.legacy_checkpoint_names == ()
    assert new.initial_stop_pct == 0.1
    assert old.initial_stop_pct == 0.4
    assert new.trailing_stop_pct == old.trailing_stop_pct == 0.4
    assert new.cadence_minutes == old.cadence_minutes == 10
    assert new.timeframes == old.timeframes == ("5m",)

    # The complete lifecycle stays inherited from the older strategy. These are the methods that
    # drive option selection, resets, reversal sequencing, expiry rolls and clock retries.
    assert new.on_start is old.on_start
    assert new.on_tick is old.on_tick
    assert new.on_clock is old.on_clock


def test_13l_stop_math_uses_point_one_initial_and_point_four_trail():
    strategy = NiftyAdaptivePremiumReset13LStrategy(
        NiftyAdaptivePremiumReset13LStrategy.Params(itm_points=900), object(), 300,
    )

    assert strategy._initial_stop(25_000, long=True) == pytest.approx(24_975)
    assert strategy._initial_stop(25_000, long=False) == pytest.approx(25_025)

    strategy.stop = 24_975
    strategy._ratchet(25_100, long=True)
    assert strategy.stop == pytest.approx(24_999.6)


def test_13l_rules_match_the_attached_candidate_and_keep_reset_grid():
    strategy = NiftyAdaptivePremiumReset13LStrategy
    rules = strategy.rules(strategy.Params(itm_points=900))
    filters = next(rule for rule in rules if rule["kind"] == "filter")["conditions"]
    risk = next(rule for rule in rules if rule["kind"] == "risk")["conditions"]

    assert any("0.10% from the fill" in condition for condition in risk)
    assert any("0.40% behind the completed close" in condition for condition in risk)
    assert any("configured quantity" in condition for condition in filters)
    assert not any("fixed at 65 units" in condition for condition in risk)
    assert any("0–1 DTE ₹850" in condition for condition in risk)
    assert any("2–3 LOW ₹900/HIGH ₹700" in condition for condition in risk)
    assert any("4–6 LOW ₹850/HIGH ₹900" in condition for condition in risk)


def test_13l_deployment_is_separate_editable_quantity_and_old_seller_is_unchanged():
    new = deployment("nifty-adaptive-premium-reset-13l")
    old = deployment("nifty-percentage-reversal-optimized-option-seller")

    assert new.label == "13 L strategy"
    assert new.strategy is NiftyAdaptivePremiumReset13LStrategy
    assert new.symbol == old.symbol == "NSE:NIFTY50-INDEX"
    assert new.timeframe == old.timeframe == "5m"
    assert new.params.itm_points == old.params.itm_points == 900
    assert new.quantity == new.backtest_fixed_quantity == 65
    assert new.max_order_qty == new.max_position == 1_800
    assert not new.strategy.capital_sizing

    assert old.label == "NIFTY Adaptive Premium Reset Seller"
    assert old.strategy is NiftyTrailingReversalOptimizedITMOptionSeller
    assert old.quantity == old.backtest_fixed_quantity == 65
    assert old.max_order_qty == old.max_position == 1_800
    assert old.strategy.initial_stop_pct == 0.4
