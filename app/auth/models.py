from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String
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


class ApiToken(Base):
    """A user's token for scripts and bots (Authorization: Bearer ...). Only a SHA-256 hash is
    stored; the token itself is shown once, when it is created."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(12))  # first characters, to recognise it in a list
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class LoginAttempt(Base):
    """Every sign-in attempt: drives the lockout and doubles as an audit log."""

    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_client_time", "client", "attempted_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client: Mapped[str] = mapped_column(String(64))
    username: Mapped[str] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean)
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
