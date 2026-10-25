"""Trades app: open and closed round-trip positions with P&L, built by app.trades.ledger.
Each user sees the trades of their own broker accounts (all of them, or ?account_id=)."""

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import case, func, select

from app.accounts.store import account_ids
from app.auth.dependencies import require_user
from app.auth.models import User
from app.core import db
from app.core.db import to_decimal
from app.strategies.models import StrategyInstance, StrategySignal
from app.trades.ledger import CLOSED, LONG, OPEN
from app.trades.models import Fill, Position, PositionFill

router = APIRouter(prefix="/trades", tags=["trades"])


def _num(value: Decimal | None) -> float | None:
    """Exact Decimal in the database, a JSON number on the wire."""
    return None if value is None else float(value)


def _unrealized(p: Position, last_price: float | None) -> Decimal | None:
    # Only known while someone is subscribed to the symbol's ticks.
    if last_price is None:
        return None
    sign = 1 if p.direction == LONG else -1
    return to_decimal(sign * (to_decimal(last_price) - p.avg_entry_price) * p.quantity)


async def _strategies(s, positions: list[Position]) -> dict[int, str]:
    """position id -> label of the strategy instance whose order opened it. Positions opened by
    hand (or by a strategy since removed) are left out."""
    if not positions:
        return {}
    rows = await s.execute(
        select(PositionFill.position_id, StrategyInstance.label)
        .join(Fill, Fill.id == PositionFill.fill_id)
        .join(StrategySignal, StrategySignal.order_id == Fill.order_id)
        .join(StrategyInstance, (StrategyInstance.id == StrategySignal.instance_id)
              & (StrategyInstance.account_id == Fill.account_id))
        .where(PositionFill.position_id.in_([p.id for p in positions]), PositionFill.role == "entry")
    )
    return {position_id: label for position_id, label in rows}


def _position(p: Position, last_price: float | None = None, strategy: str | None = None) -> dict:
    out = {
        "id": p.id, "account_id": p.account_id, "broker": p.broker, "symbol": p.symbol, "direction": p.direction,
        "status": p.status, "quantity": p.quantity, "entry_quantity": p.entry_quantity,
        "avg_entry_price": _num(p.avg_entry_price), "exit_quantity": p.exit_quantity,
        "avg_exit_price": _num(p.avg_exit_price), "realized_pnl": _num(p.realized_pnl),
        "opened_at": p.opened_at.isoformat(),
        "closed_at": p.closed_at.isoformat() if p.closed_at else None,
        "strategy": strategy,  # None: placed by hand
    }
    if p.status == OPEN:
        out["last_price"] = last_price
        out["unrealized_pnl"] = _num(_unrealized(p, last_price))
    return out


async def _filters(query, user: User, account_id: int | None, symbol: str | None):
    """Only the user's accounts (or one of them). Someone else's account id matches nothing."""
    mine = await account_ids(user.id)
    if account_id is not None:
        mine = [account_id] if account_id in mine else []
    query = query.where(Position.account_id.in_(mine))
    if symbol:
        query = query.where(Position.symbol == symbol)
    return query


def _last_price(request: Request, p: Position) -> float | None:
    """Latest tick on the position's account (only while someone watches the symbol there)."""
    runtime = request.app.state.accounts.get(p.account_id)
    return runtime.ledger.last_prices.get(p.symbol) if runtime else None


@router.get("/open")
async def open_trades(request: Request, account_id: int | None = None, symbol: str | None = None,
                      user: User = Depends(require_user)):
    query = await _filters(select(Position).where(Position.status == OPEN), user, account_id, symbol)
    async with db.session() as s:
        rows = list(await s.scalars(query.order_by(Position.opened_at.desc())))
        strategies = await _strategies(s, rows)
    known = [u for p in rows if (u := _unrealized(p, _last_price(request, p))) is not None]
    return {
        "positions": [_position(p, _last_price(request, p), strategies.get(p.id)) for p in rows],
        "totals": {
            "count": len(rows),
            "unrealized_pnl": _num(sum(known)) if known else None,
            "unpriced": len(rows) - len(known),  # positions with no recent tick
            "realized_pnl": _num(sum((p.realized_pnl for p in rows), Decimal(0))),  # partial exits
        },
    }


@router.get("/closed")
async def closed_trades(
    account_id: int | None = None,
    symbol: str | None = None,
    since: datetime | None = Query(default=None, description="closed at or after (ISO 8601)"),
    until: datetime | None = Query(default=None, description="closed before (ISO 8601)"),
    limit: int = Query(default=100, ge=1, le=1000),
    user: User = Depends(require_user),
):
    query = await _filters(select(Position).where(Position.status == CLOSED), user, account_id, symbol)
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
        strategies = await _strategies(s, rows)
    return {
        "positions": [_position(p, strategy=strategies.get(p.id)) for p in rows],
        "totals": {
            "count": count, "realized_pnl": _num(pnl), "wins": wins, "losses": losses,
            "win_rate": round(wins / count, 4) if count else None,
        },
    }


@router.get("/{position_id}")
async def trade_detail(request: Request, position_id: int, user: User = Depends(require_user)):
    async with db.session() as s:
        p = await s.get(Position, position_id)
        if p is None or p.account_id not in await account_ids(user.id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such trade")
        fills = (await s.execute(
            select(Fill, PositionFill)
            .join(PositionFill, PositionFill.fill_id == Fill.id)
            .where(PositionFill.position_id == position_id)
            .order_by(Fill.filled_at, Fill.id)
        )).all()
        strategies = await _strategies(s, [p])
    return {
        **_position(p, _last_price(request, p), strategies.get(p.id)),
        "fills": [
            {"fill_id": f.id, "order_id": f.order_id, "side": f.side, "role": link.role,
             "quantity": link.quantity, "price": _num(f.price), "filled_at": f.filled_at.isoformat()}
            for f, link in fills
        ],
    }
