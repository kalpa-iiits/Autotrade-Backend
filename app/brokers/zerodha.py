"""Zerodha Kite Connect broker: orders via the Kite REST API, ticks and order updates via the
Kite WebSocket.

REST calls use the official kiteconnect SDK (synchronous, so they go through
asyncio.to_thread). The SDK's KiteTicker runs on Twisted, whose reactor cannot be
restarted after the daily re-login, so the WebSocket is a small asyncio client here
speaking the same protocol: JSON text frames for control and order updates, binary
frames for ticks.
"""

import asyncio
import json
import logging
import struct
from datetime import date, datetime, time
from urllib.parse import urlencode

from kiteconnect import KiteConnect, KiteTicker
from kiteconnect.exceptions import KiteException
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.brokers.base import Broker, Candle, MissingCredentials, OrderError, Resolution
from app.orders.schemas import Order, OrderStatus, OrderType, PlaceOrder, Position, Side

log = logging.getLogger(__name__)
KITE_WS_URL = "wss://ws.kite.trade"
RECONNECT_DELAY = 1.0  # pause after a dropped connection before reconnecting
VARIETY = "regular"

KITE_STATUS = {
    "COMPLETE": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "CANCELLED AMO": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}  # everything else (OPEN, TRIGGER PENDING, ... PENDING) is still open

_SEGMENT = KiteTicker.EXCHANGE_MAP


def split_symbol(symbol: str) -> tuple[str, str]:
    """"NSE:SBIN" -> ("NSE", "SBIN")."""
    exchange, sep, tradingsymbol = symbol.partition(":")
    if not sep or not exchange or not tradingsymbol:
        raise OrderError(f"Zerodha symbols look like EXCHANGE:TRADINGSYMBOL, e.g. NSE:SBIN (got {symbol!r})")
    return exchange.upper(), tradingsymbol


def parse_ticks(frame: bytes) -> list[tuple[int, float]]:
    """Binary tick frame -> [(instrument_token, last_price)].

    Frame: 2-byte packet count, then per packet a 2-byte length and the packet. Every
    packet mode (ltp / quote / full) starts with the token and last price as big-endian
    int32s; prices are in paise except for currency segments.
    """
    if len(frame) < 2:  # 1-byte heartbeat
        return []
    (count,) = struct.unpack(">H", frame[:2])
    ticks, pos = [], 2
    for _ in range(count):
        (length,) = struct.unpack(">H", frame[pos:pos + 2])
        packet = frame[pos + 2:pos + 2 + length]
        pos += 2 + length
        if len(packet) < 8:
            continue
        token, raw_price = struct.unpack(">II", packet[:8])
        segment = token & 0xFF
        if segment == _SEGMENT["cds"]:
            divisor = 10_000_000.0
        elif segment in (_SEGMENT["bcd"], _SEGMENT["nco"]):
            divisor = 10_000.0
        else:
            divisor = 100.0
        ticks.append((token, raw_price / divisor))
    return ticks


def _order_from_kite(data: dict) -> Order:
    """Build an Order from an orders() row or a WebSocket order update (same fields)."""
    order_type = OrderType.LIMIT if data.get("order_type") == "LIMIT" else OrderType.MARKET
    return Order(
        id=str(data["order_id"]),
        client_order_id=data.get("tag") or None,
        symbol=f"{data['exchange']}:{data['tradingsymbol']}",
        side=Side.BUY if data.get("transaction_type") == "BUY" else Side.SELL,
        quantity=int(data.get("quantity", 0)),
        order_type=order_type,
        limit_price=data.get("price") if order_type == OrderType.LIMIT else None,
        status=KITE_STATUS.get(data.get("status"), OrderStatus.OPEN),
        filled_quantity=int(data.get("filled_quantity") or 0),
        filled_price=data.get("average_price") or None,
        reason=data.get("status_message") or None,
    )


