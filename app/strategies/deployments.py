"""The strategies this app runs. Edit this file to add, change or remove one.

Each Deployment is a strategy with its parameters, size, limits and mode. Which symbol and
timeframe it runs on is chosen in the app, where you can also add more instances of it (e.g.
the same breakout on NIFTY and BANKNIFTY). `symbol`, when present, is only a suggestion in the
deployment form; `timeframe` is its initial selection.

Changes take effect when the server restarts (with --reload, on save) and apply to every
instance; running instances come back stopped after a change. Removing a deployment here marks
its instances "removed" and keeps their signal history.

A running instance places real orders on its account's broker: there is no dry-run mode. To
try a strategy without real money, run it on a paper account.
"""

from app.strategies.builtin.intraday_breakout import IntradayBreakout
from app.strategies.builtin.intraday_breakout_options import IntradayBreakoutOptions
from app.strategies.builtin.nifty_intraday_limited_options import NiftyIntradayLimitedOptions
from app.strategies.builtin.nifty_trailing_reversal_hedged_futures import (
    NiftyTrailingReversalHedgedFutures,
    NiftyTrailingReversalOptimizedHedgedFutures,
)
from app.strategies.builtin.nifty_trailing_reversal_itm_option_seller import (
    NiftyTrailingReversalOptimizedITMOptionSeller,
)
from app.strategies.builtin.sma_crossover import SmaCrossover
from app.strategies.builtin.smart_money_concepts_options import (
    InternalChoch3RCalls,
    InternalChoch3RPuts,
)
from app.strategies.deployment import Deployment

