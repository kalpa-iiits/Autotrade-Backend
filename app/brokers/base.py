from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Awaitable, Callable, Literal

from app.orders.schemas import Order, PlaceOrder, Position, _now, moves_backwards

# Called whenever an order changes state (filled, cancelled, ...).
OrderListener = Callable[[Order], Awaitable[None]]
# Called on every price tick: (symbol, price).
TickListener = Callable[[str, float], Awaitable[None]]
# Called after a stream reconnected (not on the first connect): updates may have been missed.
ReconnectListener = Callable[[], Awaitable[None]]


# Candle sizes every broker supports: daily and 1-minute.
Resolution = Literal["D", "1"]


@dataclass(frozen=True)
class Candle:
    start: datetime  # timezone-aware
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Instrument:
    """One broker-native cash-market instrument shown by symbol search."""

    symbol: str
    ticker: str
    name: str
    kind: Literal["stock", "index"]


@dataclass(frozen=True)
class Depth:
    """The best bid and ask: the prices an order can trade at right now (0 when a side is empty)."""

    bid: float
    ask: float


@dataclass(frozen=True)
class OptionContract:
    """One listed option on an underlying, in the broker's own symbol spelling."""

    symbol: str
    kind: Literal["CE", "PE"]
    strike: float
    expiry: date  # IST trading day it expires
    lot: int


class OrderError(Exception):
    """An order request the broker refused (unknown symbol, cannot cancel, ...)."""


class MissingCredentials(ValueError):
    """The broker cannot start until the user logs in / configures credentials."""


class Broker(ABC):
    """Interface every broker (paper, Fyers, Zerodha, ...) implements."""

    def __init__(self) -> None:
        self._order_listeners: list[OrderListener] = []
        self._tick_listeners: list[TickListener] = []
        self._reconnect_listeners: list[ReconnectListener] = []
        # Last known state per order id, for brokers that report orders via _record.
        self._orders: dict[str, Order] = {}
        self.ticks_received = 0
        self.last_tick_at: datetime | None = None
        # Latest problem with the streaming connection (shown on /health); None when fine.
        self.stream_error: str | None = None

    def on_order_update(self, listener: OrderListener) -> None:
        self._order_listeners.append(listener)

    def on_tick(self, listener: TickListener) -> None:
        self._tick_listeners.append(listener)

    def on_reconnect(self, listener: ReconnectListener) -> None:
        self._reconnect_listeners.append(listener)

    async def _emit_reconnect(self) -> None:
        for listener in self._reconnect_listeners:
            await listener()

    def health(self) -> dict:
        return {
            "ticks_received": self.ticks_received,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "stream_error": self.stream_error,
        }

    async def _emit_order(self, order: Order) -> None:
        for listener in self._order_listeners:
            await listener(order)

    async def _record(self, order: Order) -> Order:
        """Store the latest state of an order and broadcast it if anything changed. Repeated
        updates, and stale ones that would move the order backwards, are dropped.
        Returns the order's latest state."""
        prev = self._orders.get(order.id)
        if prev is not None:
            if (prev.status, prev.filled_quantity, prev.filled_price) == (
                order.status, order.filled_quantity, order.filled_price
            ) or moves_backwards(prev.status, prev.filled_quantity, order.status, order.filled_quantity):
                return prev
            order.created_at = prev.created_at
            order.client_order_id = order.client_order_id or prev.client_order_id
        order.updated_at = _now()
        self._orders[order.id] = order
        await self._emit_order(order)
        return order

    async def _emit_tick(self, symbol: str, price: float) -> None:
        # Listeners must be quick (record, enqueue): slow work runs on their own tasks.
        self.ticks_received += 1
        self.last_tick_at = _now()
        for listener in self._tick_listeners:
            await listener(symbol, price)

    async def start(self) -> None:
        """Connect / start background tasks."""

    async def stop(self) -> None:
        """Disconnect / stop background tasks."""

    async def subscribe(self, symbols: list[str]) -> None:
        """Start streaming ticks for these symbols (no-op if the broker streams everything)."""

    async def unsubscribe(self, symbols: list[str]) -> None:
        """Stop streaming ticks for symbols no client wants any more."""

    @abstractmethod
    async def place_order(self, req: PlaceOrder) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> Order: ...

    @abstractmethod
    async def get_orders(self) -> list[Order]: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_funds(self) -> dict: ...

    async def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        """Last traded price per symbol. Unknown symbols are left out."""
        raise OrderError(f"{type(self).__name__} does not provide quotes")

    async def get_instruments(self, exchange: str = "NSE") -> list[Instrument]:
        """Tradable cash-market instruments, using this broker's exact symbol spelling."""
        raise OrderError(f"{type(self).__name__} does not provide an instrument catalogue")

    async def get_depth(self, symbol: str) -> Depth | None:
        """Best bid and ask for `symbol`, or None if this broker doesn't provide market depth."""
        return None

    async def modify_order(self, order_id: str, limit_price: float) -> Order:
        """Move an open limit order to a new price (quantity unchanged)."""
        raise OrderError(f"{type(self).__name__} can't modify orders")

    async def _record_price(self, order_id: str, limit_price: float) -> Order:
        """Store and broadcast an order's new limit price after a modify. Not via _record: it
        drops updates whose status and fills haven't changed, which a price change doesn't."""
        order = self._orders.get(order_id)
        if order is None:
            order = next((o for o in await self.get_orders() if o.id == order_id), None)
        if order is None:
            raise OrderError(f"Order {order_id!r} modified but not found in orderbook")
        order = order.model_copy(update={"limit_price": limit_price, "updated_at": _now()})
        self._orders[order_id] = order
        await self._emit_order(order)
        return order

    async def get_tick_size(self, symbol: str) -> float | None:
        """The exchange's minimum price step for `symbol`, or None if unknown."""
        return None

    async def get_lot_size(self, symbol: str) -> int | None:
        """The exchange's trading lot for `symbol` (1 for cash equities), or None if unknown."""
        return None

    async def get_option_contracts(self, underlying: str) -> list[OptionContract]:
        """Every unexpired CE/PE listed on `underlying` (e.g. the NIFTY 50 index); empty if the
        broker lists none or can't say."""
        return []

    async def get_history(
        self, symbol: str, resolution: Resolution, start: date, end: date
    ) -> list[Candle]:
        """Candles from `start` to `end` (inclusive), oldest first. May include today's
        unfinished candle."""
        raise OrderError(f"{type(self).__name__} does not provide price history")
