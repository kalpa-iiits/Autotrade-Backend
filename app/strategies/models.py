from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Money, UTCDateTime, utcnow


class StrategyInstance(Base):
    """One run of a deployment from app/strategies/deployments.py on one broker account,
    symbol and timeframe.

    Account, symbol, timeframe and position sizing are chosen in the app by the account's owner.
    Everything else (strategy, params, default size, limits, mode) is copied from code at startup. Status and history
    live here.
    """

    __tablename__ = "strategy_instances"
    __table_args__ = (
        UniqueConstraint("account_id", "deployment_key", "symbol", "timeframe", name="uq_strategy_instance_target"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("broker_accounts.id"), index=True)
    deployment_key: Mapped[str] = mapped_column(String(64), index=True)  # Deployment.key
    strategy: Mapped[str] = mapped_column(String(64))  # Strategy.name
    label: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(8))  # key of TIMEFRAMES, e.g. "1m"
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    # "signal" records what it would trade; "live" places real orders (also needs
    # AUTOTRADE_STRATEGIES_LIVE_ENABLED=true).
    mode: Mapped[str] = mapped_column(String(8), default="signal")
    # stopped / running / error / removed (deployment no longer in code) / archived (removed in app)
    status: Mapped[str] = mapped_column(String(8), default="stopped")
    quantity: Mapped[int] = mapped_column(Integer)  # default size of buy()/sell()
    # Position sizing, chosen by the user: "fixed" trades `fixed_quantity` units (None = the
    # code's quantity); "capital" buys as many units as `capital_per_trade` pays for at entry.
    # Either way max_order_qty and max_position (from code) cap it.
    size_mode: Mapped[str] = mapped_column(String(8), default="fixed", server_default="fixed")
    fixed_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capital_per_trade: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    max_order_qty: Mapped[int] = mapped_column(Integer)
    max_position: Mapped[int] = mapped_column(Integer)  # absolute net quantity
    max_orders_per_day: Mapped[int] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class StrategySignal(Base):
    """Every trade a strategy asked for, and what happened to it."""

    __tablename__ = "strategy_signals"
    __table_args__ = (Index("ix_strategy_signals_instance", "instance_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("strategy_instances.id", ondelete="CASCADE"))
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    symbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)  # last price when signalled
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mode: Mapped[str] = mapped_column(String(8))
    # signal_only / placed / rejected (by broker) / blocked (by a risk check)
    outcome: Mapped[str] = mapped_column(String(12))
    message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    broker: Mapped[str | None] = mapped_column(String(32), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
