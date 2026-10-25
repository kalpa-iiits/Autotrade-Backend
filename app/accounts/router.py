"""Broker accounts: each user adds, edits and removes their own (paper, Fyers, Zerodha).

Credentials are write-only: they go in with POST/PATCH and are stored encrypted; responses
only say which fields are set (client id / API key masked). Daily broker login for an account
is in app.auth.router (GET /api/accounts/{id}/broker-login).
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import update

from app.accounts import store
from app.accounts.dependencies import owned_account
from app.accounts.models import BrokerAccount
from app.accounts.specs import SPECS, BrokerName
from app.accounts.store import AccountError
from app.accounts.vault import VaultError
from app.auth.dependencies import require_user
from app.auth.models import User
from app.core import db
from app.strategies.models import StrategyInstance

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountCreate(BaseModel):
    broker: BrokerName
    label: str = Field(min_length=1, max_length=64)
    # fyers: {client_id, secret_key}; zerodha: {api_key, api_secret}; paper: none.
    credentials: dict[str, str] | None = None
    # fyers: {product_type}; zerodha: {product}; paper: {starting_cash}.
    options: dict | None = None


class AccountUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=64)
    # Only the fields given change; the rest keep their stored values.
    credentials: dict[str, str] | None = None
    options: dict | None = None


class AccessToken(BaseModel):
    access_token: str = Field(min_length=1, max_length=4096)


def _errors(e: Exception) -> HTTPException:
    if isinstance(e, VaultError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))


def _view(request: Request, account: BrokerAccount) -> dict:
    runtime = request.app.state.accounts.get(account.id)
    return store.public_view(account) | {"connection": runtime.health() if runtime else None}


@router.get("/brokers")
async def supported_brokers(user: User = Depends(require_user)):
    """What each broker needs: credential fields (secret) and options, for an "add account" form."""
    return {
        name: {
            "credentials": list(spec.credentials.model_fields) if spec.credentials else [],
            "options": spec.options.model_json_schema()["properties"],
            "daily_login": spec.daily_login,
        }
        for name, spec in SPECS.items()
    }


@router.get("")
async def list_accounts(request: Request, user: User = Depends(require_user)):
    return [_view(request, a) for a in await store.list_accounts(user.id)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(request: Request, body: AccountCreate, user: User = Depends(require_user)):
    try:
        account = await store.create_account(user.id, body.broker, body.label, body.credentials, body.options)
    except (AccountError, VaultError) as e:
        raise _errors(e)
    await request.app.state.accounts.add(account)
    return _view(request, account)


@router.get("/{account_id}")
async def get_account(request: Request, account: BrokerAccount = Depends(owned_account)):
    return _view(request, account)


@router.patch("/{account_id}")
async def update_account(request: Request, body: AccountUpdate, account: BrokerAccount = Depends(owned_account)):
    """New credentials or options reconnect the account (and drop today's broker login)."""
    try:
        updated = await store.update_account(account.id, body.label, body.credentials, body.options)
    except (AccountError, VaultError) as e:
        raise _errors(e)
    if body.credentials or body.options is not None:
        await request.app.state.accounts.reconnect(account.id)
    return _view(request, updated)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(request: Request, account: BrokerAccount = Depends(owned_account)):
    """Disconnects the account and wipes its credentials and token. Its order and trade
    history is kept (still yours). Stop its strategies first."""
    runner = request.app.state.strategy_runner
    if any(inst.account_id == account.id for inst in runner.running.values()):
        raise HTTPException(status.HTTP_409_CONFLICT, "Stop this account's strategies first")
    async with db.session() as s, s.begin():
        await s.execute(update(StrategyInstance).where(StrategyInstance.account_id == account.id)
                        .values(status="archived"))
    await request.app.state.accounts.remove(account.id)
    await store.delete_account(account.id)


@router.post("/{account_id}/access-token")
async def paste_access_token(request: Request, body: AccessToken, account: BrokerAccount = Depends(owned_account)):
    """Use an access token generated elsewhere for today, instead of the broker login."""
    if not SPECS[account.broker].daily_login:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{account.broker} accounts don't use tokens")
    try:
        await store.save_token(account.id, body.access_token.strip())
    except VaultError as e:
        raise _errors(e)
    runtime = await request.app.state.accounts.reconnect(account.id)
    return {"connected": bool(runtime and runtime.service.connected),
            "reason": runtime.service.not_connected_reason if runtime and not runtime.service.connected else None}


@router.post("/{account_id}/broker-logout")
async def broker_logout(request: Request, account: BrokerAccount = Depends(owned_account)):
    """Forget today's broker token and disconnect."""
    await store.clear_token(account.id)
    await request.app.state.accounts.disconnect(account.id, "Logged out of the broker")
    return {"connected": False}


@router.post("/{account_id}/reconnect")
async def reconnect(request: Request, account: BrokerAccount = Depends(owned_account)):
    runtime = await request.app.state.accounts.reconnect(account.id)
    return _view(request, account) | {"connected": bool(runtime and runtime.service.connected)}
