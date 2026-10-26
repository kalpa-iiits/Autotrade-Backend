"""Paper-trading broker: simulated prices and fills, no real money involved."""

import asyncio
import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.brokers.base import Broker, Candle, Depth, Instrument, OrderError, Resolution
from app.orders.schemas import (
    Order,
    OrderStatus,
    OrderType,
    PlaceOrder,
    Position,
    Side,
    _now,
)

SEED_PRICES = {
    "NSE:RELIANCE-EQ": 2900.0,
    "NSE:TCS-EQ": 4100.0,
    "NSE:INFY-EQ": 1850.0,
    "NSE:HDFCBANK-EQ": 1700.0,
    "NSE:SBIN-EQ": 820.0,
    "NSE:NIFTY50-INDEX": 25800.0,  # for backtests; an index, so the live brokers won't take orders on it
}


class PaperBroker(Broker):
    def __init__(self, starting_cash: float, tick_interval: float = 1.0) -> None:
        super().__init__()
        self.cash = starting_cash
        self.tick_interval = tick_interval
        self.prices: dict[str, float] = dict(SEED_PRICES)
        self.orders: dict[str, Order] = {}
        self.positions: dict[str, Position] = {}
        self._triggered: set[str] = set()  # SL / SL-M orders whose trigger price was reached
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._price_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def get_instruments(self, exchange: str = "NSE") -> list[Instrument]:
        if exchange.upper() != "NSE":
            raise OrderError(f"Paper instrument search does not support {exchange!r}")
        return [
            Instrument(
                symbol=symbol,
                ticker=symbol.removeprefix("NSE:").removesuffix("-EQ").removesuffix("-INDEX"),
                name=symbol.removeprefix("NSE:"),
                kind="index" if symbol.endswith("-INDEX") else "stock",
            )
            for symbol in sorted(self.prices)
        ]

    # ---------- price simulation ----------

    async def _price_loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_interval)
            for symbol, price in self.prices.items():
                new_price = round(max(0.05, price * (1 + random.gauss(0, 0.001))), 2)
                self.prices[symbol] = new_price
                await self._emit_tick(symbol, new_price)
            await self._match_limit_orders()

    async def _match_limit_orders(self) -> None:
        async with self._lock:
            for order in list(self.orders.values()):
                if order.status != OrderStatus.OPEN:
                    continue
                price = self.prices[order.symbol]
                if order.order_type in (OrderType.SL, OrderType.SL_M) and order.id not in self._triggered:
                    # Stops trigger when the price reaches trigger_price from the safe side.
                    hit = (order.side == Side.BUY and price >= order.trigger_price) or (
                        order.side == Side.SELL and price <= order.trigger_price
                    )
                    if not hit:
                        continue
                    self._triggered.add(order.id)
                    if order.order_type == OrderType.SL_M:
                        self._fill(order, price)
                        await self._emit_order(order)
                        continue
                if order.limit_price is None:
                    continue
                crossed = (order.side == Side.BUY and price <= order.limit_price) or (
                    order.side == Side.SELL and price >= order.limit_price
                )
                if crossed:
                    self._fill(order, order.limit_price)
                    await self._emit_order(order)

    # ---------- order handling ----------

    def _fill(self, order: Order, price: float) -> None:
        """Apply a fill to cash and positions. Caller holds the lock and has validated funds."""
        signed_qty = order.quantity if order.side == Side.BUY else -order.quantity
        self.cash -= signed_qty * price

        pos = self.positions.get(order.symbol)
        if pos is None:
            self.positions[order.symbol] = Position(
                symbol=order.symbol, quantity=signed_qty, avg_price=price
            )
        else:
            new_qty = pos.quantity + signed_qty
            if new_qty == 0:
                del self.positions[order.symbol]
            else:
                # Average price only moves when adding to the position.
                if (pos.quantity > 0) == (signed_qty > 0):
                    pos.avg_price = round(
                        (pos.avg_price * pos.quantity + price * signed_qty) / new_qty, 2
                    )
                elif (pos.quantity > 0) != (new_qty > 0):
                    pos.avg_price = price  # flipped from long to short or vice versa
                pos.quantity = new_qty

        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.filled_price = price
        order.updated_at = _now()

    async def place_order(self, req: PlaceOrder) -> Order:
        if req.symbol not in self.prices:
            raise OrderError(f"Unknown symbol {req.symbol!r}")

        order = Order(
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            quantity=req.quantity,
            order_type=req.order_type,
            limit_price=req.limit_price,
            trigger_price=req.trigger_price,
            product=req.product,
        )
        async with self._lock:
            ref_price = {
                OrderType.MARKET: self.prices[req.symbol],
                OrderType.SL_M: req.trigger_price,
            }.get(req.order_type, req.limit_price)
            if req.side == Side.BUY and ref_price * req.quantity > self.cash:
                order.status = OrderStatus.REJECTED
                order.reason = "Insufficient funds"
            elif req.order_type == OrderType.MARKET:
                self._fill(order, self.prices[req.symbol])
            elif req.order_type == OrderType.LIMIT and (
                (req.side == Side.BUY and self.prices[req.symbol] <= req.limit_price)
                or (req.side == Side.SELL and self.prices[req.symbol] >= req.limit_price)
            ):
                self._fill(order, self.prices[req.symbol])  # marketable: fills now, at the market
            self.orders[order.id] = order
        await self._emit_order(order)
        return order

    async def get_depth(self, symbol: str) -> Depth | None:
        """No order book here: both sides at the last price."""
        price = self.prices.get(symbol)
        return Depth(bid=price, ask=price) if price is not None else None

    async def modify_order(self, order_id: str, limit_price: float) -> Order:
        async with self._lock:
            order = self.orders.get(order_id)
            if order is None:
                raise OrderError(f"Order {order_id!r} not found")
            if order.status != OrderStatus.OPEN or order.order_type != OrderType.LIMIT:
                raise OrderError(f"Order {order_id!r} is not an open limit order, cannot modify")
            order.limit_price = limit_price
            order.updated_at = _now()
            price = self.prices[order.symbol]
            if (order.side == Side.BUY and price <= limit_price) or (order.side == Side.SELL and price >= limit_price):
                self._fill(order, price)  # now marketable: fills at the market
        await self._emit_order(order)
        return order

    async def cancel_order(self, order_id: str) -> Order:
        async with self._lock:
            order = self.orders.get(order_id)
            if order is None:
                raise OrderError(f"Order {order_id!r} not found")
            if order.status != OrderStatus.OPEN:
                raise OrderError(f"Order {order_id!r} is {order.status.value}, cannot cancel")
            order.status = OrderStatus.CANCELLED
            order.updated_at = _now()
        await self._emit_order(order)
        return order

    async def get_orders(self) -> list[Order]:
        return list(self.orders.values())

    async def get_positions(self) -> list[Position]:
        return list(self.positions.values())

    async def get_funds(self) -> dict:
        return {"cash": round(self.cash, 2)}

    async def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        return {s: self.prices[s] for s in symbols if s in self.prices}

    async def get_history(
        self, symbol: str, resolution: Resolution, start: date, end: date
    ) -> list[Candle]:
        """Made-up candles around the current price (weekdays, 09:15-15:30 IST), stable for
        a given symbol and day so repeated calls agree."""
        if symbol not in self.prices:
            raise OrderError(f"Unknown symbol {symbol!r}")
        ist, price, out = ZoneInfo("Asia/Kolkata"), self.prices[symbol], []
        day = start
        while day <= end:
            if day.weekday() < 5:
                rng = random.Random(f"{symbol}:{day.isoformat()}")
                if resolution == "D":
                    width = price * rng.uniform(0.006, 0.016)
                    low = price - width * rng.random()
                    o, c = (low + width * rng.random() for _ in range(2))
                    out.append(Candle(datetime.combine(day, time(9, 15), ist),
                                      round(o, 2), round(low + width, 2), round(low, 2), round(c, 2)))
                else:
                    p = price
                    for minute in range(375):
                        start_at = datetime.combine(day, time(9, 15), ist) + timedelta(minutes=minute)
                        o, p = p, p * (1 + rng.gauss(0, 0.0005))
                        out.append(Candle(start_at, round(o, 2), round(max(o, p) * 1.0002, 2),
                                          round(min(o, p) * 0.9998, 2), round(p, 2)))
            day += timedelta(days=1)
        return out
