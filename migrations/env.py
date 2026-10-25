"""Alembic environment.

Two ways in:
  - CLI (`alembic upgrade head`, `alembic revision --autogenerate -m "..."`): connects to
    settings.database_url itself.
  - The app at startup (app.core.db.init): passes its own open connection in
    config.attributes["connection"], so migrations run inside the app's event loop.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.core.config import settings
from app.core.db import Base, UTCDateTime, import_models

config = context.config
import_models()
target_metadata = Base.metadata


def _render_item(type_, obj, autogen_context):
    """Write our UTCDateTime as a plain DateTime in migration files (same column type)."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"  # migration files already import sqlalchemy as sa
    return False


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite can't ALTER most things; batch mode rebuilds the table
        render_item=_render_item,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=settings.database_url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(settings.database_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
elif (connection := config.attributes.get("connection")) is not None:
    do_run_migrations(connection)  # called from the running app
else:
    if config.config_file_name is not None:
        fileConfig(config.config_file_name)  # CLI only: don't reset the app's logging
    asyncio.run(run_async_migrations())
