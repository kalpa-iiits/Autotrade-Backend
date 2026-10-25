"""Tickers app: live prices on /api/ws/tickers (subscribe, receive `tick` messages), plus REST
market data: quotes and candles (for charts)."""

from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, status
from pydantic import TypeAdapter

from app.auth.dependencies import require_user
from app.core.http import broker_errors, broker_or_503

from app.core.connections import ConnectionManager
from app.core.ws import serve
from app.tickers.schemas import Subscribe, TickerMessage

router = APIRouter(tags=["tickers"])
_adapter = TypeAdapter(TickerMessage)


async def publish_tick(connections: ConnectionManager, symbol: str, price: float) -> None:
    await connections.publish(symbol, {"type": "tick", "symbol": symbol, "ltp": price})


@router.websocket("/ws/tickers")
async def tickers_socket(ws: WebSocket):
    connections: ConnectionManager = ws.app.state.ticker_connections
    brokers = ws.app.state.brokers

    async def handle(msg: Subscribe) -> dict:
        subs = connections.subscriptions[ws]
        if msg.action == "subscribe":
            subs.update(msg.symbols)
            await brokers.require().subscribe(msg.symbols)
        else:
            subs.difference_update(msg.symbols)
            unwatched = set(msg.symbols) - brokers.watched_symbols()
            if unwatched and brokers.connected:
                await brokers.broker.unsubscribe(sorted(unwatched))
        return {"symbols": sorted(subs)}

    async def on_disconnect(ws: WebSocket) -> None:
        # Drop upstream subscriptions nobody else is still watching.
        unwatched = connections.remove(ws) - brokers.watched_symbols()
        if unwatched and brokers.connected:
            await brokers.broker.unsubscribe(sorted(unwatched))

    await serve(ws, _adapter, handle, connections, on_disconnect=on_disconnect)



# ---------- REST market data ----------

market = APIRouter(prefix="/market", tags=["market"], dependencies=[Depends(require_user)])
IST = ZoneInfo("Asia/Kolkata")
MAX_DAYS = {"1": 60, "D": 1000}  # range limits per resolution


@market.get("/quotes")
async def quotes(request: Request, symbols: str = Query(description="Comma-separated, e.g. NSE:SBIN-EQ,NSE:TCS-EQ")):
    """Last traded price per symbol: {"NSE:SBIN-EQ": 812.3, ...}. Unknown symbols are left out."""
    wanted = [s.strip() for s in symbols.split(",") if s.strip()]
    if not wanted or len(wanted) > 50:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Give 1 to 50 symbols")
    async with broker_errors():
        return await broker_or_503(request).get_quotes(wanted)


@market.get("/candles")
async def candles(
    request: Request,
    symbol: str,
    resolution: Literal["1", "D"] = Query("1", description='"1" = 1-minute, "D" = daily'),
    start: date | None = Query(None, description="First day (IST); default: a few days back"),
    end: date | None = Query(None, description="Last day (IST), inclusive; default: today"),
):
    """OHLCV candles, oldest first: [{"time": ISO8601, "open", "high", "low", "close", "volume"}]."""
    end = end or datetime.now(IST).date()
    start = start or end - timedelta(days=5 if resolution == "1" else 365)
    if start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start is after end")
    if (end - start).days > MAX_DAYS[resolution]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"At most {MAX_DAYS[resolution]} days for resolution {resolution}")
    async with broker_errors():
        rows = await broker_or_503(request).get_history(symbol, resolution, start, end)
    return [
        {"time": c.start.isoformat(), "open": c.open, "high": c.high, "low": c.low,
         "close": c.close, "volume": c.volume}
        for c in rows
    ]


router.include_router(market)
