import asyncio
import logging
import sqlite3

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Column, Integer, Table, create_engine, inspect

from app.core import db
from app.core.db import BASELINE_REVISION, Base, alembic_config, import_models


def init(path):
    async def go():
        await db.init(f"sqlite+aiosqlite:///{path}")
        await db.close()
    asyncio.run(go())


def version(path):
    return sqlite3.connect(path).execute("select version_num from alembic_version").fetchone()


def tables(path):
    rows = sqlite3.connect(path).execute("select name from sqlite_master where type='table'")
    return {r[0] for r in rows}


def legacy_db(path):
    """A database as old code built it (create_all, no Alembic): the baseline schema without
    an alembic_version row."""
    with create_engine(f"sqlite:///{path}").begin() as conn:
        command.upgrade(alembic_config(conn), BASELINE_REVISION)
        conn.exec_driver_sql("drop table alembic_version")


def diffs_against_models(path):
    import_models()
    with create_engine(f"sqlite:///{path}").connect() as conn:
        return compare_metadata(MigrationContext.configure(conn), Base.metadata)


def test_fresh_database_matches_models(tmp_path):
    path = tmp_path / "fresh.db"
    init(path)
    assert {"users", "orders", "positions", "strategy_instances", "alembic_version"} <= tables(path)
    assert version(path) is not None
    assert diffs_against_models(path) == []  # every model change has a migration


def test_adopts_database_created_before_migrations(tmp_path, caplog):
    path = tmp_path / "legacy.db"
    legacy_db(path)
    with caplog.at_level(logging.INFO, logger="app.core.db"):
        init(path)
    assert version(path) is not None
    assert "Adopting" in caplog.text
    assert diffs_against_models(path) == []  # and later migrations were applied on top


def test_adopts_when_version_table_is_empty(tmp_path):
    """Regression: a failed first run left an empty alembic_version table."""
    path = tmp_path / "half.db"
    legacy_db(path)
    with create_engine(f"sqlite:///{path}").begin() as conn:
        conn.exec_driver_sql("create table alembic_version (version_num varchar(32) not null)")
    init(path)
    assert version(path) is not None
    assert diffs_against_models(path) == []


def test_downgrade_and_upgrade(tmp_path):
    path = tmp_path / "roundtrip.db"
    init(path)
    with create_engine(f"sqlite:///{path}").begin() as conn:
        cfg = alembic_config(conn)
        command.downgrade(cfg, "base")
        assert set(inspect(conn).get_table_names()) <= {"alembic_version"}  # same transaction
        command.upgrade(cfg, "head")
    assert diffs_against_models(path) == []


def test_warns_when_a_model_has_no_migration(tmp_path, caplog):
    init(tmp_path / "a.db")
    extra = Table("not_migrated", Base.metadata, Column("id", Integer, primary_key=True))
    try:
        with caplog.at_level(logging.WARNING, logger="app.core.db"):
            init(tmp_path / "a.db")
        assert "alembic revision --autogenerate" in caplog.text
    finally:
        Base.metadata.remove(extra)


def test_baseline_is_the_first_revision():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_revision(BASELINE_REVISION).down_revision is None


def test_instances_migration_keeps_existing_rows(tmp_path):
    """Rows from before symbol/timeframe-per-instance become 1m instances of their deployment,
    on an imported broker account; a running one comes back stopped (check it, then start)."""
    path = tmp_path / "old.db"
    with create_engine(f"sqlite:///{path}").begin() as conn:
        command.upgrade(alembic_config(conn), BASELINE_REVISION)
        conn.exec_driver_sql(
            "insert into strategy_instances (key, strategy, label, symbol, params, mode, status, quantity, "
            "max_order_qty, max_position, max_orders_per_day, created_at, updated_at) values "
            "('brk', 'intraday_breakout', 'brk', 'NSE:X', '{}', 'signal', 'running', 1, 1, 1, 1, "
            "'2026-01-01', '2026-01-01')")
    init(path)
    row = sqlite3.connect(path).execute(
        "select deployment_key, symbol, timeframe, status, account_id from strategy_instances").fetchone()
    assert row == ("brk", "NSE:X", "1m", "stopped", 1)
    account = sqlite3.connect(path).execute("select user_id, broker, label from broker_accounts").fetchone()
    assert account[1:] == ("paper", "Imported (paper)")  # owned by a disabled "legacy" user (no users)
    assert diffs_against_models(path) == []
