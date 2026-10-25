from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime


class Fill(Base):
    """One execution: part or all of an order traded at a price."""

    __tablename__ = "fills"
    __table_args__ = (Index("ix_fills_symbol_time", "broker", "symbol", "filled_at"),)

    # "<broker>:<order id>:<cumulative filled qty>" — the same update seen twice maps to the
    # same id, so a fill is never counted twice.
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    broker: Mapped[str] = mapped_column(String(32))
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    filled_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Position(Base):
    """A round trip: opened by a fill, closed when fills bring the quantity back to zero."""

    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_lookup", "broker", "symbol", "status"),
                      Index("ix_positions_closed_at", "closed_at"))

    id: Mapped[int] = mapped_column(primary_key=True)
    broker: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(5))  # LONG / SHORT
    status: Mapped[str] = mapped_column(String(6))  # OPEN / CLOSED
    quantity: Mapped[int] = mapped_column(Integer)  # still open
    entry_quantity: Mapped[int] = mapped_column(Integer)
    avg_entry_price: Mapped[float] = mapped_column(Float)
    exit_quantity: Mapped[int] = mapped_column(Integer, default=0)
    avg_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)  # before charges/taxes
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class PositionFill(Base):
    """Which fills built or closed which position. One fill can close one position and open
    the next (e.g. selling 15 while long 10), so this is a link table."""

    __tablename__ = "position_fills"

    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), primary_key=True)
    fill_id: Mapped[str] = mapped_column(ForeignKey("fills.id"), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(5))  # entry / exit
