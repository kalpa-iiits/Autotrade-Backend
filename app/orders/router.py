"""Orders app: place/cancel orders over REST or /api/ws/orders, `order_update` pushes on the
socket, order history, and account (positions, funds)."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, status
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.auth.dependencies import require_user
from app.core.broker_service import BrokerService
from app.core.http import broker_errors, broker_or_503
from app.core.connections import ConnectionManager
from app.core.ws import serve
from app.orders.history import list_events, list_history
from app.orders.schemas import (
    CLIENT_ORDER_ID_PATTERN, CancelOrder, Order, OrderMessage, OrderQuery, OrderType, PlaceOrder,
    Position, Side,
)

router = APIRouter(tags=["orders"])
_adapter = TypeAdapter(OrderMessage)


async def publish_order(connections: ConnectionManager, order: Order) -> None:
    await connections.broadcast({"type": "order_update", "order": order.model_dump(mode="json")})


@router.websocket("/ws/orders")
async def orders_socket(ws: WebSocket):
    connections: ConnectionManager = ws.app.state.order_connections
    brokers: BrokerService = ws.app.state.brokers

    async def handle(msg: PlaceOrder | CancelOrder | OrderQuery) -> dict:
        broker = brokers.require()
        if isinstance(msg, PlaceOrder):
            return {"order": (await brokers.place_order(msg)).model_dump(mode="json")}
        if isinstance(msg, CancelOrder):
            return {"order": (await broker.cancel_order(msg.order_id)).model_dump(mode="json")}
        if msg.action == "get_orders":
            return {"orders": [o.model_dump(mode="json") for o in await broker.get_orders()]}
        if msg.action == "get_positions":
            return {"positions": [p.model_dump(mode="json") for p in await broker.get_positions()]}
        return {"funds": await broker.get_funds()}

    await serve(ws, _adapter, handle, on_connect=connections.add,
                on_disconnect=lambda ws: _forget(connections, ws))


async def _forget(connections: ConnectionManager, ws: WebSocket) -> None:
    connections.remove(ws)


# ---------- REST (session cookie, or Authorization: Bearer <API token>) ----------

rest = APIRouter(prefix="/orders", tags=["orders"], dependencies=[Depends(require_user)])
account = APIRouter(prefix="/account", tags=["account"], dependencies=[Depends(require_user)])


class OrderCreate(BaseModel):
    symbol: str = Field(min_length=1)
    side: Side
    quantity: int = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = Field(default=None, gt=0)
    # Send the same one when retrying: the order is placed only once.
    client_order_id: str | None = Field(default=None, pattern=CLIENT_ORDER_ID_PATTERN)


@rest.get("", response_model=list[Order])
async def list_orders(request: Request):
    """Today's orders from the broker's order book."""
    async with broker_errors():
        return await broker_or_503(request).get_orders()


@rest.post("", response_model=Order, status_code=status.HTTP_201_CREATED)
async def place_order(request: Request, body: OrderCreate):
    """Place an order (same as place_order on /api/ws/orders). A broker rejection still returns
    201 with status REJECTED and the reason; 422 means the request itself was refused.
    Repeating a request with the same `client_order_id` returns the first order."""
    try:
        req = PlaceOrder(action="place_order", **body.model_dump())
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, e.errors(include_url=False, include_context=False))
    async with broker_errors():
        return await request.app.state.brokers.place_order(req)


@rest.get("/history")
async def history(
    broker: str | None = None,
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
):
    """Orders recorded in the database, newest first; works even when the broker is offline."""
    return await list_history(broker=broker, symbol=symbol, limit=limit)


@rest.delete("/{order_id}", response_model=Order)
async def cancel_order(request: Request, order_id: str):
    """Cancel an open order."""
    async with broker_errors():
        return await broker_or_503(request).cancel_order(order_id)


@rest.get("/{order_id}/events")
async def order_events(order_id: str, broker: str | None = None):
    """Every recorded state change of one order, oldest first."""
    events = await list_events(order_id, broker=broker)
    if not events:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No events recorded for this order")
    return events


@account.get("/positions", response_model=list[Position])
async def list_positions(request: Request):
    """Open positions at the broker."""
    async with broker_errors():
        return await broker_or_503(request).get_positions()


@account.get("/funds")
async def funds(request: Request):
    async with broker_errors():
        return await broker_or_503(request).get_funds()


router.include_router(rest)
router.include_router(account)
