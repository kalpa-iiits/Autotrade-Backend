from datetime import datetime

from sqlalchemy import Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime


class OrderRecord(Base):
    """Latest known state of every order the app has seen, across all brokers."""

    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_updated_at", "updated_at"),
                      Index("ix_orders_client_order_id", "broker", "client_order_id"))

    broker: Mapped[str] = mapped_column(String(32), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # broker's order id
    # Ours, sent as the broker's order tag; placing again with it returns this order.
    client_order_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    order_type: Mapped[str] = mapped_column(String(16))
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class OrderEvent(Base):
    """Every change to an order (placed, partly filled, filled, cancelled, ...), in order."""

    __tablename__ = "order_events"
    __table_args__ = (Index("ix_order_events_order", "broker", "order_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    broker: Mapped[str] = mapped_column(String(32))
    order_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    filled_quantity: Mapped[int] = mapped_column(Integer)
    filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime)
