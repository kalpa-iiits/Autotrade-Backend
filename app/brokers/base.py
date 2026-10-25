from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Awaitable, Callable, Literal

from app.orders.schemas import Order, PlaceOrder, Position, _now, moves_backwards

# Called whenever an order changes state (filled, cancelled, ...).
OrderListener = Callable[[Order], Awaitable[None]]
# Called on every price tick: (symbol, price).
TickListener = Callable[[str, float], Awaitable[None]]


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


class OrderError(Exception):
    """An order request the broker refused (unknown symbol, cannot cancel, ...)."""


class MissingCredentials(ValueError):
    """The broker cannot start until the user logs in / configures credentials."""


class Broker(ABC):
    """Interface every broker (paper, Fyers, Zerodha, ...) implements."""

    def __init__(self) -> None:
        self._order_listeners: list[OrderListener] = []
        self._tick_listeners: list[TickListener] = []
        # Last known state per order id, for brokers that report orders via _record.
        self._orders: dict[str, Order] = {}

    def on_order_update(self, listener: OrderListener) -> None:
        self._order_listeners.append(listener)

    def on_tick(self, listener: TickListener) -> None:
        self._tick_listeners.append(listener)

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

    async def get_history(
        self, symbol: str, resolution: Resolution, start: date, end: date
    ) -> list[Candle]:
        """Candles from `start` to `end` (inclusive), oldest first. May include today's
        unfinished candle."""
        raise OrderError(f"{type(self).__name__} does not provide price history")
