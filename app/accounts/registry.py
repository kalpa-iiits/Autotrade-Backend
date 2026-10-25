"""Every active broker account's live pieces, kept apart from every other account's.

Per account (AccountRuntime): its broker connection (BrokerService: order de-duplication,
rate limit, resync), its trade ledger, and its own WebSocket channels for order updates and
ticks. One account's slow broker, failing ledger or disconnected socket never touches another.
The strategy runner is shared; each instance runs on one account.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from app.accounts import store
from app.accounts.models import BrokerAccount
from app.accounts.vault import VaultError
from app.brokers import Broker, MissingCredentials, create_broker
from app.core.broker_service import BrokerService
from app.core.config import Settings
from app.core.connections import ConnectionManager
from app.orders.schemas import Order
from app.trades.ledger import TradeLedger

if TYPE_CHECKING:
    from app.strategies.runner import StrategyRunner

log = logging.getLogger(__name__)


class AccountRuntime:
    def __init__(self, account: BrokerAccount, settings: Settings, runner: "StrategyRunner | None") -> None:
        self.id = account.id
        self.user_id = account.user_id
        self.broker = account.broker
        self.orders_ws = ConnectionManager(settings.ws_queue_size)
        self.tickers_ws = ConnectionManager(settings.ws_queue_size)
        self.ledger = TradeLedger(account.id, account.broker)
        self.service = BrokerService(settings, self._build_broker, name=account.broker, account_id=account.id,
                                     records_reliable=lambda: self.ledger.healthy)
        self._settings = settings

        s = self.service
        s.on_order_update(self.ledger.on_order)  # orders, order_events, fills, positions tables
        s.on_order_update(self._publish_order)
        s.on_tick(self.ledger.on_tick)
        s.on_tick(self._publish_tick)
        s.add_watcher(self.tickers_ws.watched)
        if runner is not None:
            s.add_watcher(lambda: runner.symbols(self.id))
            s.on_order_update(lambda order: runner.on_order(self.id, order))
            s.on_tick(lambda symbol, price: runner.on_tick(self.id, symbol, price))

    async def _build_broker(self) -> Broker:
        """Fresh from the database every (re)connect: new credentials or a new login apply."""
        account = await store.get_account(self.id)
        if account is None or account.status != "active":
            raise MissingCredentials("This account was deleted")
        try:
            credentials, token = store.credentials_of(account), store.token_of(account)
        except VaultError as e:
            raise MissingCredentials(str(e)) from None
        return create_broker(self._settings, account.broker, credentials, account.options,
                             access_token=token, account_id=account.id)

    async def _publish_order(self, order: Order) -> None:
        await self.orders_ws.broadcast({"type": "order_update", "account_id": self.id,
                                        "order": order.model_dump(mode="json")})

    async def _publish_tick(self, symbol: str, price: float) -> None:
        await self.tickers_ws.publish(symbol, {"type": "tick", "account_id": self.id, "symbol": symbol, "ltp": price})

    async def start(self, resync: bool = True) -> None:
        await self.ledger.start()
        try:
            await self.service.start(resync=resync)
        except Exception as e:
            # One account's broker failing to connect must not stop the server or other accounts.
            self.service.not_connected_reason = f"Could not connect: {type(e).__name__}: {e}"
            log.warning("Account %s (%s): %s", self.id, self.broker, self.service.not_connected_reason)

    async def stop(self) -> None:
        await self.service.stop("Account stopped")
        await self.ledger.stop()

    def health(self) -> dict:
        return {"broker": self.service.health(), "ledger": self.ledger.health(),
                "websockets": {"orders": len(self.orders_ws), "tickers": len(self.tickers_ws)}}


class AccountRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.runner: "StrategyRunner | None" = None
        self._runtimes: dict[int, AccountRuntime] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, account_id: int) -> AccountRuntime | None:
        return self._runtimes.get(account_id)

    def __iter__(self):
        return iter(list(self._runtimes.values()))

    def _lock(self, account_id: int) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    async def start_all(self, resync: bool = True) -> None:
        """At startup: bring up every active account (in parallel; failures stay per account)."""
        accounts = await store.list_accounts()
        await asyncio.gather(*(self.add(a, resync=resync) for a in accounts))

    async def add(self, account: BrokerAccount, resync: bool = True) -> AccountRuntime:
        async with self._lock(account.id):
            if (existing := self._runtimes.get(account.id)) is not None:
                return existing
            runtime = AccountRuntime(account, self.settings, self.runner)
            self._runtimes[account.id] = runtime
            await runtime.start(resync=resync)
            return runtime

    async def reconnect(self, account_id: int) -> AccountRuntime | None:
        """After new credentials or a broker login: reconnect with what's stored now."""
        runtime = self._runtimes.get(account_id)
        if runtime is None:
            account = await store.get_account(account_id)
            return await self.add(account) if account and account.status == "active" else None
        async with self._lock(account_id):
            await runtime.service.restart()
        return runtime

    async def disconnect(self, account_id: int, reason: str) -> None:
        if (runtime := self._runtimes.get(account_id)) is not None:
            async with self._lock(account_id):
                await runtime.service.stop(reason)

    async def remove(self, account_id: int) -> None:
        runtime = self._runtimes.pop(account_id, None)
        if runtime is not None:
            async with self._lock(account_id):
                await runtime.stop()

    async def resync_all(self) -> None:
        await asyncio.gather(*(r.service.resync() for r in self))

    async def stop_all(self) -> None:
        await asyncio.gather(*(r.stop() for r in self), return_exceptions=True)
        self._runtimes.clear()

    async def publish(self, account_id: int, message: dict) -> None:
        """Push to the account's /ws/orders clients (strategy signals and status changes)."""
        if (runtime := self._runtimes.get(account_id)) is not None:
            await runtime.orders_ws.broadcast({"account_id": account_id, **message})

    def summary(self) -> dict:
        """Counts only: safe for the public /health."""
        runtimes = list(self)
        return {
            "active": len(runtimes),
            "connected": sum(r.service.connected for r in runtimes),
            "ledger_unhealthy": sum(not r.ledger.healthy for r in runtimes),
            "stream_errors": sum(bool(getattr(r.service.broker, "stream_error", None)) for r in runtimes),
            "websocket_clients": sum(len(r.orders_ws) + len(r.tickers_ws) for r in runtimes),
            "evicted_slow_clients": sum(r.orders_ws.evicted + r.tickers_ws.evicted for r in runtimes),
        }
