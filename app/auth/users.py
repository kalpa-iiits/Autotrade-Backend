"""User accounts, plus a CLI to manage them:

    python -m app.auth.users create <username>
    python -m app.auth.users set-password <username>
    python -m app.auth.users disable <username>
    python -m app.auth.users enable <username>
    python -m app.auth.users list
    python -m app.auth.users token <username> [name]   # prints a new personal API token
"""

import asyncio
import hashlib
import re

from sqlalchemy import func, select

from app.auth.models import User
from app.auth.passwords import DUMMY_HASH, hash_password, verify_password
from app.core import db
from app.core.db import utcnow

MIN_PASSWORD_LENGTH = 10
# Users sign up with their email as their username. Users created before that (or with the CLI)
# may have a plain username; they still sign in with it.
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserError(ValueError):
    pass


def password_fingerprint(user: User) -> str:
    """Stored in the session; changing a user's password logs out their existing sessions."""
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def login_name(username: str) -> str:
    """The stored username to look up: emails are compared lowercase, plain usernames as typed."""
    username = username.strip()
    return username.lower() if "@" in username else username


def normalize_email(email: str) -> str:
    """A sign-up email, lowercase; UserError if it doesn't look like one."""
    email = email.strip().lower()
    if not EMAIL.match(email) or len(email) > 64:
        raise UserError("Enter a valid email address.")
    return email


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"Use at least {MIN_PASSWORD_LENGTH} characters.")


async def get_user(user_id: int) -> User | None:
    async with db.session() as s:
        return await s.get(User, user_id)


async def count_users() -> int:
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(User))


async def authenticate(username: str, password: str) -> User | None:
    async with db.session() as s, s.begin():
        user = await s.scalar(select(User).where(User.username == login_name(username)))
        # Always run the hash check so a wrong username takes as long as a wrong password.
        ok = await asyncio.to_thread(verify_password, password, user.password_hash if user else DUMMY_HASH)
        if not (user and ok and user.is_active):
            return None
        user.last_login_at = utcnow()
        return user


async def check_password(username: str, password: str) -> bool:
    """Password check without touching last_login_at (for re-authentication)."""
    async with db.session() as s:
        user = await s.scalar(select(User).where(User.username == username))
    return await asyncio.to_thread(verify_password, password, user.password_hash if user else DUMMY_HASH) \
        and user is not None


async def create_user(username: str, password: str) -> User:
    username = username.strip()
    if not username or len(username) > 64:
        raise UserError("Username must be 1-64 characters.")
    _check_password(password)
    async with db.session() as s, s.begin():
        if await s.scalar(select(User).where(User.username == username)):
            raise UserError(f"User {username!r} already exists.")
        user = User(username=username, password_hash=hash_password(password))
        s.add(user)
    return user


async def _update(username: str, **fields) -> None:
    async with db.session() as s, s.begin():
        user = await s.scalar(select(User).where(User.username == username))
        if user is None:
            raise UserError(f"No user {username!r}.")
        for name, value in fields.items():
            setattr(user, name, value)


async def set_password(username: str, password: str) -> None:
    _check_password(password)
    await _update(username, password_hash=hash_password(password))


async def set_active(username: str, active: bool) -> None:
    await _update(username, is_active=active)


async def list_users() -> list[User]:
    async with db.session() as s:
        return list(await s.scalars(select(User).order_by(User.username)))


def _main() -> None:
    import argparse
    import asyncio
    import getpass

    from app.core.config import settings

    parser = argparse.ArgumentParser(prog="python -m app.auth.users")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "set-password", "disable", "enable"):
        sub.add_parser(name).add_argument("username")
    sub.add_parser("list")
    tok = sub.add_parser("token")
    tok.add_argument("username")
    tok.add_argument("name", nargs="?", default="cli")
    args = parser.parse_args()

    def ask_password() -> str:
        first = getpass.getpass("Password: ")
        if getpass.getpass("Repeat password: ") != first:
            raise UserError("Passwords did not match.")
        return first

    async def run() -> str:
        await db.init(settings.database_url)
        try:
            if args.command == "create":
                await create_user(args.username, ask_password())
                return f"Created user {args.username!r}."
            if args.command == "set-password":
                await set_password(args.username, ask_password())
                return f"Password changed; {args.username!r} is signed out everywhere."
            if args.command == "token":
                from app.auth.tokens import create_token

                async with db.session() as s:
                    user = await s.scalar(select(User).where(User.username == args.username))
                if user is None:
                    raise UserError(f"No user {args.username!r}.")
                _, token = await create_token(user.id, args.name)
                return f"API token for {args.username!r} (shown once, keep it secret):\n{token}"
            if args.command in ("disable", "enable"):
                await set_active(args.username, args.command == "enable")
                return f"{args.username!r} {args.command}d."
            rows = [f"{u.username:<20} {'active' if u.is_active else 'disabled':<9} "
                    f"last login: {u.last_login_at or 'never'}" for u in await list_users()]
            return "\n".join(rows) or "No users yet. Create one with: python -m app.auth.users create <name>"
        finally:
            await db.close()

    try:
        print(asyncio.run(run()))
    except UserError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    _main()
