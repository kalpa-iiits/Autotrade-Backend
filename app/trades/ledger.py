"""Turns broker order updates into order events, fills and open/closed positions.

Brokers report each order's cumulative filled quantity and average fill price. A fill is
the difference between two updates, so partial fills work and a repeated update adds nothing.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import db
from app.orders.models import OrderEvent, OrderRecord
from app.orders.schemas import Order, OrderStatus, Side, moves_backwards
from app.trades.models import Fill, Position, PositionFill

log = logging.getLogger(__name__)

LONG, SHORT = "LONG", "SHORT"
OPEN, CLOSED = "OPEN", "CLOSED"


def _weighted(avg: float | None, qty: int, price: float, add_qty: int) -> float:
    total = qty + add_qty
    return round(((avg or 0.0) * qty + price * add_qty) / total, 4)


class TradeLedger:
    def __init__(self, broker: str) -> None:
        self.broker = broker
        self.last_prices: dict[str, float] = {}  # from ticks; used for unrealized P&L
        # Updates are applied one at a time so two can't both read the same previous state.
        self._lock = asyncio.Lock()

    async def on_tick(self, symbol: str, price: float) -> None:
        self.last_prices[symbol] = price

    async def on_order(self, order: Order) -> None:
        # Called from broker callbacks: never let a bookkeeping error break order handling.
        try:
            async with self._lock, db.session() as s, s.begin():
                await self._apply_order(s, order)
        except Exception:
            log.exception("Could not record order update %s", order.id)

    async def _apply_order(self, s: AsyncSession, order: Order) -> None:
        prev = await s.get(OrderRecord, (self.broker, order.id))
        filled_qty = order.filled_quantity
        if order.status == OrderStatus.FILLED and not filled_qty:
            filled_qty = order.quantity  # broker said filled but didn't send a quantity
        prev_qty = prev.filled_quantity if prev else 0
        prev_avg = prev.filled_price if prev else None
        if prev is not None and moves_backwards(OrderStatus(prev.status), prev_qty, order.status, filled_qty):
            log.info("Ignoring stale update for order %s: %s %d after %s %d", order.id,
                     order.status.value, filled_qty, prev.status, prev_qty)
            return

        state = (order.status.value, filled_qty, order.filled_price)
        if prev is None or (prev.status, prev.filled_quantity, prev.filled_price) != state:
            s.add(OrderEvent(
                broker=self.broker, order_id=order.id, status=order.status.value,
                filled_quantity=filled_qty, filled_price=order.filled_price,
                reason=order.reason, at=order.updated_at,
            ))

        new_qty = filled_qty - prev_qty
        if new_qty > 0:
            if not order.filled_price:
                log.warning("Order %s filled %d without a price; fill not recorded", order.id, new_qty)
            else:
                # Price of just the new part, backed out of the two cumulative averages.
                if prev_qty and prev_avg:
                    price = (order.filled_price * filled_qty - prev_avg * prev_qty) / new_qty
                else:
                    price = order.filled_price
                fill = Fill(
                    id=f"{self.broker}:{order.id}:{filled_qty}", broker=self.broker,
                    order_id=order.id, symbol=order.symbol, side=order.side.value,
                    quantity=new_qty, price=round(price, 4), filled_at=order.updated_at,
                )
                s.add(fill)
                await s.flush()
                await self._apply_fill(s, fill)

        await s.merge(OrderRecord(
            broker=self.broker, id=order.id,
            # Later updates may not carry it (e.g. orderbook rows): keep the one we placed with.
            client_order_id=order.client_order_id or (prev.client_order_id if prev else None),
            symbol=order.symbol, side=order.side.value,
            quantity=order.quantity, order_type=order.order_type.value,
            limit_price=order.limit_price, status=order.status.value,
            filled_quantity=filled_qty, filled_price=order.filled_price, reason=order.reason,
            created_at=prev.created_at if prev else order.created_at, updated_at=order.updated_at,
        ))

    async def _apply_fill(self, s: AsyncSession, fill: Fill) -> None:
        direction = LONG if fill.side == Side.BUY.value else SHORT
        remaining = fill.quantity
        pos = await s.scalar(select(Position).where(
            Position.broker == fill.broker, Position.symbol == fill.symbol, Position.status == OPEN,
        ))

        if pos is not None and pos.direction != direction:
            # Opposite side: reduce or close the open position.
            closing = min(remaining, pos.quantity)
            sign = 1 if pos.direction == LONG else -1
            pos.realized_pnl = round(
                pos.realized_pnl + sign * (fill.price - pos.avg_entry_price) * closing, 4
            )
            pos.avg_exit_price = _weighted(pos.avg_exit_price, pos.exit_quantity, fill.price, closing)
            pos.exit_quantity += closing
            pos.quantity -= closing
            s.add(PositionFill(position_id=pos.id, fill_id=fill.id, quantity=closing, role="exit"))
            remaining -= closing
            if pos.quantity == 0:
                pos.status = CLOSED
                pos.closed_at = fill.filled_at
                pos = None

        if remaining <= 0:
            return
        if pos is None:
            # Nothing open (or the fill just closed one and has quantity left): open a new one.
            pos = Position(
                broker=fill.broker, symbol=fill.symbol, direction=direction, status=OPEN,
                quantity=remaining, entry_quantity=remaining, avg_entry_price=fill.price,
                exit_quantity=0, realized_pnl=0.0, opened_at=fill.filled_at,
            )
            s.add(pos)
            await s.flush()
        else:
            # Same side: add to the open position.
            pos.avg_entry_price = _weighted(pos.avg_entry_price, pos.quantity, fill.price, remaining)
            pos.quantity += remaining
            pos.entry_quantity += remaining
        s.add(PositionFill(position_id=pos.id, fill_id=fill.id, quantity=remaining, role="entry"))
