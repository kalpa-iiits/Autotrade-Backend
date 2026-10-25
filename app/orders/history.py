"""Order history reads. Writes happen in app.trades.ledger on every order update."""

from sqlalchemy import select

from app.core import db
from app.orders.models import OrderEvent, OrderRecord
from app.orders.schemas import Order


def _to_order(r: OrderRecord) -> Order:
    return Order(
        id=r.id, client_order_id=r.client_order_id, symbol=r.symbol, side=r.side,
        quantity=r.quantity, order_type=r.order_type, limit_price=r.limit_price, status=r.status,
        filled_quantity=r.filled_quantity, filled_price=r.filled_price, reason=r.reason,
        created_at=r.created_at, updated_at=r.updated_at,
    )


async def list_history(
    broker: str | None = None, symbol: str | None = None, limit: int = 100
) -> list[dict]:
    query = select(OrderRecord).order_by(OrderRecord.updated_at.desc()).limit(limit)
    if broker:
        query = query.where(OrderRecord.broker == broker)
    if symbol:
        query = query.where(OrderRecord.symbol == symbol)
    async with db.session() as s:
        rows = await s.scalars(query)
        return [{"broker": r.broker, **_to_order(r).model_dump(mode="json")} for r in rows]


async def find_by_client_order_id(broker: str, client_order_id: str) -> Order | None:
    """Latest recorded state of the order placed with this client order id, if any."""
    async with db.session() as s:
        row = await s.scalar(select(OrderRecord).where(
            OrderRecord.broker == broker, OrderRecord.client_order_id == client_order_id,
        ).limit(1))
    return _to_order(row) if row is not None else None


async def list_events(order_id: str, broker: str | None = None) -> list[dict]:
    query = select(OrderEvent).where(OrderEvent.order_id == order_id).order_by(OrderEvent.id)
    if broker:
        query = query.where(OrderEvent.broker == broker)
    async with db.session() as s:
        return [
            {"broker": e.broker, "order_id": e.order_id, "status": e.status,
             "filled_quantity": e.filled_quantity, "filled_price": e.filled_price,
             "reason": e.reason, "at": e.at.isoformat()}
            for e in await s.scalars(query)
        ]
