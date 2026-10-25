from datetime import timedelta

from sqlalchemy import delete, func, select

from app.auth.models import LoginAttempt
from app.core import db
from app.core.db import utcnow


class LoginThrottle:
    """Locks a client out after too many failed logins within a time window.

    Attempts live in the login_attempts table, so lockouts survive restarts.
    """

    def __init__(self, max_failures: int = 5, window: float = 300.0) -> None:
        self.max_failures = max_failures
        self.window = timedelta(seconds=window)

    async def retry_after(self, client: str) -> int:
        """Seconds until this client may try again; 0 if not locked out."""
        cutoff = utcnow() - self.window
        async with db.session() as s:
            # A successful login resets the count, so only look after the latest success.
            last_ok = await s.scalar(
                select(func.max(LoginAttempt.attempted_at))
                .where(LoginAttempt.client == client, LoginAttempt.success.is_(True))
            )
            since = max(cutoff, last_ok) if last_ok else cutoff
            failures = (await s.scalars(
                select(LoginAttempt.attempted_at)
                .where(LoginAttempt.client == client, LoginAttempt.success.is_(False),
                       LoginAttempt.attempted_at > since)
                .order_by(LoginAttempt.attempted_at)
            )).all()
        if len(failures) < self.max_failures:
            return 0
        # Unlocks once the oldest failure that still counts falls out of the window.
        oldest = failures[-self.max_failures]
        return max(1, int((oldest + self.window - utcnow()).total_seconds()))

    async def record(self, client: str, username: str, success: bool) -> None:
        async with db.session() as s, s.begin():
            s.add(LoginAttempt(client=client, username=username[:64], success=success))

    async def prune(self, keep_days: int = 90) -> None:
        async with db.session() as s, s.begin():
            await s.execute(
                delete(LoginAttempt).where(LoginAttempt.attempted_at < utcnow() - timedelta(days=keep_days))
            )
