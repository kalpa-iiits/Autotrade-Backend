from datetime import date, datetime

from sqlalchemy import Boolean, Date, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class BrokerToken(Base):
    """Today's access token per broker (Fyers/Zerodha tokens expire daily)."""

    __tablename__ = "broker_tokens"

    broker: Mapped[str] = mapped_column(String(32), primary_key=True)
    access_token: Mapped[str] = mapped_column(String(4096))
    token_date: Mapped[date] = mapped_column(Date)  # trading day (IST) the token is valid for
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class LoginAttempt(Base):
    """Every sign-in attempt: drives the lockout and doubles as an audit log."""

    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_client_time", "client", "attempted_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client: Mapped[str] = mapped_column(String(64))
    username: Mapped[str] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean)
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
