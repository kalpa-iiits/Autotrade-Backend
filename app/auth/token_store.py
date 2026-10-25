"""Keeps today's broker access token in the database so a restart doesn't force a re-login."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.auth.models import BrokerToken
from app.core import db

IST = ZoneInfo("Asia/Kolkata")


def _today() -> date:
    return datetime.now(IST).date()


class TokenStore:
    def __init__(self, broker: str) -> None:
        self.broker = broker

    async def load(self) -> str | None:
        """Today's token, or None if there is no login yet today (broker tokens expire daily)."""
        async with db.session() as s:
            row = await s.get(BrokerToken, self.broker)
        if row is None or row.token_date != _today():
            return None
        return row.access_token

    async def save(self, access_token: str) -> None:
        async with db.session() as s, s.begin():
            await s.merge(BrokerToken(broker=self.broker, access_token=access_token, token_date=_today()))

    async def clear(self) -> None:
        async with db.session() as s, s.begin():
            if row := await s.get(BrokerToken, self.broker):
                await s.delete(row)
