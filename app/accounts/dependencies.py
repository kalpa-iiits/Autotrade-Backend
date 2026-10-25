"""Route dependencies: the caller's own broker account, or 404. Another user's account id
gets the same 404 as one that doesn't exist, so ids can't be probed."""

from fastapi import Depends, HTTPException, Request, status

from app.accounts import store
from app.accounts.models import BrokerAccount
from app.accounts.registry import AccountRuntime
from app.auth.dependencies import require_user
from app.auth.models import User


def owns(user: User | None, account: BrokerAccount | None) -> bool:
    return user is not None and account is not None and account.user_id == user.id and account.status == "active"


async def owned_account(account_id: int, user: User = Depends(require_user)) -> BrokerAccount:
    account = await store.get_account(account_id)
    if not owns(user, account):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such broker account")
    return account


async def account_runtime(request: Request, account: BrokerAccount = Depends(owned_account)) -> AccountRuntime:
    runtime = request.app.state.accounts.get(account.id)
    if runtime is None:
        runtime = await request.app.state.accounts.add(account)
    return runtime
