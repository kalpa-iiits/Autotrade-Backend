"""Orders app, per broker account: place/cancel orders over REST or the account's
/ws/orders socket (which also pushes its `order_update`s), order history, positions and funds.
Everything is under /api/accounts/{account_id}/... and only for the account's owner."""

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.accounts.dependencies import account_runtime
from app.accounts.registry import AccountRuntime
from app.core.http import broker_errors, broker_or_503
from app.core.ws import serve
from app.orders.history import list_events, list_history
from app.orders.schemas import (
    CLIENT_ORDER_ID_PATTERN, CancelOrder, Order, OrderMessage, OrderQuery, OrderType, PlaceOrder,
    Position, Side,
)

router = APIRouter(tags=["orders"])
_adapter = TypeAdapter(OrderMessage)


@router.websocket("/accounts/{account_id}/ws/orders")
async def orders_socket(ws: WebSocket, account_id: int):
    runtime = ws.app.state.accounts.get(account_id)

    async def handle(msg: PlaceOrder | CancelOrder | OrderQuery) -> dict:
        service = runtime.service
        broker = service.require()
        if isinstance(msg, PlaceOrder):
            return {"order": (await service.place_order(msg)).model_dump(mode="json")}
        if isinstance(msg, CancelOrder):
            return {"order": (await service.cancel_order(msg.order_id)).model_dump(mode="json")}
        if msg.action == "get_orders":
            return {"orders": [o.model_dump(mode="json") for o in await broker.get_orders()]}
        if msg.action == "get_positions":
            return {"positions": [p.model_dump(mode="json") for p in await broker.get_positions()]}
        return {"funds": await broker.get_funds()}

    await serve(ws, _adapter, handle, runtime, lambda r: r.orders_ws)


# ---------- REST (session cookie, or Authorization: Bearer <API token>) ----------

rest = APIRouter(prefix="/accounts/{account_id}", tags=["orders"])


class OrderCreate(BaseModel):
    symbol: str = Field(min_length=1)
    side: Side
    quantity: int = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = Field(default=None, gt=0)
    # Send the same one when retrying: the order is placed only once.
    client_order_id: str | None = Field(default=None, pattern=CLIENT_ORDER_ID_PATTERN)


@rest.get("/orders", response_model=list[Order])
async def list_orders(runtime: AccountRuntime = Depends(account_runtime)):
    """Today's orders from the broker's order book."""
    async with broker_errors():
        return await broker_or_503(runtime.service).get_orders()


@rest.post("/orders", response_model=Order, status_code=status.HTTP_201_CREATED)
async def place_order(body: OrderCreate, runtime: AccountRuntime = Depends(account_runtime)):
    """Place an order (same as place_order on the account's socket). A broker rejection still
    returns 201 with status REJECTED and the reason; 422 means the request itself was refused.
    Repeating a request with the same `client_order_id` returns the first order."""
    try:
        req = PlaceOrder(action="place_order", **body.model_dump())
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, e.errors(include_url=False, include_context=False))
    async with broker_errors():
        return await runtime.service.place_order(req)


@rest.get("/orders/history")
async def history(
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    runtime: AccountRuntime = Depends(account_runtime),
):
    """Orders recorded in the database, newest first; works even when the broker is offline."""
    return await list_history(runtime.id, symbol=symbol, limit=limit)


@rest.delete("/orders/{order_id}", response_model=Order)
async def cancel_order(order_id: str, runtime: AccountRuntime = Depends(account_runtime)):
    """Cancel an open order."""
    async with broker_errors():
        return await runtime.service.cancel_order(order_id)


@rest.get("/orders/{order_id}/events")
async def order_events(order_id: str, runtime: AccountRuntime = Depends(account_runtime)):
    """Every recorded state change of one order, oldest first."""
    events = await list_events(runtime.id, order_id)
    if not events:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No events recorded for this order")
    return events


@rest.get("/positions", response_model=list[Position], tags=["account"])
async def list_positions(runtime: AccountRuntime = Depends(account_runtime)):
    """Open positions at the broker."""
    async with broker_errors():
        return await broker_or_503(runtime.service).get_positions()


@rest.get("/funds", tags=["account"])
async def funds(runtime: AccountRuntime = Depends(account_runtime)):
    async with broker_errors():
        return await broker_or_503(runtime.service).get_funds()


router.include_router(rest)
