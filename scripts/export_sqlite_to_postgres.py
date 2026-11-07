"""Export the app's SQLite data as PostgreSQL SQL.

The database schema is generated from the SQLAlchemy models, so the export stays
aligned with the application instead of relying on SQLite's dialect-specific dump.
Run from ``autotrade_backend``::

    python scripts/export_sqlite_to_postgres.py autotrade.db /tmp/autotrade.sql
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import db  # noqa: E402


def quote(value: object, column_type: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(column_type, Boolean):
        return "TRUE" if bool(value) else "FALSE"
    if isinstance(value, bytes):
        return "'\\x" + value.hex() + "'::bytea"
    text = str(value).replace("'", "''")
    suffix = ""
    if isinstance(column_type, DateTime):
        suffix = "::timestamp"
    elif isinstance(column_type, Date):
        suffix = "::date"
    elif column_type.__class__.__name__ == "JSON":
        # Validate now so a corrupt SQLite JSON value cannot produce a partial import.
        json.loads(str(value))
        suffix = "::json"
    return f"'{text}'{suffix}"


def export(source: Path, destination: Path) -> None:
    db.import_models()
    dialect = postgresql.dialect()
    connection = sqlite3.connect(source)
    connection.row_factory = sqlite3.Row

    lines = [
        "BEGIN;",
        "SET LOCAL client_min_messages = warning;",
    ]

    for table in db.Base.metadata.sorted_tables:
        lines.append(str(CreateTable(table).compile(dialect=dialect)).rstrip() + ";")
    for table in db.Base.metadata.sorted_tables:
        for index in table.indexes:
            lines.append(str(CreateIndex(index).compile(dialect=dialect)).rstrip() + ";")

    # Alembic manages this table rather than the model metadata.
    lines.append(
        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL "
        "PRIMARY KEY);"
    )

    for table in db.Base.metadata.sorted_tables:
        rows = connection.execute(f'SELECT * FROM "{table.name}"').fetchall()
        if not rows:
            continue
        names = [column.name for column in table.columns]
        quoted_names = ", ".join(f'"{name}"' for name in names)
        for row in rows:
            values = ", ".join(
                quote(row[name], table.c[name].type) for name in names
            )
            lines.append(
                f'INSERT INTO "{table.name}" ({quoted_names}) VALUES ({values});'
            )

    revision = connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone()[0]
    lines.append(
        "INSERT INTO alembic_version (version_num) VALUES "
        f"('{str(revision).replace(chr(39), chr(39) * 2)}');"
    )

    for table in db.Base.metadata.sorted_tables:
        pk = list(table.primary_key.columns)
        if len(pk) == 1 and pk[0].autoincrement is not False and str(pk[0].type) == "INTEGER":
            name = table.name.replace("'", "''")
            column = pk[0].name.replace("'", "''")
            lines.append(
                "SELECT setval(pg_get_serial_sequence('"
                f"{name}', '{column}'), COALESCE((SELECT MAX(\"{pk[0].name}\") "
                f'FROM "{table.name}"), 1), EXISTS (SELECT 1 FROM "{table.name}"));'
            )

    lines.extend(["COMMIT;", ""])
    destination.write_text("\n".join(lines))
    connection.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: export_sqlite_to_postgres.py SOURCE.db OUTPUT.sql")
    export(Path(sys.argv[1]), Path(sys.argv[2]))
