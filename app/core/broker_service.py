"""Owns one broker account's live connection, so its apps (orders, tickers, strategies) share
it and a broker login can reconnect it. There is one per broker account."""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Awaitable, Callable

from app.brokers import Broker, MissingCredentials, OrderError
from app.brokers.base import OrderListener, TickListener
from app.core.config import Settings
from app.core.db import utcnow
from app.core.ratelimit import RateLimiter
from app.orders.history import find_by_client_order_id, recorded_states
from app.orders.schemas import Order, OrderStatus, PlaceOrder, moves_backwards, new_client_order_id


log = logging.getLogger(__name__)


class BrokerNotConnected(OrderError):
    pass


def _state(order: Order) -> tuple[str, int, Decimal | None]:
    """What the ledger records about an order, normalised the way the ledger stores it."""
    filled = order.filled_quantity
    if order.status == OrderStatus.FILLED and not filled:
        filled = order.quantity
    price = Decimal(str(order.filled_price)).quantize(Decimal("0.0001")) if order.filled_price else None
    return order.status.value, filled, price


class BrokerService:
    def __init__(
        self,
        settings: Settings,
        broker_factory: Callable[[], Awaitable[Broker]],
        name: str,
        account_id: int | None = None,
        records_reliable: Callable[[], bool] = lambda: True,
    ) -> None:
        self.settings = settings
        self.name = name  # paper / fyers / zerodha
        self.account_id = account_id
        # Builds the account's broker from its stored credentials and today's token; raises
        # MissingCredentials while it can't (no credentials, not logged in today ...).
        self._broker_factory = broker_factory
        # False while the trade ledger has updates it could not save: then the database can't
        # be trusted to say whether an order was already placed, so the broker is asked.
        self.records_reliable = records_reliable
        self.broker: Broker | None = None
        self.not_connected_reason = "Broker not started"
        self._order_listeners: list[OrderListener] = []
        self._tick_listeners: list[TickListener] = []
        # Each returns symbols someone still needs (ticker clients, running strategies...).
        self._watchers: list[Callable[[], set[str]]] = []
        # client order id -> the placement in progress, so a concurrent retry joins it.
        self._placing: dict[str, asyncio.Future[Order]] = {}
        # Client order ids whose placement failed without an answer: it may have gone through.
        self._unknown_outcome: set[str] = set()
        self._orders_limit = RateLimiter(settings.broker_orders_per_second)
        self._resync_lock = asyncio.Lock()
        self._resync_task: asyncio.Task | None = None
        self.last_resync_at: datetime | None = None
        self.last_resync_error: str | None = None
        self.resynced_orders = 0  # updates replayed because a socket never delivered them

    def on_order_update(self, listener: OrderListener) -> None:
        self._order_listeners.append(listener)

    def on_tick(self, listener: TickListener) -> None:
        self._tick_listeners.append(listener)

    def add_watcher(self, watcher: Callable[[], set[str]]) -> None:
        self._watchers.append(watcher)

    def watched_symbols(self) -> set[str]:
        """Symbols to keep subscribed; re-subscribed whenever the broker (re)connects."""
        return set().union(*(w() for w in self._watchers))

    @property
    def connected(self) -> bool:
        return self.broker is not None

    def require(self) -> Broker:
        if self.broker is None:
            raise BrokerNotConnected(self.not_connected_reason)
        return self.broker

    async def start(self, resync: bool = True) -> None:
        try:
            broker = await self._broker_factory()
        except MissingCredentials as e:
            self.not_connected_reason = str(e)
            return
        for listener in self._order_listeners:
            broker.on_order_update(listener)
        for listener in self._tick_listeners:
            broker.on_tick(listener)
        if hasattr(broker, "on_reconnect"):
            broker.on_reconnect(self.resync)
        await broker.start()
        self.broker = broker
        if symbols := self.watched_symbols():
            await broker.subscribe(sorted(symbols))
        if resync:
            await self.resync()
        if self.settings.resync_interval > 0 and self._resync_task is None:
            self._resync_task = asyncio.create_task(self._resync_loop())

    async def stop(self, reason: str = "Broker stopped") -> None:
        broker, self.broker = self.broker, None
        self.not_connected_reason = reason
        if self._resync_task is not None:
            self._resync_task.cancel()
            self._resync_task = None
        if broker is not None:
            # Drop it first: even if shutting down fails, a restart must still bring up a
            # fresh broker rather than keep a half-closed one.
            try:
                await broker.stop()
            except Exception:
                log.warning("Error while stopping %s", type(broker).__name__, exc_info=True)

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    # ---------- reconciliation ----------

    async def resync(self) -> int:
        """Compare the broker's order book with what we recorded and replay every order whose
        state we missed (a socket down, an update dropped) to the order listeners: ledger,
        running strategies, clients. Runs after every (re)connect and every resync_interval.
        Returns how many orders were replayed. Never raises."""
        broker = self.broker
        if broker is None:
            return 0
        async with self._resync_lock:
            try:
                orders = await broker.get_orders()
                known = await recorded_states(self.account_id, [o.id for o in orders])
                replayed = 0
                for order in orders:
                    state = _state(order)
                    prev = known.get(order.id)
                    if prev == state:
                        continue
                    if prev is not None and moves_backwards(OrderStatus(prev[0]), prev[1],
                                                            order.status, state[1]):
                        continue  # our record is newer than the order book row
                    replayed += 1
                    for listener in self._order_listeners:
                        await listener(order)
            except Exception as e:
                self.last_resync_error = f"{type(e).__name__}: {e}"
                log.warning("Order resync with account %s (%s) failed: %s", self.account_id, self.name,
                            self.last_resync_error)
                return 0
            self.last_resync_at, self.last_resync_error = utcnow(), None
            self.resynced_orders += replayed
            if replayed:
                log.warning("Resync: %d order update(s) on account %s (%s) had been missed; applied now",
                            replayed, self.account_id, self.name)
            return replayed

    async def _resync_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.resync_interval)
            await self.resync()

    def health(self) -> dict:
        return {
            "name": self.name,
            "connected": self.connected,
            "reason": None if self.connected else self.not_connected_reason,
            "last_resync_at": self.last_resync_at.isoformat() if self.last_resync_at else None,
            "last_resync_error": self.last_resync_error,
            "resynced_orders": self.resynced_orders,
            **(self.broker.health() if self.broker is not None and hasattr(self.broker, "health") else {}),
        }

    # ---------- orders ----------

    async def place_order(self, req: PlaceOrder) -> Order:
        """Place an order at most once per client order id (one is made up if missing).
        Placing again with the same id, also while the first call is still running, returns
        that order instead of sending a second one. Every order placement goes through here."""
        broker = self.require()
        if req.client_order_id is None:
            req = req.model_copy(update={"client_order_id": new_client_order_id()})
        cid = req.client_order_id
        task = self._placing.get(cid)
        if task is None:
            task = asyncio.ensure_future(self._place_once(broker, req))
            self._placing[cid] = task
            task.add_done_callback(lambda _: self._placing.pop(cid, None))
        # Shielded: a caller that goes away (closed request) doesn't abandon a half-sent order.
        return await asyncio.shield(task)

    async def _place_once(self, broker: Broker, req: PlaceOrder) -> Order:
        cid = req.client_order_id
        # Already placed: the trade ledger recorded it when the broker reported it.
        if (order := await find_by_client_order_id(self.account_id, cid)) is not None:
            return order
        if cid in self._unknown_outcome or not self.records_reliable():
            # The last try got no answer (or the ledger missed updates), so it may have
            # reached the broker: look for it there.
            if found := next((o for o in await broker.get_orders() if o.client_order_id == cid), None):
                return found
        await self._orders_limit.acquire()
        try:
            order = await broker.place_order(req)
        except OrderError:
            raise  # refused before anything was sent (bad symbol ...): a retry may try again
        except Exception:
            self._unknown_outcome.add(cid)
            raise
        self._unknown_outcome.discard(cid)
        return order

    async def cancel_order(self, order_id: str) -> Order:
        """Every cancel goes through here (rate-limited like placements)."""
        broker = self.require()
        await self._orders_limit.acquire()
        return await broker.cancel_order(order_id)
