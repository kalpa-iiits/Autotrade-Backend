"""Tickers app, per broker account: live prices on the account's /ws/tickers (subscribe,
receive `tick` messages), plus REST market data from that account's broker: quotes and candles."""

from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
from pydantic import TypeAdapter

from app.accounts.dependencies import account_runtime
from app.accounts.registry import AccountRuntime
from app.core.http import broker_errors, broker_or_503
from app.core.ws import serve
from app.tickers.schemas import Subscribe, TickerMessage

router = APIRouter(tags=["tickers"])
_adapter = TypeAdapter(TickerMessage)


@router.websocket("/accounts/{account_id}/ws/tickers")
async def tickers_socket(ws: WebSocket, account_id: int):
    runtime = ws.app.state.accounts.get(account_id)

    async def handle(msg: Subscribe) -> dict:
        connections, service = runtime.tickers_ws, runtime.service
        subs = connections.subscriptions[ws]
        if msg.action == "subscribe":
            subs.update(msg.symbols)
            await service.require().subscribe(msg.symbols)
        else:
            subs.difference_update(msg.symbols)
            unwatched = set(msg.symbols) - service.watched_symbols()
            if unwatched and service.connected:
                await service.broker.unsubscribe(sorted(unwatched))
        return {"symbols": sorted(subs)}

    async def on_disconnect(ws: WebSocket) -> None:
        # Drop upstream subscriptions nobody else on this account is still watching.
        service = runtime.service
        unwatched = runtime.tickers_ws.remove(ws) - service.watched_symbols()
        if unwatched and service.connected:
            await service.broker.unsubscribe(sorted(unwatched))

    await serve(ws, _adapter, handle, runtime, lambda r: r.tickers_ws, on_disconnect=on_disconnect)


# ---------- REST market data ----------

market = APIRouter(prefix="/accounts/{account_id}/market", tags=["market"])
IST = ZoneInfo("Asia/Kolkata")
MAX_DAYS = {"1": 60, "D": 1000}  # range limits per resolution


@market.get("/quotes")
async def quotes(
    symbols: str = Query(description="Comma-separated, e.g. NSE:SBIN-EQ,NSE:TCS-EQ"),
    runtime: AccountRuntime = Depends(account_runtime),
):
    """Last traded price per symbol: {"NSE:SBIN-EQ": 812.3, ...}. Unknown symbols are left out."""
    wanted = [s.strip() for s in symbols.split(",") if s.strip()]
    if not wanted or len(wanted) > 50:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Give 1 to 50 symbols")
    async with broker_errors():
        return await broker_or_503(runtime.service).get_quotes(wanted)


@market.get("/candles")
async def candles(
    symbol: str,
    resolution: Literal["1", "D"] = Query("1", description='"1" = 1-minute, "D" = daily'),
    start: date | None = Query(None, description="First day (IST); default: a few days back"),
    end: date | None = Query(None, description="Last day (IST), inclusive; default: today"),
    runtime: AccountRuntime = Depends(account_runtime),
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
        rows = await broker_or_503(runtime.service).get_history(symbol, resolution, start, end)
    return [
        {"time": c.start.isoformat(), "open": c.open, "high": c.high, "low": c.low,
         "close": c.close, "volume": c.volume}
        for c in rows
    ]


router.include_router(market)
