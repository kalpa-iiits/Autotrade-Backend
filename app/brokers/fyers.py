"""Fyers API v3 broker: real orders via REST, live ticks and order updates via WebSockets.

The Fyers SDK is synchronous and runs its sockets on its own threads, so REST calls go
through asyncio.to_thread and socket callbacks hop back onto the event loop through one queue,
handled by one task: ticks and order updates reach listeners one at a time and in the order
the sockets delivered them, as with the other brokers.
"""

import asyncio
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws, order_ws

from app.brokers.base import Broker, Candle, MissingCredentials, OrderError, Resolution
from app.orders.schemas import (
    Order,
    OrderStatus,
    OrderType,
    PlaceOrder,
    Position,
    Side,
)

log = logging.getLogger(__name__)

# Fyers numeric codes -> our enums.
FYERS_STATUS = {
    1: OrderStatus.CANCELLED,
    2: OrderStatus.FILLED,
    4: OrderStatus.OPEN,  # transit
    5: OrderStatus.REJECTED,
    6: OrderStatus.OPEN,  # pending
}
FYERS_ORDER_TYPE = {OrderType.LIMIT: 1, OrderType.MARKET: 2}
FYERS_SIDE = {Side.BUY: 1, Side.SELL: -1}


def _client_order_id(tag) -> str | None:
    """Our client order id from a Fyers orderTag ("abc123", or with a "1:" style prefix)."""
    tag = str(tag or "").rsplit(":", 1)[-1]
    return tag if tag and tag != "Untagged" else None


def _order_from_fyers(data: dict) -> Order:
    """Build an Order from an orderbook row or an order-socket update (same field names)."""
    order_type = OrderType.LIMIT if data.get("type") == 1 else OrderType.MARKET
    return Order(
        id=str(data["id"]),
        client_order_id=_client_order_id(data.get("orderTag")),
        symbol=data["symbol"],
        side=Side.BUY if data.get("side") == 1 else Side.SELL,
        quantity=int(data.get("qty", 0)),
        order_type=order_type,
        limit_price=data.get("limitPrice") if order_type == OrderType.LIMIT else None,
        status=FYERS_STATUS.get(data.get("status"), OrderStatus.OPEN),
        filled_quantity=int(data.get("filledQty") or 0),
        filled_price=data.get("tradedPrice") or None,
        reason=data.get("message") or None,
    )


