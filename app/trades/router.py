"""Trades app: open and closed round-trip positions with P&L, built by app.trades.ledger."""

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import case, func, select

from app.auth.dependencies import require_user
from app.core import db
from app.core.db import to_decimal
from app.trades.ledger import CLOSED, LONG, OPEN
from app.trades.models import Fill, Position, PositionFill

router = APIRouter(prefix="/trades", tags=["trades"], dependencies=[Depends(require_user)])


def _num(value: Decimal | None) -> float | None:
    """Exact Decimal in the database, a JSON number on the wire."""
    return None if value is None else float(value)


def _unrealized(p: Position, last_price: float | None) -> Decimal | None:
    # Only known while someone is subscribed to the symbol's ticks.
    if last_price is None:
        return None
    sign = 1 if p.direction == LONG else -1
    return to_decimal(sign * (to_decimal(last_price) - p.avg_entry_price) * p.quantity)


def _position(p: Position, last_price: float | None = None) -> dict:
    out = {
        "id": p.id, "broker": p.broker, "symbol": p.symbol, "direction": p.direction,
        "status": p.status, "quantity": p.quantity, "entry_quantity": p.entry_quantity,
        "avg_entry_price": _num(p.avg_entry_price), "exit_quantity": p.exit_quantity,
        "avg_exit_price": _num(p.avg_exit_price), "realized_pnl": _num(p.realized_pnl),
        "opened_at": p.opened_at.isoformat(),
        "closed_at": p.closed_at.isoformat() if p.closed_at else None,
    }
    if p.status == OPEN:
        out["last_price"] = last_price
        out["unrealized_pnl"] = _num(_unrealized(p, last_price))
    return out


def _filters(query, broker: str | None, symbol: str | None):
    if broker:
        query = query.where(Position.broker == broker)
    if symbol:
        query = query.where(Position.symbol == symbol)
    return query


@router.get("/open")
async def open_trades(request: Request, broker: str | None = None, symbol: str | None = None):
    query = _filters(select(Position).where(Position.status == OPEN), broker, symbol)
    async with db.session() as s:
        rows = list(await s.scalars(query.order_by(Position.opened_at.desc())))
    prices = request.app.state.ledger.last_prices
    known = [u for p in rows if (u := _unrealized(p, prices.get(p.symbol))) is not None]
    return {
        "positions": [_position(p, prices.get(p.symbol)) for p in rows],
        "totals": {
            "count": len(rows),
            "unrealized_pnl": _num(sum(known)) if known else None,
            "unpriced": len(rows) - len(known),  # positions with no recent tick
            "realized_pnl": _num(sum((p.realized_pnl for p in rows), Decimal(0))),  # partial exits
        },
    }


@router.get("/closed")
async def closed_trades(
    broker: str | None = None,
    symbol: str | None = None,
    since: datetime | None = Query(default=None, description="closed at or after (ISO 8601)"),
    until: datetime | None = Query(default=None, description="closed before (ISO 8601)"),
    limit: int = Query(default=100, ge=1, le=1000),
):
    query = _filters(select(Position).where(Position.status == CLOSED), broker, symbol)
    if since:
        query = query.where(Position.closed_at >= since)
    if until:
        query = query.where(Position.closed_at < until)

    # Totals cover every match, not just the page returned.
    stats = query.with_only_columns(
        func.count(),
        func.coalesce(func.sum(Position.realized_pnl), Decimal(0)),
        func.coalesce(func.sum(case((Position.realized_pnl > 0, 1), else_=0)), 0),
        func.coalesce(func.sum(case((Position.realized_pnl < 0, 1), else_=0)), 0),
    )
    async with db.session() as s:
        rows = list(await s.scalars(query.order_by(Position.closed_at.desc()).limit(limit)))
        count, pnl, wins, losses = (await s.execute(stats)).one()
    return {
        "positions": [_position(p) for p in rows],
        "totals": {
            "count": count, "realized_pnl": _num(pnl), "wins": wins, "losses": losses,
            "win_rate": round(wins / count, 4) if count else None,
        },
    }


@router.get("/{position_id}")
async def trade_detail(request: Request, position_id: int):
    async with db.session() as s:
        p = await s.get(Position, position_id)
        if p is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such trade")
        fills = (await s.execute(
            select(Fill, PositionFill)
            .join(PositionFill, PositionFill.fill_id == Fill.id)
            .where(PositionFill.position_id == position_id)
            .order_by(Fill.filled_at, Fill.id)
        )).all()
    return {
        **_position(p, request.app.state.ledger.last_prices.get(p.symbol)),
        "fills": [
            {"fill_id": f.id, "order_id": f.order_id, "side": f.side, "role": link.role,
             "quantity": link.quantity, "price": _num(f.price), "filled_at": f.filled_at.isoformat()}
            for f, link in fills
        ],
    }
