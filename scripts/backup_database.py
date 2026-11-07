"""Create a verified, timestamped database backup for a production deployment."""

import argparse
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.config import settings


def _postgres_args(url):
    args = ["pg_dump", "--format=custom", "--no-owner"]
    if url.host:
        args += ["--host", url.host]
    if url.port:
        args += ["--port", str(url.port)]
    if url.username:
        args += ["--username", url.username]
    args += ["--dbname", url.database]
    return args


def backup(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    url = make_url(settings.database_url)
    if url.get_backend_name() == "postgresql":
        if shutil.which("pg_dump") is None:
            raise RuntimeError("pg_dump is required for production backups")
        destination = output_dir / f"autotrade-{stamp}.dump"
        temporary = destination.with_suffix(".dump.tmp")
        env = os.environ.copy()
        if url.password:
            env["PGPASSWORD"] = url.password
        env.setdefault("PGSSLMODE", "require")
        subprocess.run(_postgres_args(url) + ["--file", str(temporary)], check=True, env=env)
    elif url.get_backend_name() == "sqlite":
        source = Path(url.database).resolve()
        destination = output_dir / f"autotrade-{stamp}.sqlite3"
        temporary = destination.with_suffix(".sqlite3.tmp")
        with sqlite3.connect(source) as src, sqlite3.connect(temporary) as dst:
            src.backup(dst)
    else:
        raise RuntimeError(f"Unsupported database backend: {url.get_backend_name()}")
    if not temporary.exists() or temporary.stat().st_size == 0:
        raise RuntimeError("database backup is empty")
    temporary.chmod(0o600)
    temporary.replace(destination)
    print(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    backup(args.output_dir)
