"""Personal API tokens for scripts and bots. Each belongs to one user and acts as that user.
Only the SHA-256 hash is stored, so a database leak doesn't hand out working tokens."""

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select

from app.auth.models import ApiToken, User
from app.core import db
from app.core.db import utcnow

PREFIX = "atk_"
TOUCH_EVERY = timedelta(minutes=5)  # how stale last_used_at may get (avoids a write per request)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_token(user_id: int, name: str, token: str | None = None) -> tuple[ApiToken, str]:
    """Returns the row and the token. The token is not stored and can't be shown again."""
    token = token or PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(user_id=user_id, name=name.strip()[:64] or "token", token_hash=_hash(token),
                   prefix=token[:12])
    async with db.session() as s, s.begin():
        s.add(row)
    return row, token


async def user_for_token(token: str | None) -> User | None:
    """The active user a token belongs to, or None."""
    if not token:
        return None
    async with db.session() as s, s.begin():
        row = await s.scalar(select(ApiToken).where(ApiToken.token_hash == _hash(token)))
        if row is None:
            return None
        user = await s.get(User, row.user_id)
        if user is None or not user.is_active:
            return None
        now = utcnow()
        if row.last_used_at is None or now - row.last_used_at > TOUCH_EVERY:
            row.last_used_at = now
        return user


async def list_tokens(user_id: int) -> list[ApiToken]:
    async with db.session() as s:
        return list(await s.scalars(select(ApiToken).where(ApiToken.user_id == user_id).order_by(ApiToken.id)))


async def revoke_token(user_id: int, token_id: int) -> bool:
    async with db.session() as s, s.begin():
        row = await s.get(ApiToken, token_id)
        if row is None or row.user_id != user_id:
            return False
        await s.delete(row)
        return True


def token_dict(row: ApiToken) -> dict:
    return {"id": row.id, "name": row.name, "prefix": row.prefix, "created_at": row.created_at.isoformat(),
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None}
