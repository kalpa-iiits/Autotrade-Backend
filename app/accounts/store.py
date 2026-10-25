"""Broker accounts in the database. The only place credentials and access tokens are
encrypted or decrypted (app.accounts.vault); nothing returned to clients carries them."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import func, select

from app.accounts.models import BrokerAccount
from app.accounts.specs import SPECS, masked_credentials
from app.accounts.vault import VaultError, vault
from app.core import db
from app.core.config import settings

IST = ZoneInfo("Asia/Kolkata")


class AccountError(ValueError):
    """A request about accounts that can't be done (bad credentials, duplicate label, ...)."""


def today_ist() -> date:
    return datetime.now(IST).date()


def _context(account_id: int, field: str) -> str:
    return f"broker_account:{account_id}:{field}"


def _validated(model, data: dict | None, what: str) -> dict:
    try:
        return model(**(data or {})).model_dump(mode="json")
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or what}: {err['msg']}" for err in e.errors())
        raise AccountError(f"Invalid {what}: {problems}") from None


# ---------- reading secrets (server side only) ----------

def credentials_of(account: BrokerAccount) -> dict | None:
    """Decrypted credentials, or None if none are stored. Raises VaultError if unreadable."""
    if not account.credentials:
        return None
    return vault().decrypt_json(account.credentials, _context(account.id, "credentials"))


def token_of(account: BrokerAccount) -> str | None:
    """Today's decrypted access token, or None (none saved, or from an earlier day: broker
    tokens expire daily). Raises VaultError if unreadable."""
    if not account.access_token or account.token_date != today_ist():
        return None
    return vault().decrypt(account.access_token, _context(account.id, "access_token")).decode()


# ---------- queries ----------

async def get_account(account_id: int) -> BrokerAccount | None:
    async with db.session() as s:
        return await s.get(BrokerAccount, account_id)


async def list_accounts(user_id: int | None = None) -> list[BrokerAccount]:
    """Active accounts, of one user or (None) everyone's."""
    query = select(BrokerAccount).where(BrokerAccount.status == "active").order_by(BrokerAccount.id)
    if user_id is not None:
        query = query.where(BrokerAccount.user_id == user_id)
    async with db.session() as s:
        return list(await s.scalars(query))


async def account_ids(user_id: int) -> list[int]:
    """Ids of the user's accounts, deleted ones included (their history is still theirs)."""
    async with db.session() as s:
        return list(await s.scalars(select(BrokerAccount.id).where(BrokerAccount.user_id == user_id)))


# ---------- changes ----------

async def create_account(user_id: int, broker: str, label: str, credentials: dict | None = None,
                         options: dict | None = None) -> BrokerAccount:
    spec = SPECS.get(broker)
    if spec is None:
        raise AccountError(f"Unknown broker {broker!r}; use one of {', '.join(SPECS)}")
    label = label.strip()
    if not 1 <= len(label) <= 64:
        raise AccountError("Label must be 1-64 characters")
    opts = _validated(spec.options, options, "options")
    creds = None
    if spec.credentials is not None:
        creds = _validated(spec.credentials, credentials, "credentials")
        if not vault().configured:
            raise VaultError("Broker credentials can't be stored: the server has no AUTOTRADE_CREDENTIALS_KEY")
    elif credentials:
        raise AccountError(f"{broker} accounts take no credentials")

    async with db.session() as s, s.begin():
        count = await s.scalar(select(func.count()).select_from(BrokerAccount).where(
            BrokerAccount.user_id == user_id, BrokerAccount.status == "active"))
        if count >= settings.max_accounts_per_user:
            raise AccountError(f"At most {settings.max_accounts_per_user} broker accounts per user")
        if await s.scalar(select(BrokerAccount.id).where(
                BrokerAccount.user_id == user_id, BrokerAccount.label == label)):
            raise AccountError(f"You already have an account labelled {label!r}")
        account = BrokerAccount(user_id=user_id, broker=broker, label=label, options=opts, status="active")
        s.add(account)
        await s.flush()  # the id binds the ciphertext to this row
        if creds is not None:
            account.credentials = vault().encrypt_json(creds, _context(account.id, "credentials"))
    return account


async def update_account(account_id: int, label: str | None = None, credentials: dict | None = None,
                         options: dict | None = None) -> BrokerAccount:
    """Change the label, options and/or credentials. Credential fields that are left out keep
    their stored value. New credentials drop today's access token (it belonged to the old app)."""
    async with db.session() as s, s.begin():
        account = await s.get(BrokerAccount, account_id)
        if account is None or account.status != "active":
            raise AccountError("No such account")
        spec = SPECS[account.broker]
        if label is not None:
            label = label.strip()
            if not 1 <= len(label) <= 64:
                raise AccountError("Label must be 1-64 characters")
            if label != account.label and await s.scalar(select(BrokerAccount.id).where(
                    BrokerAccount.user_id == account.user_id, BrokerAccount.label == label)):
                raise AccountError(f"You already have an account labelled {label!r}")
            account.label = label
        if options is not None:
            account.options = _validated(spec.options, {**account.options, **options}, "options")
        if credentials:
            if spec.credentials is None:
                raise AccountError(f"{account.broker} accounts take no credentials")
            merged = {**(credentials_of(account) or {}), **{k: v for k, v in credentials.items() if v}}
            creds = _validated(spec.credentials, merged, "credentials")
            account.credentials = vault().encrypt_json(creds, _context(account.id, "credentials"))
            account.access_token = account.token_date = None
    return account


async def delete_account(account_id: int) -> None:
    """Wipe the credentials and token; keep the row so the account's history stays."""
    async with db.session() as s, s.begin():
        account = await s.get(BrokerAccount, account_id)
        if account is not None:
            account.status = "deleted"
            account.credentials = account.access_token = account.token_date = None
            account.label = f"{account.label[:48]} (deleted #{account.id})"  # frees the label


async def save_token(account_id: int, access_token: str) -> None:
    async with db.session() as s, s.begin():
        account = await s.get(BrokerAccount, account_id)
        account.access_token = vault().encrypt(access_token.encode(), _context(account_id, "access_token"))
        account.token_date = today_ist()


async def clear_token(account_id: int) -> None:
    async with db.session() as s, s.begin():
        account = await s.get(BrokerAccount, account_id)
        if account is not None:
            account.access_token = account.token_date = None


async def reencrypt_all() -> int:
    """Re-encrypt every stored secret with the current key (after a key rotation)."""
    v, done = vault(), 0
    async with db.session() as s, s.begin():
        for account in await s.scalars(select(BrokerAccount)):
            for field in ("credentials", "access_token"):
                token = getattr(account, field)
                if token and not v.is_current(token):
                    ctx = _context(account.id, field)
                    setattr(account, field, v.encrypt(v.decrypt(token, ctx), ctx))
                    done += 1
    return done


# ---------- what clients see ----------

def public_view(account: BrokerAccount) -> dict:
    """The account as the API shows it: no secrets. Identifiers (client id / API key) masked."""
    spec = SPECS[account.broker]
    try:
        creds, unreadable = credentials_of(account), None
    except VaultError as e:
        creds, unreadable = None, str(e)
    try:
        logged_in_today = token_of(account) is not None
    except VaultError:
        logged_in_today = False
    return {
        "id": account.id, "broker": account.broker, "label": account.label,
        "options": account.options,
        "credentials": masked_credentials(account.broker, creds) if not unreadable else None,
        "credentials_error": unreadable,
        "needs_daily_login": spec.daily_login,
        "logged_in_today": logged_in_today if spec.daily_login else None,
        "created_at": account.created_at.isoformat(),
    }
