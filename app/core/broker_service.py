"""Owns the live broker so apps can share it and auth can reconnect it after a login."""

import asyncio
import logging
from typing import Awaitable, Callable

from app.brokers import Broker, MissingCredentials, OrderError, create_broker
from app.brokers.base import OrderListener, TickListener
from app.core.config import Settings
from app.orders.history import find_by_client_order_id
from app.orders.schemas import Order, PlaceOrder, new_client_order_id


log = logging.getLogger(__name__)


class BrokerNotConnected(OrderError):
    pass


class BrokerService:
    def __init__(
        self, settings: Settings, token_loader: Callable[[], Awaitable[str | None]]
    ) -> None:
        self.settings = settings
        self.name = settings.broker
        self._token_loader = token_loader
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

    async def start(self) -> None:
        try:
            broker = create_broker(self.settings, access_token=await self._token_loader())
        except MissingCredentials as e:
            self.not_connected_reason = str(e)
            return
        for listener in self._order_listeners:
            broker.on_order_update(listener)
        for listener in self._tick_listeners:
            broker.on_tick(listener)
        await broker.start()
        self.broker = broker
        if symbols := self.watched_symbols():
            await broker.subscribe(sorted(symbols))

    async def stop(self, reason: str = "Broker stopped") -> None:
        broker, self.broker = self.broker, None
        self.not_connected_reason = reason
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
        if (order := await find_by_client_order_id(self.name, cid)) is not None:
            return order
        if cid in self._unknown_outcome:
            # The last try got no answer, so it may have reached the broker: look for it there.
            if found := next((o for o in await broker.get_orders() if o.client_order_id == cid), None):
                return found
        try:
            order = await broker.place_order(req)
        except OrderError:
            raise  # refused before anything was sent (bad symbol ...): a retry may try again
        except Exception:
            self._unknown_outcome.add(cid)
            raise
        self._unknown_outcome.discard(cid)
        return order
