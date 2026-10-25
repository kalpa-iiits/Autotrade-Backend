"""Strategies app: run the strategies declared in app/strategies/deployments.py.

Code defines each strategy's logic, parameters, size, limits and mode (a "deployment"). In
the app you choose where it runs: add instances of a deployment, each on its own symbol and
timeframe, change those while an instance is stopped, start/stop them, and see their signals.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.auth.dependencies import require_user
from app.brokers import OrderError
from app.core import db
from app.strategies.models import StrategyInstance, StrategySignal
from app.strategies.runner import StrategyError, StrategyRunner, signal_dict

router = APIRouter(prefix="/strategies", tags=["strategies"], dependencies=[Depends(require_user)])
IST = ZoneInfo("Asia/Kolkata")
HIDDEN = ("archived", "removed")


class InstanceCreate(BaseModel):
    deployment_key: str
    symbol: str = Field(min_length=1, max_length=64)
    timeframe: str


class InstanceUpdate(BaseModel):
    symbol: str | None = Field(default=None, min_length=1, max_length=64)
    timeframe: str | None = None


def _runner(request: Request) -> StrategyRunner:
    return request.app.state.strategy_runner


def _instance_dict(row: StrategyInstance, runner: StrategyRunner) -> dict:
    live = runner.running.get(row.id)
    dep = runner.deployments.get(row.deployment_key)
    return {
        "id": row.id, "deployment_key": row.deployment_key, "strategy": row.strategy,
        "description": dep.strategy.description if dep else None,
        "label": row.label, "symbol": row.symbol, "timeframe": row.timeframe,
        "timeframes": list(dep.strategy.timeframes) if dep else [],
        "params": row.params, "mode": row.mode, "status": row.status,
        "quantity": row.quantity, "max_order_qty": row.max_order_qty,
        "max_position": row.max_position, "max_orders_per_day": row.max_orders_per_day,
        "last_error": row.last_error,
        "position": live.position if live else None,
        "last_price": live.last_price if live else None,
        "orders_today": live.orders_today if live else None,
        "pending_order_id": live.pending_order_id if live else None,
    }


async def _get(instance_id: int) -> StrategyInstance:
    async with db.session() as s:
        row = await s.get(StrategyInstance, instance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such strategy instance")
    return row


def _check_timeframe(runner: StrategyRunner, deployment_key: str, timeframe: str) -> None:
    dep = runner.deployments.get(deployment_key)
    if dep is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No deployment {deployment_key!r} in code")
    try:
        dep.check_timeframe(timeframe)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))


async def _check_symbol(runner: StrategyRunner, symbol: str) -> str | None:
    """Ask the broker whether it knows the symbol. Returns a warning if it can't be checked."""
    if not runner.brokers.connected:
        return f"Broker not connected; could not check {symbol!r}"
    today = datetime.now(IST).date()
    try:
        await runner.brokers.broker.get_history(symbol, "D", today - timedelta(days=7), today)
    except OrderError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{symbol}: {e}")
    return None


async def _conflict(deployment_key: str, symbol: str, timeframe: str, exclude_id: int | None = None):
    async with db.session() as s:
        row = await s.scalar(select(StrategyInstance).where(
            StrategyInstance.deployment_key == deployment_key,
            StrategyInstance.symbol == symbol,
            StrategyInstance.timeframe == timeframe,
        ))
    return row if row is not None and row.id != exclude_id else None


