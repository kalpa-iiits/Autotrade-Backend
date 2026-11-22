import asyncio
from datetime import datetime, timedelta

import pytest

from app.brokers.base import Candle
from app.strategies.backtest import Simulation
from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
    NiftyAdaptivePremiumReset13LStrategy,
    StrictHundredStrikeWeeklyOptionLegs,
)
from app.strategies.builtin.nifty_trailing_reversal_itm_option_seller import (
    NiftyTrailingReversalOptimizedITMOptionSeller,
)
from app.strategies.builtin.nifty_trailing_reversal_optimized import (
    IST,
    NiftyTrailingReversalOptimized,
)
from app.strategies.deployment import Deployment
from app.strategies.deployments import DEPLOYMENTS


def deployment(key):
    return next(value for value in DEPLOYMENTS if value.key == key)


class ExperimentalSignalReference(NiftyTrailingReversalOptimized):
    """Independent 0.10% signal reference for backtest-parity regression coverage."""

    name = "test_13l_signal_reference"
    initial_stop_pct = 0.1


def test_13l_strategy_changes_only_rule_metadata_and_initial_stop():
    old = NiftyTrailingReversalOptimizedITMOptionSeller
    new = NiftyAdaptivePremiumReset13LStrategy

    assert new.name == "nifty_adaptive_premium_reset_13l"
    assert new.legacy_checkpoint_names == ()
    assert new.initial_stop_pct == 0.1
    assert old.initial_stop_pct == 0.4
    assert "trailing_stop_pct" not in new.__dict__
    assert "cadence_minutes" not in new.__dict__
    assert new.trailing_stop_pct == old.trailing_stop_pct == 0.4
    assert new.cadence_minutes == old.cadence_minutes == 10
    assert new.timeframes == old.timeframes == ("5m",)
    assert new.option_legs_class is StrictHundredStrikeWeeklyOptionLegs
    assert old.option_legs_class is not StrictHundredStrikeWeeklyOptionLegs

    # The complete lifecycle stays inherited from the older strategy. These are the methods that
    # drive option selection, resets, reversal sequencing, expiry rolls and clock retries.
    assert new.on_start is old.on_start
    assert new.on_tick is old.on_tick
    assert new.on_clock is old.on_clock
    assert new.on_bar is old.on_bar
    assert new.on_backtest_candle is old.on_backtest_candle


def test_13l_stop_math_uses_point_one_initial_and_point_four_trail():
    strategy = NiftyAdaptivePremiumReset13LStrategy(
        NiftyAdaptivePremiumReset13LStrategy.Params(itm_points=900), object(), 300,
    )

    assert strategy._initial_stop(25_000, long=True) == pytest.approx(24_975)
    assert strategy._initial_stop(25_000, long=False) == pytest.approx(25_025)

    strategy.stop = 24_975
    strategy._ratchet(25_100, long=True)
    assert strategy.stop == pytest.approx(24_999.6)


def test_13l_backtest_trades_exactly_match_point_one_signal_reference():
    first = datetime(2026, 11, 2, 9, 15, tzinfo=IST)
    candles = [
        Candle(first, 24_950, 25_000, 24_900, 24_980),
        # The initial LONG fills at 25,000. Its 0.10% stop at 24,975 is hit intrabar.
        Candle(first + timedelta(minutes=5), 25_000, 25_020, 24_950, 24_960),
        # The next candle gaps above the SHORT stop, so historical replay uses 25,050.
        Candle(first + timedelta(minutes=10), 25_050, 25_060, 25_010, 25_030),
        Candle(first + timedelta(minutes=15), 25_030, 25_100, 25_020, 25_090),
        Candle(first + timedelta(minutes=20), 25_090, 25_100, 25_000, 25_010),
    ]

    async def replay(strategy, params):
        candidate = Deployment(
            key=f"test-{strategy.name}",
            strategy=strategy,
            symbol="NSE:NIFTY50-INDEX",
            timeframe="5m",
            params=params,
            quantity=65,
            max_order_qty=1_800,
            max_position=1_800,
            max_orders_per_day=100,
            backtest_fixed_quantity=65,
        )
        simulation = Simulation(candidate, candidate.symbol, "5m", daily=[])
        await simulation.run(candles)
        return [
            (
                trade.direction,
                trade.entry_at,
                trade.entry_price,
                trade.exit_at,
                trade.exit_price,
                trade.quantity,
            )
            for trade in simulation.trades
        ]

    reference = asyncio.run(replay(
        ExperimentalSignalReference,
        ExperimentalSignalReference.Params(),
    ))
    strategy = asyncio.run(replay(
        NiftyAdaptivePremiumReset13LStrategy,
        NiftyAdaptivePremiumReset13LStrategy.Params(itm_points=900),
    ))
    previous = asyncio.run(replay(
        NiftyTrailingReversalOptimizedITMOptionSeller,
        NiftyTrailingReversalOptimizedITMOptionSeller.Params(itm_points=900),
    ))

    assert strategy == reference
    assert strategy != previous


def test_13l_rules_match_the_attached_candidate_and_keep_reset_grid():
    strategy = NiftyAdaptivePremiumReset13LStrategy
    rules = strategy.rules(strategy.Params(itm_points=900))
    filters = next(rule for rule in rules if rule["kind"] == "filter")["conditions"]
    risk = next(rule for rule in rules if rule["kind"] == "risk")["conditions"]
    exits = next(rule for rule in rules if rule["kind"] == "exit")["conditions"]

    assert any("0.10% from the fill" in condition for condition in risk)
    assert any("0.40% behind the completed close" in condition for condition in risk)
    assert any("configured quantity" in condition for condition in filters)
    assert any("nearest listed strike divisible by 100" in condition for condition in filters)
    assert not any("fall back" in condition for condition in filters)
    assert any("previous NSE session" in condition and "same session" in condition
               for condition in exits)
    assert any("current ask" in condition and "current bid" in condition for condition in exits)
    assert not any("fixed at 65 units" in condition for condition in risk)
    assert any("0–1 DTE ₹850" in condition for condition in risk)
    assert any("2–3 LOW ₹900/HIGH ₹700" in condition for condition in risk)
    assert any("4–7 LOW ₹850/HIGH ₹900" in condition for condition in risk)


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
