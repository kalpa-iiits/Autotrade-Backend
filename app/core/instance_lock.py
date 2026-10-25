"""One server process at a time.

Strategies, the broker connection and order de-duplication live in the process, so two
processes (e.g. `uvicorn --workers 2`, or a second server started by mistake) would run every
strategy twice and place every order twice. The server holds an exclusive lock on a file while
it runs; a second one fails to start with a clear message. The OS releases the lock if the
process dies, so a crash never leaves it stuck.

This protects one machine. Servers on different machines sharing one Postgres database would
need a database lock (e.g. pg_advisory_lock) instead.
"""

import os
from pathlib import Path

from sqlalchemy.engine import make_url

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None


class AlreadyRunning(RuntimeError):
    pass


def lock_path(lock_file: str, database_url: str) -> Path:
    if lock_file:
        return Path(lock_file)
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        return Path(url.database).with_suffix(".lock")
    return Path("autotrade.lock")


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        if fcntl is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunning(
                f"Another AutoTrade server is already running (it holds {self.path}). Run only "
                "one process: no --workers > 1, no second server on this database."
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
