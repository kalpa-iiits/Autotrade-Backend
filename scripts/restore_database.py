"""Restore a deployment backup. Intended for the automated failed-release rollback."""

import argparse
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.config import settings
from scripts.backup_database import _postgres_args


def _is_schema_entry(line: str) -> bool:
    """TOC lines for the schema itself (SCHEMA, COMMENT ON SCHEMA, its ACL), e.g.
    '3; 2615 2200 SCHEMA - public pg_database_owner'."""
    fields = line.split(";", 1)[-1].split()
    return not line.startswith(";") and len(fields) >= 3 and (
        fields[2] == "SCHEMA" or (fields[2] in ("COMMENT", "ACL") and fields[3:5] == ["-", "SCHEMA"]))


def restore(source: Path, confirmed_database: str) -> None:
    url = make_url(settings.database_url)
    if confirmed_database != url.database:
        raise RuntimeError("--confirm-database must exactly match the configured database name")
    if not source.is_file() or source.stat().st_size == 0:
        raise RuntimeError("backup does not exist or is empty")
    if url.get_backend_name() == "postgresql":
        if shutil.which("pg_restore") is None:
            raise RuntimeError("pg_restore is required for rollback")
        env = os.environ.copy()
        if url.password:
            env["PGPASSWORD"] = url.password
        env.setdefault("PGSSLMODE", "require")
        command = [arg.replace("pg_dump", "pg_restore") for arg in _postgres_args(url)]
        command.remove("--format=custom")
        # Restore the app's objects only, never the schema itself: with --clean the schema
        # entry would DROP SCHEMA public, which on managed Postgres also drops provider grants.
        toc = subprocess.run(["pg_restore", "--list", str(source)], check=True, env=env,
                             capture_output=True, text=True).stdout
        entries = [line for line in toc.splitlines() if not _is_schema_entry(line)]
        with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as listing:
            listing.write("\n".join(entries) + "\n")
        try:
            command += ["--clean", "--if-exists", "--exit-on-error",
                        "--use-list", listing.name, str(source)]
            subprocess.run(command, check=True, env=env)
        finally:
            os.unlink(listing.name)
    elif url.get_backend_name() == "sqlite":
        destination = Path(url.database).resolve()
        temporary = destination.with_suffix(destination.suffix + ".restore")
        with sqlite3.connect(source) as src, sqlite3.connect(temporary) as dst:
            src.backup(dst)
        temporary.replace(destination)
    else:
        raise RuntimeError(f"Unsupported database backend: {url.get_backend_name()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("backup", type=Path)
    parser.add_argument("--confirm-database", required=True)
    args = parser.parse_args()
    restore(args.backup, args.confirm_database)