DEPLOYMENTS: list[Deployment] = [
    Deployment(
        key="nifty-intraday-limited",
        label="NIFTY Limited-Trade Strategy · 700-point ITM Options",
        strategy=NiftyIntradayLimitedOptions,
        symbol="NSE:NIFTY50-INDEX",
        timeframe="5m",
        params=NiftyIntradayLimitedOptions.Params(
            lookback=18,
            z_threshold=1.0,
            stop_points=50,
            target_points=300,
            max_entries_per_day=3,
            itm_points=700,
        ),
        quantity=65,
        max_order_qty=1_800,
        max_position=1_800,
        max_orders_per_day=10,
        backtest_initial_capital=500_000,
        backtest_fixed_quantity=65,
        backtest_commission_per_unit=0,
    ),
    # Hedged current-month futures: buy the 500-point OTM option before opening/reversing the
    # future, so the broker can apply the lower hedged margin. Update the future at monthly roll.
    Deployment(
        key="nifty-percentage-reversal",
        label="NIFTY Percentage Trailing Reversal · Hedged Current-Month Futures",
        strategy=NiftyTrailingReversalHedgedFutures,
        symbol="NSE:NIFTY26NOVFUT",
        timeframe="5m",
        params=NiftyTrailingReversalHedgedFutures.Params(hedge_points=500),
        quantity=65,
        # Capital sizing may target up to 1,800 units. A net reversal order can be twice the
        # position size because it closes the old side and opens the new side in one order.
        max_order_qty=10_000,
        max_position=1_800,
        max_orders_per_day=100,
        backtest_initial_capital=1_000_000,
    ),
    # Same current-month future execution with the optimized 0.4% initial protective stop.
    Deployment(
        key="nifty-percentage-reversal-optimized",
        label="NIFTY Percentage Trailing Reversal · Hedged Current-Month Futures · Optimized",
        strategy=NiftyTrailingReversalOptimizedHedgedFutures,
        symbol="NSE:NIFTY26NOVFUT",
        timeframe="5m",
        params=NiftyTrailingReversalOptimizedHedgedFutures.Params(hedge_points=500),
        quantity=65,
        max_order_qty=10_000,
        max_position=1_800,
        max_orders_per_day=100,
        backtest_initial_capital=1_000_000,
    ),
    # Same optimized NIFTY trigger engine, but the execution leg is one current-week option
    # short. It is separate from the futures deployment so existing live instances are not
    # silently converted to a materially different instrument.
    Deployment(
        key="nifty-percentage-reversal-optimized-option-seller",
        label="NIFTY Adaptive Premium Reset Seller",
        strategy=NiftyTrailingReversalOptimizedITMOptionSeller,
        symbol="NSE:NIFTY50-INDEX",
        timeframe="5m",
        params=NiftyTrailingReversalOptimizedITMOptionSeller.Params(
            itm_points=900,
        ),
        quantity=65,
        # Keep the default at one NIFTY lot while allowing larger fixed-size deployments.
        max_order_qty=1_800,
        max_position=1_800,
        max_orders_per_day=100,
        backtest_initial_capital=1_000_000,
        backtest_fixed_quantity=65,
        backtest_commission_per_unit=0,
    ),
    # Intraday breakout (ported from the TradingView script). By default an instance trades one
    # futures lot (65); users can pick another fixed quantity or capital per trade in the app,
    # up to 5,000 units per order and 1,800 units per position (room for capital sizing on stocks,
    # e.g. ₹5L of
    # a ₹100 share). Strategy Tester sizes entries from its own capital / order-size settings.
    # The user chooses the instrument and timeframe for each instance in the app.
    # 8 trades/day = up to 16 orders plus square-off, so the order cap is 20.
    Deployment(
        key="nifty-1m-breakout",
        label="Intraday Breakout",
        strategy=IntradayBreakout,
        timeframe="5m",
        params=IntradayBreakout.Params(
            lookback=5, max_width=0.4, avg_days=14,
            stop_k=0.25, trail_k=0.40, max_trades_per_day=8,
            entries_from="10:30", entries_until="14:45", square_off="15:15",
        ),
        quantity=65,
        max_order_qty=5_000,
        max_position=1_800,
        max_orders_per_day=20,
        # The reference TradingView report has commission overridden to zero: its trade-level
        # "Net P&L" equals raw price P&L (for example, 78.15 points × 65 = ₹5,079.75).
        backtest_commission_per_unit=0,
    ),
    # The same breakout signals on the NIFTY 50 index, traded with options 300 points in the
    # money: a CE on a long signal, a PE on a short one, sold when the index trade exits. Sized in
    # option lots (65 units = 1 NIFTY lot), up to the shared 1,800-unit position ceiling.
    Deployment(
        key="nifty-options-breakout",
        label="Intraday Breakout · NIFTY options",
        strategy=IntradayBreakoutOptions,
        symbol="NSE:NIFTY50-INDEX",
        timeframe="5m",
        params=IntradayBreakoutOptions.Params(
            lookback=5, max_width=0.4, avg_days=14,
            stop_k=0.25, trail_k=0.40, max_trades_per_day=8,
            entries_from="10:30", entries_until="14:45", square_off="15:15",
            itm_points=300,
        ),
        quantity=65,
        max_order_qty=1_800,
        max_position=1_800,
        max_orders_per_day=20,
        backtest_fixed_quantity=65,  # one lot, like live (the tester trades index points)
    ),
    Deployment(
        key="smart-money-concepts",
        label="Internal CHoCH 25% 3R · 500 ITM Calls",
        strategy=InternalChoch3RCalls,
        symbol="NSE:NIFTY50-INDEX",
        timeframe="5m",
        params=InternalChoch3RCalls.Params(itm_points=500),
        quantity=65,
        max_order_qty=1_800,
        max_position=1_800,
        max_orders_per_day=100,
        backtest_initial_capital=200_000,
        backtest_fixed_quantity=65,
        backtest_commission_per_unit=0,
    ),
    Deployment(
        key="smart-money-concepts-puts",
        label="Internal CHoCH 25% 3R · 500 ITM Puts",
        strategy=InternalChoch3RPuts,
        symbol="NSE:NIFTY50-INDEX",
        timeframe="5m",
        params=InternalChoch3RPuts.Params(itm_points=500),
        quantity=65,
        max_order_qty=1_800,
        max_position=1_800,
        max_orders_per_day=100,
        backtest_initial_capital=200_000,
        backtest_fixed_quantity=65,
        backtest_commission_per_unit=0,
    ),
    Deployment(
        key="sbin-sma-1m",
        strategy=SmaCrossover,
        symbol="NSE:SBIN-EQ",
        timeframe="5m",
        params=SmaCrossover.Params(fast=9, slow=21),
        quantity=1,
        max_order_qty=1_800,
        max_position=1_800,
        max_orders_per_day=10,
    ),
]