@router.get("")
async def list_strategies(request: Request, include_hidden: bool = False):
    query = select(StrategyInstance).order_by(StrategyInstance.deployment_key, StrategyInstance.id)
    if not include_hidden:
        query = query.where(StrategyInstance.status.not_in(HIDDEN))
    async with db.session() as s:
        rows = list(await s.scalars(query))
    runner = _runner(request)
    return {
        "live_trading_enabled": runner.settings.strategies_live_enabled,
        # What code defines: for the "add an instance" form.
        "deployments": [
            {"key": d.key, "strategy": d.strategy.name, "description": d.strategy.description,
             "mode": d.mode, "default_symbol": d.symbol, "default_timeframe": d.timeframe,
             "timeframes": list(d.strategy.timeframes)}
            for d in runner.deployments.values()
        ],
        "strategies": [_instance_dict(r, runner) for r in rows],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_instance(request: Request, body: InstanceCreate):
    """Run a code-defined deployment on another symbol/timeframe. Starts stopped."""
    runner = _runner(request)
    symbol = body.symbol.strip()
    _check_timeframe(runner, body.deployment_key, body.timeframe)
    warning = await _check_symbol(runner, symbol)

    existing = await _conflict(body.deployment_key, symbol, body.timeframe)
    if existing is not None and existing.status != "archived":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"{body.deployment_key} already runs on {symbol} {body.timeframe} (#{existing.id})")
    dep = runner.deployments[body.deployment_key]
    async with db.session() as s, s.begin():
        if existing is not None:  # bring the archived one back, with its history
            row = await s.get(StrategyInstance, existing.id)
            row.status, row.last_error = "stopped", None
        else:
            row = StrategyInstance(deployment_key=dep.key, symbol=symbol, timeframe=body.timeframe,
                                   status="stopped", **dep.settings())
            s.add(row)
    return _instance_dict(row, runner) | {"warning": warning}


@router.get("/{instance_id}")
async def get_instance(request: Request, instance_id: int):
    return _instance_dict(await _get(instance_id), _runner(request))


@router.patch("/{instance_id}")
async def update_instance(request: Request, instance_id: int, body: InstanceUpdate):
    """Change an instance's symbol and/or timeframe. It must be stopped."""
    runner = _runner(request)
    row = await _get(instance_id)
    if instance_id in runner.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Stop the strategy before changing it")
    if row.status in HIDDEN:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This instance is {row.status}")
    symbol = (body.symbol or row.symbol).strip()
    timeframe = body.timeframe or row.timeframe
    _check_timeframe(runner, row.deployment_key, timeframe)
    warning = await _check_symbol(runner, symbol) if symbol != row.symbol else None
    if (other := await _conflict(row.deployment_key, symbol, timeframe, exclude_id=instance_id)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Instance #{other.id} ({other.status}) already uses {symbol} {timeframe}")
    async with db.session() as s, s.begin():
        row = await s.get(StrategyInstance, instance_id)
        if (row.symbol, row.timeframe) != (symbol, timeframe):
            # Its position history belongs to the old symbol: a different instrument now.
            row.symbol, row.timeframe, row.last_error = symbol, timeframe, None
    return _instance_dict(row, runner) | {"warning": warning}


@router.delete("/{instance_id}")
async def archive_instance(request: Request, instance_id: int):
    """Remove an instance from the list. Its signal history is kept; adding the same
    symbol and timeframe again brings it back."""
    runner = _runner(request)
    await _get(instance_id)
    if instance_id in runner.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Stop the strategy before removing it")
    async with db.session() as s, s.begin():
        row = await s.get(StrategyInstance, instance_id)
        row.status = "archived"
    return _instance_dict(row, runner)


@router.post("/{instance_id}/start")
async def start_instance(request: Request, instance_id: int):
    runner = _runner(request)
    await _get(instance_id)
    try:
        await runner.start(instance_id)
    except StrategyError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return _instance_dict(await _get(instance_id), runner)


@router.post("/{instance_id}/stop")
async def stop_instance(request: Request, instance_id: int):
    runner = _runner(request)
    row = await _get(instance_id)
    if row.status not in HIDDEN:
        await runner.stop(instance_id)
    return _instance_dict(await _get(instance_id), runner)


@router.post("/stop-all")
async def stop_all(request: Request):
    """Kill switch: stop every running strategy. Open orders are left as they are."""
    return {"stopped": await _runner(request).stop_all()}


@router.get("/{instance_id}/signals")
async def list_signals(instance_id: int, limit: int = Query(default=100, ge=1, le=1000)):
    await _get(instance_id)
    async with db.session() as s:
        rows = await s.scalars(
            select(StrategySignal).where(StrategySignal.instance_id == instance_id)
            .order_by(StrategySignal.id.desc()).limit(limit)
        )
        return [signal_dict(r) for r in rows]
