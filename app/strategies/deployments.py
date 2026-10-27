"""The strategies this app runs. Edit this file to add, change or remove one.

Each Deployment is a strategy with its parameters, size, limits and mode. Which symbol and
timeframe it runs on is chosen in the app, where you can also add more instances of it (e.g.
the same breakout on NIFTY and BANKNIFTY). `symbol`, when present, is only a suggestion in the
deployment form; `timeframe` is its initial selection.

Changes take effect when the server restarts (with --reload, on save) and apply to every
instance; running instances come back stopped after a change. Removing a deployment here marks
its instances "removed" and keeps their signal history.

New deployments start in mode="signal" (no orders). Switch to mode="live" only after
dry-running it; live orders also need AUTOTRADE_STRATEGIES_LIVE_ENABLED=true in .env.
"""

from app.strategies.builtin.intraday_breakout import IntradayBreakout
from app.strategies.builtin.intraday_breakout_options import IntradayBreakoutOptions
from app.strategies.builtin.sma_crossover import SmaCrossover
from app.strategies.deployment import Deployment

DEPLOYMENTS: list[Deployment] = [
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
        mode="live",  # real broker orders (also needs AUTOTRADE_STRATEGIES_LIVE_ENABLED=true)
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
        mode="signal",
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
        mode="signal",
    ),
]
