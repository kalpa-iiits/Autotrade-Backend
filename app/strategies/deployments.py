"""The strategies this app runs. Edit this file to add, change or remove one.

Each Deployment is a strategy with its parameters, size, limits and mode. Which symbol and
timeframe it runs on is chosen in the app, where you can also add more instances of it (e.g.
the same breakout on NIFTY and BANKNIFTY). `symbol` and `timeframe` here are only the defaults
for the first instance, created the first time the server sees the deployment.

Changes take effect when the server restarts (with --reload, on save) and apply to every
instance; running instances come back stopped after a change. Removing a deployment here marks
its instances "removed" and keeps their signal history.

New deployments start in mode="signal" (no orders). Switch to mode="live" only after
dry-running it; live orders also need AUTOTRADE_STRATEGIES_LIVE=true in .env.
"""

from app.strategies.builtin.intraday_breakout import IntradayBreakout
from app.strategies.builtin.sma_crossover import SmaCrossover
from app.strategies.deployment import Deployment

DEPLOYMENTS: list[Deployment] = [
    # NIFTY 1-minute breakout (ported from the TradingView script). Trades one futures lot
    # (65). Futures expire monthly (last Tuesday): roll the instance's symbol to the next
    # contract in the app before expiry, e.g. NSE:NIFTY26NOVFUT -> NSE:NIFTY26DECFUT.
    # 8 trades/day = up to 16 orders plus square-off, so the order cap is 20.
    Deployment(
        key="nifty-1m-breakout",
        strategy=IntradayBreakout,
        symbol="NSE:NIFTY26NOVFUT",
        timeframe="1m",
        params=IntradayBreakout.Params(
            lookback=5, max_width=0.4, avg_days=14,
            stop_k=0.25, trail_k=0.40, max_trades_per_day=8,
            entries_from="10:30", entries_until="14:45", square_off="15:15",
        ),
        quantity=65,
        max_position=65,
        max_orders_per_day=20,
        mode="signal",
    ),
    Deployment(
        key="sbin-sma-1m",
        strategy=SmaCrossover,
        symbol="NSE:SBIN-EQ",
        timeframe="1m",
        params=SmaCrossover.Params(fast=9, slow=21),
        quantity=1,
        max_position=1,
        max_orders_per_day=10,
        mode="signal",
    ),
]
