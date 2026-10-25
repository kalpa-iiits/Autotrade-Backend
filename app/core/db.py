"""Database setup. SQLite for now; any SQLAlchemy async URL works (e.g. postgresql+asyncpg://...).

The schema is managed by Alembic (migrations/). Startup applies any pending migrations.
After changing a model, write a migration:
    alembic revision --autogenerate -m "what changed"
review the generated file in migrations/versions/, then restart the app.
"""

import logging
import os
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from sqlalchemy import BigInteger, DateTime, event, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
# First migration: the schema as it was when Alembic was introduced. Databases created before
# that (by create_all) are stamped with it instead of having it applied.
BASELINE_REVISION = "14fe2d4fd8bd"


class UTCDateTime(TypeDecorator):
    """Stores UTC; always hands back timezone-aware datetimes (SQLite drops the tzinfo)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


class Money(TypeDecorator):
    """Exact prices and P&L: stored as a whole number of 1/10000ths (BIGINT, exact on SQLite
    and Postgres alike), handed back as Decimal. Accepts Decimal, int, float or str."""

    impl = BigInteger
    cache_ok = True
    SCALE = Decimal("0.0001")

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return int(to_decimal(value) / self.SCALE)

    def process_result_value(self, value, dialect):
        return None if value is None else (Decimal(value) * self.SCALE).quantize(self.SCALE)


def to_decimal(value) -> Decimal:
    """A price as an exact Decimal with 4 places. Floats go through str(), so 101.1 stays
    101.1 instead of 101.0999999..."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return d.quantize(Money.SCALE, rounding=ROUND_HALF_EVEN)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


async def init(url: str) -> None:
    """Connect and create any missing tables. Called once at startup."""
    global _engine, _sessionmaker
    await close()
    _engine = create_async_engine(url)
    if _engine.dialect.name == "sqlite":
        @event.listens_for(_engine.sync_engine, "connect")
        def _sqlite_pragmas(conn, _record):
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    import_models()
    async with _engine.begin() as conn:
        await conn.run_sync(_migrate)

    path = make_url(url).database
    if _engine.dialect.name == "sqlite" and path and path != ":memory:" and os.path.exists(path):
        os.chmod(path, 0o600)  # holds password hashes and broker tokens
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)


def import_models() -> None:
    """Import every app's models so their tables are registered on Base.metadata."""
    import app.auth.models  # noqa: F401
    import app.orders.models  # noqa: F401
    import app.strategies.models  # noqa: F401
    import app.trades.models  # noqa: F401


def alembic_config(connection=None):
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def _migrate(connection) -> None:
    """Bring the schema to the latest migration (runs on the app's own connection)."""
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    cfg = alembic_config(connection)
    tables = set(inspect(connection).get_table_names())
    current = MigrationContext.configure(connection).get_current_revision()
    # No recorded revision (alembic_version missing or empty) but the app's tables exist.
    if current is None and "users" in tables:
        log.info("Adopting a database created before migrations: stamping %s", BASELINE_REVISION)
        command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, "head")

    # Catch "changed a model but didn't write a migration" early, with a clear message.
    diffs = compare_metadata(MigrationContext.configure(connection, opts={"compare_type": False}),
                             Base.metadata)
    if diffs:
        log.warning(
            "The models don't match the database schema (%d difference(s), e.g. %s). "
            "Write a migration: alembic revision --autogenerate -m \"...\"", len(diffs), diffs[0],
        )


async def close() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = _sessionmaker = None


def session() -> AsyncSession:
    """`async with db.session() as s: ...`"""
    if _sessionmaker is None:
        raise RuntimeError("Database not initialised; call db.init() first")
    return _sessionmaker()
