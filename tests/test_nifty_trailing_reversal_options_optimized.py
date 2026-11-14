import asyncio
from datetime import datetime, timedelta
from types import CodeType, FunctionType

import pytest

from app.brokers.base import Candle
from app.strategies.backtest import Simulation
from app.strategies.builtin import (
    nifty_trailing_reversal_options as original_options_module,
)
from app.strategies.builtin import (
    nifty_trailing_reversal_options_optimized as optimized_options_module,
)
from app.strategies.builtin.nifty_trailing_reversal import NiftyTrailingReversal
from app.strategies.builtin.nifty_trailing_reversal_optimized import (
    IST,
    NiftyTrailingReversalOptimized,
)
from app.strategies.builtin.nifty_trailing_reversal_options import (
    NiftyTrailingReversalOptions,
)
from app.strategies.builtin.nifty_trailing_reversal_options_optimized import (
    NiftyTrailingReversalOptionsOptimized,
)
from app.strategies.deployment import Deployment
from app.strategies.deployments import DEPLOYMENTS

SYM = "NSE:NIFTY50-INDEX"


def _methods(cls):
    return {
        name: value
        for name, value in cls.__dict__.items()
        if isinstance(value, FunctionType)
    }


def _normalized_constants(code):
    return tuple(
        (constant.co_code, _normalized_constants(constant))
        if isinstance(constant, CodeType) else constant
        for constant in code.co_consts
    )


def test_complete_signal_execution_was_copied_without_method_changes():
    original = _methods(NiftyTrailingReversal)
    optimized = _methods(NiftyTrailingReversalOptimized)
    assert original.keys() == optimized.keys()
    for name in original:
        # The checkpoint compatibility table contains the intended 0.8 -> 0.4 condition
        # change. Every other method still has the same executable bytecode and constants.
        if name == "_checkpoint_params_match":
            continue
        assert optimized[name].__code__.co_code == original[name].__code__.co_code, name
        assert _normalized_constants(optimized[name].__code__) == _normalized_constants(
            original[name].__code__
        ), name


def test_complete_option_execution_was_copied_without_method_changes():
    original = _methods(NiftyTrailingReversalOptions)
    optimized = _methods(NiftyTrailingReversalOptionsOptimized)
    assert original.keys() == optimized.keys()
    for name in original:
        assert optimized[name].__code__.co_code == original[name].__code__.co_code, name
        assert _normalized_constants(optimized[name].__code__) == _normalized_constants(
            original[name].__code__
        ), name

    original_legs = _methods(original_options_module.ReversalOptionLegs)
    optimized_legs = _methods(optimized_options_module.ReversalOptionLegs)
    assert original_legs.keys() == optimized_legs.keys()
    for name in original_legs:
        assert optimized_legs[name].__code__.co_code == original_legs[name].__code__.co_code, name
        assert _normalized_constants(optimized_legs[name].__code__) == _normalized_constants(
            original_legs[name].__code__
        ), name


def test_only_initial_protective_stop_condition_changed():
    assert NiftyTrailingReversal.initial_stop_pct == 0.8
    assert NiftyTrailingReversalOptimized.initial_stop_pct == 0.4
    assert NiftyTrailingReversalOptimized.trailing_stop_pct == NiftyTrailingReversal.trailing_stop_pct == 0.4
    assert NiftyTrailingReversalOptimized.cadence_minutes == NiftyTrailingReversal.cadence_minutes == 10
    assert NiftyTrailingReversalOptionsOptimized.initial_stop_pct == 0.4

    rules = NiftyTrailingReversalOptionsOptimized.rules(
        NiftyTrailingReversalOptionsOptimized.Params()
    )
    risk = next(rule for rule in rules if rule["kind"] == "risk")
    assert risk["conditions"][:2] == [
        "From its fill, each position gets a stop 0.4% from the fill",
        "Every 10 minutes from 09:15 IST, ratchet to 0.4% behind the completed close",
    ]


def test_optimized_backtest_arms_the_recommended_point_four_percent_stop():
    first = datetime(2026, 11, 2, 9, 15, tzinfo=IST)
    bars = [
        Candle(first, 24_950, 25_000, 24_900, 24_980),
        Candle(first + timedelta(minutes=5), 25_000, 25_050, 24_850, 24_950),
    ]
    deployment = Deployment(
        key="optimized-reversal-test",
        strategy=NiftyTrailingReversalOptionsOptimized,
        symbol=SYM,
        timeframe="5m",
        quantity=65,
        max_order_qty=650,
        max_position=650,
        max_orders_per_day=100,
        backtest_initial_capital=1_000_000,
        backtest_fixed_quantity=65,
        params=NiftyTrailingReversalOptionsOptimized.Params(itm_points=1000, history_days=7),
    )
    simulation = Simulation(deployment, SYM, "5m", daily=[])
    asyncio.run(simulation.run(bars))

    assert simulation.trades[0].direction == "LONG"
    assert simulation.trades[0].entry_price == 25_000
    assert simulation.trades[0].exit_price == pytest.approx(25_000 * 0.996)


def test_optimized_options_strategy_is_a_separate_five_minute_deployment():
    original = next(d for d in DEPLOYMENTS if d.key == "nifty-percentage-reversal-options")
    optimized = next(
        d for d in DEPLOYMENTS
        if d.key == "nifty-percentage-reversal-options-optimized"
    )

    assert original.strategy is NiftyTrailingReversalOptions
    assert optimized.strategy is NiftyTrailingReversalOptionsOptimized
    assert optimized.label == "NIFTY Percentage Trailing Reversal · Options · Optimized"
    assert optimized.symbol == original.symbol == SYM
    assert optimized.timeframe == original.timeframe == "5m"
    assert optimized.quantity == original.quantity == 65
    assert optimized.max_order_qty == original.max_order_qty == 650
    assert optimized.max_position == original.max_position == 650
    assert optimized.max_orders_per_day == original.max_orders_per_day == 100
    assert optimized.backtest_initial_capital == original.backtest_initial_capital == 1_000_000
    assert optimized.backtest_fixed_quantity == original.backtest_fixed_quantity == 65
    assert optimized.params.model_dump() == original.params.model_dump() == {
        "itm_points": 1000.0,
        "history_days": 7,
    }
