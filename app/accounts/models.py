from datetime import date, datetime

from sqlalchemy import JSON, Date, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow


class BrokerAccount(Base):
    """One user's account at one broker. Every order, fill, position and strategy instance
    belongs to exactly one of these.

    `credentials` and `access_token` hold ciphertext only (app.accounts.vault), bound to this
    row's id. `options` is not secret (product type, paper starting cash).
    """

    __tablename__ = "broker_accounts"
    __table_args__ = (UniqueConstraint("user_id", "label", name="uq_broker_accounts_user_label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    broker: Mapped[str] = mapped_column(String(16))  # paper / fyers / zerodha
    label: Mapped[str] = mapped_column(String(64))
    credentials: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted JSON
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted
    token_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # IST trading day
    # active / deleted (credentials wiped, history kept)
    status: Mapped[str] = mapped_column(String(8), default="active")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
