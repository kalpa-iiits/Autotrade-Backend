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
from app.strategies.builtin.nifty_trailing_reversal import NiftyTrailingReversal
from app.strategies.builtin.nifty_trailing_reversal_options import NiftyTrailingReversalOptions
from app.strategies.builtin.sma_crossover import SmaCrossover
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
        max_order_qty=65,
        max_position=65,
        max_orders_per_day=10,
        backtest_initial_capital=500_000,
        backtest_fixed_quantity=65,
        backtest_commission_per_unit=0,
    ),
    # Direct-instrument version: one 130-unit order reverses a 65-unit future/index position.
    Deployment(
        key="nifty-percentage-reversal",
        label="NIFTY Percentage Trailing Reversal",
        strategy=NiftyTrailingReversal,
        timeframe="5m",
        quantity=65,
        # Capital sizing may target up to 5,000 units. A net reversal order can be twice the
        # position size because it closes the old side and opens the new side in one order.
        max_order_qty=10_000,
        max_position=5_000,
        max_orders_per_day=100,
        backtest_initial_capital=1_000_000,
    ),
    # Option version: NIFTY supplies the signals; a reversal sells the held CE/PE before buying
    # the opposite leg through the runner's depth-priced limit and repricing policy.
    Deployment(
        key="nifty-percentage-reversal-options",
        label="NIFTY Percentage Trailing Reversal · Options",
        strategy=NiftyTrailingReversalOptions,
        symbol="NSE:NIFTY50-INDEX",
        timeframe="5m",
        params=NiftyTrailingReversalOptions.Params(itm_points=300, history_days=7),
        quantity=65,
        max_order_qty=650,
        max_position=650,
        max_orders_per_day=100,
        backtest_initial_capital=1_000_000,
        backtest_fixed_quantity=65,
    ),
    # Intraday breakout (ported from the TradingView script). By default an instance trades one
    # futures lot (65); users can pick another fixed quantity or capital per trade in the app,
    # up to 5,000 units per order and position (room for capital sizing on stocks, e.g. ₹5L of
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
        max_position=5_000,
        max_orders_per_day=20,
        # The reference TradingView report has commission overridden to zero: its trade-level
        # "Net P&L" equals raw price P&L (for example, 78.15 points × 65 = ₹5,079.75).
        backtest_commission_per_unit=0,
    ),
    # The same breakout signals on the NIFTY 50 index, traded with options 300 points in the
    # money: a CE on a long signal, a PE on a short one, sold when the index trade exits. Sized in
    # option lots (65 units = 1 NIFTY lot), up to 10 lots.
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
        max_order_qty=650,
        max_position=650,
        max_orders_per_day=20,
        backtest_fixed_quantity=65,  # one lot, like live (the tester trades index points)
    ),
    Deployment(
        key="sbin-sma-1m",
        strategy=SmaCrossover,
        symbol="NSE:SBIN-EQ",
        timeframe="5m",
        params=SmaCrossover.Params(fast=9, slow=21),
        quantity=1,
        max_position=1,
        max_orders_per_day=10,
    ),
]