class FyersBroker(Broker):
    def __init__(
        self,
        client_id: str,
        access_token: str,
        product_type: str = "INTRADAY",
        log_dir: str = "logs",
        model=None,
        data_socket_factory=None,
        order_socket_factory=None,
    ) -> None:
        super().__init__()
        if not client_id or not access_token:
            raise MissingCredentials(
                "Not logged in to Fyers today: start the broker login for this account "
                "(GET /api/accounts/{id}/broker-login) or paste an access token"
            )
        # Accept either the raw token or the "CLIENT_ID:token" form the sockets want.
        if ":" in access_token:
            access_token = access_token.split(":", 1)[1]

        self.product_type = product_type
        self._socket_token = f"{client_id}:{access_token}"
        self._log_dir = str(Path(log_dir).resolve())
        Path(self._log_dir).mkdir(parents=True, exist_ok=True)

        self._model = model or fyersModel.FyersModel(
            client_id=client_id, token=access_token, is_async=False, log_path=self._log_dir
        )
        self._data_socket_factory = data_socket_factory or data_ws.FyersDataSocket
        self._order_socket_factory = order_socket_factory or order_ws.FyersOrderSocket

        self._loop: asyncio.AbstractEventLoop | None = None
        self._events: asyncio.Queue | None = None
        self._pump: asyncio.Task | None = None
        self._data_socket = None
        self._order_socket = None
        self._symbols: set[str] = set()
        self._order_connects = 0

    # ---------- lifecycle ----------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._events = asyncio.Queue()
        self._pump = asyncio.create_task(self._run_events())
        self._data_socket = self._data_socket_factory(
            access_token=self._socket_token,
            log_path=self._log_dir,
            litemode=True,  # LTP only; switch off for full quote/depth fields
            write_to_file=False,
            reconnect=True,
            on_connect=self._on_data_connect,
            on_message=self._on_tick_message,
            on_error=lambda msg: self._on_socket_error("data", msg),
            on_close=lambda msg: log.info("Fyers data socket closed: %s", msg),
        )
        self._order_socket = self._order_socket_factory(
            access_token=self._socket_token,
            log_path=self._log_dir,
            write_to_file=False,
            reconnect=True,
            on_connect=self._on_order_connect,
            on_orders=self._on_order_message,
            on_general=lambda msg: None,  # otherwise the SDK print()s these
            on_error=lambda msg: self._on_socket_error("order", msg),
            on_close=lambda msg: log.info("Fyers order socket closed: %s", msg),
        )
        await asyncio.to_thread(self._data_socket.connect)
        await asyncio.to_thread(self._order_socket.connect)

    async def stop(self) -> None:
        # Close each socket on its own: an SDK error on one must not leave the other running.
        # (FyersOrderSocket.close_connection joins a thread that only exists if the SDK's
        # keep_running() was called, so it raises AttributeError after the socket is closed.)
        for sock in (self._data_socket, self._order_socket):
            if sock is None:
                continue
            try:
                await asyncio.to_thread(sock.close_connection)
            except Exception:
                log.warning("Error while closing %s", type(sock).__name__, exc_info=True)
        self._data_socket = self._order_socket = None
        self._loop = None
        if self._pump is not None:
            self._pump.cancel()
            try:
                await self._pump
            except asyncio.CancelledError:
                pass
            self._pump = None
        while self._events is not None and not self._events.empty():
            self._events.get_nowait().close()  # never-run coroutines: no "never awaited" warning

    async def _run_events(self) -> None:
        while True:
            coro = await self._events.get()
            try:
                await coro
            except Exception:
                log.exception("Error handling a Fyers socket message")

    # ---------- socket callbacks (run on SDK threads) ----------

    def _on_data_connect(self) -> None:
        # Also fires after a reconnect, so re-subscribe everything we were watching.
        self.stream_error = None
        if self._symbols:
            self._data_socket.subscribe(symbols=sorted(self._symbols), data_type="SymbolUpdate")

    def _on_order_connect(self) -> None:
        self._order_socket.subscribe(data_type="OnOrders")
        self._order_connects += 1
        if self._order_connects > 1:
            # Order updates sent while the socket was down are lost: reconcile.
            self._schedule(self._emit_reconnect())

    def _on_socket_error(self, which: str, message) -> None:
        self.stream_error = f"Fyers {which} socket: {message}"
        log.warning("%s", self.stream_error)

    def _on_tick_message(self, message) -> None:
        if isinstance(message, dict) and "ltp" in message and "symbol" in message:
            self._schedule(self._emit_tick(message["symbol"], message["ltp"]))

    def _on_order_message(self, message) -> None:
        data = message.get("orders") if isinstance(message, dict) else None
        if data and "id" in data:
            self._schedule(self._record(_order_from_fyers(data)))

    def _schedule(self, coro) -> None:
        loop, events = self._loop, self._events
        if loop is None or events is None:
            coro.close()
            return
        try:
            loop.call_soon_threadsafe(events.put_nowait, coro)
        except RuntimeError:  # loop already closed (shutdown)
            coro.close()

    # ---------- market data ----------

    async def subscribe(self, symbols: list[str]) -> None:
        new = set(symbols) - self._symbols
        if not new:
            return
        self._symbols |= new
        await asyncio.to_thread(
            self._data_socket.subscribe, symbols=sorted(new), data_type="SymbolUpdate"
        )

    async def unsubscribe(self, symbols: list[str]) -> None:
        gone = set(symbols) & self._symbols
        if not gone:
            return
        self._symbols -= gone
        await asyncio.to_thread(
            self._data_socket.unsubscribe, symbols=sorted(gone), data_type="SymbolUpdate"
        )

    # ---------- orders ----------

    async def place_order(self, req: PlaceOrder) -> Order:
        payload = {
            "symbol": req.symbol,
            "qty": req.quantity,
            "type": FYERS_ORDER_TYPE[req.order_type],
            "side": FYERS_SIDE[req.side],
            "productType": self.product_type,
            "limitPrice": req.limit_price or 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        if req.client_order_id:
            payload["orderTag"] = req.client_order_id
        resp = await asyncio.to_thread(self._model.place_order, payload)

        order = Order(
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            quantity=req.quantity,
            order_type=req.order_type,
            limit_price=req.limit_price,
        )
        if resp.get("s") == "ok" and resp.get("id"):
            order.id = str(resp["id"])
        else:
            order.status = OrderStatus.REJECTED
            order.reason = resp.get("message", "Order rejected by Fyers")
        # Fills and later state changes arrive through the order socket, possibly before this
        # reply: then the socket's state is newer and is what we return.
        return await self._record(order)

    async def cancel_order(self, order_id: str) -> Order:
        resp = await asyncio.to_thread(self._model.cancel_order, {"id": order_id})
        if resp.get("s") != "ok":
            raise OrderError(resp.get("message", f"Could not cancel order {order_id!r}"))

        order = self._orders.get(order_id)
        if order is None:
            order = next((o for o in await self.get_orders() if o.id == order_id), None)
        if order is None:
            raise OrderError(f"Order {order_id!r} cancelled but not found in orderbook")
        return await self._record(order.model_copy(update={"status": OrderStatus.CANCELLED}))

    async def get_orders(self) -> list[Order]:
        resp = await asyncio.to_thread(self._model.orderbook)
        if resp.get("s") != "ok":
            raise OrderError(resp.get("message", "Could not fetch orderbook"))
        return [_order_from_fyers(row) for row in resp.get("orderBook", [])]

    async def get_positions(self) -> list[Position]:
        resp = await asyncio.to_thread(self._model.positions)
        if resp.get("s") != "ok":
            raise OrderError(resp.get("message", "Could not fetch positions"))
        return [
            Position(symbol=p["symbol"], quantity=p["netQty"], avg_price=p["netAvg"])
            for p in resp.get("netPositions", [])
            if p.get("netQty")
        ]

    async def get_funds(self) -> dict:
        resp = await asyncio.to_thread(self._model.funds)
        if resp.get("s") != "ok":
            raise OrderError(resp.get("message", "Could not fetch funds"))
        limits = {row["title"]: row.get("equityAmount") for row in resp.get("fund_limit", [])}
        return {"cash": limits.get("Available Balance"), "limits": limits}

    async def get_history(
        self, symbol: str, resolution: Resolution, start: date, end: date
    ) -> list[Candle]:
        resp = await asyncio.to_thread(self._model.history, {
            "symbol": symbol, "resolution": resolution, "date_format": "1",
            "range_from": start.isoformat(), "range_to": end.isoformat(),
            "cont_flag": "1",  # futures: continuous across expiries
        })
        if resp.get("s") != "ok":
            raise OrderError(resp.get("message", f"Could not fetch history for {symbol}"))
        return [
            Candle(datetime.fromtimestamp(ts, tz=timezone.utc), o, h, low, c, v)
            for ts, o, h, low, c, v in resp.get("candles", [])
        ]


    async def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        resp = await asyncio.to_thread(self._model.quotes, {"symbols": ",".join(symbols)})
        if resp.get("s") != "ok":
            raise OrderError(resp.get("message", "Could not fetch quotes"))
        return {d["n"]: d["v"]["lp"] for d in resp.get("d", [])
                if d.get("s") == "ok" and isinstance(d.get("v"), dict) and "lp" in d["v"]}