class ZerodhaBroker(Broker):
    def __init__(
        self,
        api_key: str,
        access_token: str,
        product: str = "MIS",
        kite=None,
        connect=ws_connect,
        ws_url: str = KITE_WS_URL,
    ) -> None:
        super().__init__()
        if not api_key or not access_token:
            raise MissingCredentials(
                "Not logged in to Zerodha today: log in to the broker for this account "
                "(broker-login) or paste today's access token"
            )
        self.product = product
        self._kite = kite or KiteConnect(api_key=api_key, access_token=access_token)
        self._connect = connect
        self._ws_url = f"{ws_url}?{urlencode({'api_key': api_key, 'access_token': access_token})}"

        self._ws = None
        self._task: asyncio.Task | None = None
        # Kite streams by numeric instrument token; keep both directions of the mapping.
        self._token_of: dict[str, int] = {}
        self._symbol_of: dict[int, str] = {}
        self._subscribed: set[int] = set()

    # ---------- lifecycle ----------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_socket())

    async def stop(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_socket(self) -> None:
        # Iterating connect() reconnects, with backoff when connecting fails.
        connects = 0
        try:
            async for ws in self._connect(self._ws_url):
                self._ws = ws
                self.stream_error = None
                connects += 1
                try:
                    if self._subscribed:
                        await self._send_subscribe(sorted(self._subscribed))
                    if connects > 1:
                        # Order updates sent while we were disconnected are lost: reconcile.
                        await self._emit_reconnect()
                    async for frame in ws:
                        await self._handle_frame(frame)
                except ConnectionClosed as e:
                    self.stream_error = f"Kite WebSocket dropped: {e}"
                    log.warning("%s; reconnecting", self.stream_error)
                finally:
                    self._ws = None
                # connect() only backs off on failed attempts, so pace reconnects after drops.
                await asyncio.sleep(RECONNECT_DELAY)
        except InvalidStatus as e:
            # 403 = expired/invalid access token; retrying won't help until a new login.
            self.stream_error = f"Kite WebSocket refused the connection: {e}"
            log.error("%s", self.stream_error)

    async def _handle_frame(self, frame) -> None:
        if isinstance(frame, bytes):
            for token, price in parse_ticks(frame):
                if symbol := self._symbol_of.get(token):
                    await self._emit_tick(symbol, price)
            return
        try:
            message = json.loads(frame)
        except ValueError:
            return
        if message.get("type") == "order" and message.get("data"):
            await self._record(_order_from_kite(message["data"]))

    # ---------- market data ----------

    async def _send_subscribe(self, tokens: list[int]) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps({"a": "subscribe", "v": tokens}))
            await self._ws.send(json.dumps({"a": "mode", "v": ["ltp", tokens]}))

    async def _resolve(self, symbols: list[str]) -> list[int]:
        missing = [s for s in symbols if s not in self._token_of]
        if missing:
            for s in missing:
                split_symbol(s)
            try:
                quotes = await asyncio.to_thread(self._kite.ltp, missing)
            except KiteException as e:
                raise OrderError(str(e))
            for s in missing:
                if s not in quotes:
                    raise OrderError(f"Unknown symbol {s!r}")
                token = int(quotes[s]["instrument_token"])
                self._token_of[s] = token
                self._symbol_of[token] = s
        return [self._token_of[s] for s in symbols]

    async def subscribe(self, symbols: list[str]) -> None:
        new = [t for t in await self._resolve(symbols) if t not in self._subscribed]
        if new:
            self._subscribed.update(new)
            await self._send_subscribe(new)

    async def unsubscribe(self, symbols: list[str]) -> None:
        tokens = [self._token_of[s] for s in symbols if self._token_of.get(s) in self._subscribed]
        if tokens:
            self._subscribed.difference_update(tokens)
            if self._ws is not None:
                await self._ws.send(json.dumps({"a": "unsubscribe", "v": tokens}))

    # ---------- orders ----------

    async def place_order(self, req: PlaceOrder) -> Order:
        exchange, tradingsymbol = split_symbol(req.symbol)
        params = dict(
            variety=VARIETY,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=req.side.value,
            quantity=req.quantity,
            product=self.product,
            order_type=req.order_type.value,
            validity="DAY",
        )
        if req.client_order_id:
            params["tag"] = req.client_order_id
        if req.order_type == OrderType.LIMIT:
            params["price"] = req.limit_price
        else:
            params["market_protection"] = -1  # let Kite apply its automatic price band

        order = Order(
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            quantity=req.quantity,
            order_type=req.order_type,
            limit_price=req.limit_price,
        )
        try:
            order.id = str(await asyncio.to_thread(self._kite.place_order, **params))
        except KiteException as e:
            order.status = OrderStatus.REJECTED
            order.reason = str(e)
        # Fills and later state changes arrive through the WebSocket, possibly before this
        # reply: then the socket's state is newer and is what we return.
        return await self._record(order)

    async def cancel_order(self, order_id: str) -> Order:
        try:
            await asyncio.to_thread(self._kite.cancel_order, VARIETY, order_id)
        except KiteException as e:
            raise OrderError(str(e))

        order = self._orders.get(order_id)
        if order is None:
            order = next((o for o in await self.get_orders() if o.id == order_id), None)
        if order is None:
            raise OrderError(f"Order {order_id!r} cancelled but not found in orderbook")
        return await self._record(order.model_copy(update={"status": OrderStatus.CANCELLED}))

    async def _call(self, fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except KiteException as e:
            raise OrderError(str(e))

    async def get_orders(self) -> list[Order]:
        return [_order_from_kite(row) for row in await self._call(self._kite.orders)]

    async def get_positions(self) -> list[Position]:
        positions = await self._call(self._kite.positions)
        return [
            Position(
                symbol=f"{p['exchange']}:{p['tradingsymbol']}",
                quantity=p["quantity"],
                avg_price=p["average_price"],
            )
            for p in positions.get("net", [])
            if p.get("quantity")
        ]

    async def get_funds(self) -> dict:
        equity = await self._call(self._kite.margins, "equity")
        return {"cash": equity.get("net"), "limits": equity.get("available", {})}

    async def get_history(
        self, symbol: str, resolution: Resolution, start: date, end: date
    ) -> list[Candle]:
        [token] = await self._resolve([symbol])
        interval = "day" if resolution == "D" else "minute"
        rows = await self._call(
            self._kite.historical_data, token,
            datetime.combine(start, time.min), datetime.combine(end, time.max), interval,
        )
        return [Candle(r["date"], r["open"], r["high"], r["low"], r["close"], r.get("volume", 0))
                for r in rows]


    async def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        for s in symbols:
            split_symbol(s)
        quotes = await self._call(self._kite.ltp, symbols)
        return {s: q["last_price"] for s, q in quotes.items()}
