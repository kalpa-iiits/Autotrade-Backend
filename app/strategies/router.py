"""Strategies app: run the strategies declared in app/strategies/deployments.py on your own
broker accounts.

Code defines each strategy's logic, parameters, size, limits and mode (a "deployment"): the
catalog. Each user chooses where it runs: add instances of a deployment on one of their broker
accounts, each with its own symbol and timeframe, change those while an instance is stopped,
start/stop them, and see their signals. Users only ever see and touch their own instances.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import select

from app.accounts import store
from app.accounts.dependencies import owns
from app.auth.dependencies import require_user
from app.auth.models import User
from app.brokers import OrderError
from app.core import db
from app.core.http import broker_errors, broker_or_503
from app.strategies.backtest import DEFAULT_CAPITAL, DEFAULT_SIZE_PCT, DEPLOYMENT_SIZE, Backtester, BacktestError, Run
from app.strategies.deployment import Deployment
from app.strategies.instruments import search_instruments
from app.strategies.models import StrategyInstance, StrategySignal
from app.strategies.runner import StrategyError, StrategyRunner, signal_dict, sized_quantity

router = APIRouter(prefix="/strategies", tags=["strategies"])
IST = ZoneInfo("Asia/Kolkata")
HIDDEN = ("archived", "removed")


class Sizing(BaseModel):
    """Position sizing: `fixed_quantity` units per trade (None = the strategy's default), or
    `capital_per_trade` rupees' worth at entry. The strategy's code limits cap both."""
    size_mode: Literal["fixed", "capital"] | None = None
    fixed_quantity: int | None = Field(default=None, gt=0)
    capital_per_trade: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)


class InstanceCreate(Sizing):
    deployment_key: str
    account_id: int
    symbol: str = Field(min_length=1, max_length=64)
    timeframe: str


class InstanceUpdate(Sizing):
    symbol: str | None = Field(default=None, min_length=1, max_length=64)
    timeframe: str | None = None


def _sizing(body: Sizing, current: StrategyInstance | None, limits: dict, dep: Deployment | None) -> dict:
    """The sizing columns after applying `body` to `current` (None: a new instance), checked
    against the strategy's limits."""
    mode = body.size_mode or (current.size_mode if current else "fixed")
    if mode == "capital" and dep is not None and not dep.strategy.capital_sizing:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "This strategy is sized by fixed quantity only (it trades option lots)")
    fields = body.model_fields_set
    fixed = body.fixed_quantity if "fixed_quantity" in fields else (current.fixed_quantity if current else None)
    capital = body.capital_per_trade if "capital_per_trade" in fields else (current.capital_per_trade if current else None)
    cap = min(limits["max_order_qty"], limits["max_position"])
    if mode == "fixed" and fixed is not None and fixed > cap:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"This strategy allows at most {cap} units per trade")
    if mode == "capital" and not capital:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Capital sizing needs an amount per trade")
    return {"size_mode": mode, "fixed_quantity": fixed, "capital_per_trade": capital}


def _runner(request: Request) -> StrategyRunner:
    return request.app.state.strategy_runner


def _instance_dict(row: StrategyInstance, runner: StrategyRunner) -> dict:
    live = runner.running.get(row.id)
    dep = runner.deployments.get(row.deployment_key)
    return {
        "id": row.id, "account_id": row.account_id, "deployment_key": row.deployment_key,
        "strategy": row.strategy, "description": dep.strategy.description if dep else None,
        "label": row.label, "symbol": row.symbol, "timeframe": row.timeframe,
        "timeframes": list(dep.strategy.timeframes) if dep else [],
        "capital_sizing": dep.strategy.capital_sizing if dep else True,
        "params": row.params, "mode": row.mode, "status": row.status,
        "quantity": row.quantity, "max_order_qty": row.max_order_qty,
        "size_mode": row.size_mode, "fixed_quantity": row.fixed_quantity,
        "capital_per_trade": float(row.capital_per_trade) if row.capital_per_trade is not None else None,
        # Units the next entry would trade at the last price (None while it has no price).
        "sized_quantity": (sized_quantity(row, live.last_price if live else None, live.lot_size if live else 1)
                           if row.size_mode != "capital" or (live and live.last_price) else None),
        "max_position": row.max_position, "max_orders_per_day": row.max_orders_per_day,
        "last_error": row.last_error,
        "position": live.position if live else None,
        "last_price": live.last_price if live else None,
        "orders_today": live.orders_today if live else None,
        "pending_order_id": live.pending_order_id if live else None,
        "dropped_ticks": live.dropped_ticks if live else None,
    }


async def _get(instance_id: int, user: User) -> StrategyInstance:
    """The user's instance, or 404 (also for someone else's)."""
    async with db.session() as s:
        row = await s.get(StrategyInstance, instance_id)
    if row is None or row.account_id not in await store.account_ids(user.id):
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


async def _check_symbol(runner: StrategyRunner, account_id: int, symbol: str) -> str | None:
    """Ask the account's broker whether it knows the symbol. Returns a warning if it can't be checked."""
    service = runner.service(account_id)
    if service is None or not service.connected:
        return f"Broker account not connected; could not check {symbol!r}"
    today = datetime.now(IST).date()
    try:
        await service.broker.get_history(symbol, "D", today - timedelta(days=7), today)
    except OrderError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{symbol}: {e}")
    return None


async def _conflict(account_id: int, deployment_key: str, symbol: str, timeframe: str,
                    exclude_id: int | None = None):
    async with db.session() as s:
        row = await s.scalar(select(StrategyInstance).where(
            StrategyInstance.account_id == account_id,
            StrategyInstance.deployment_key == deployment_key,
            StrategyInstance.symbol == symbol,
            StrategyInstance.timeframe == timeframe,
        ))
    return row if row is not None and row.id != exclude_id else None


@router.get("")
async def list_strategies(request: Request, include_hidden: bool = False, account_id: int | None = None,
                          user: User = Depends(require_user)):
    mine = await store.account_ids(user.id)
    if account_id is not None:
        mine = [account_id] if account_id in mine else []
    query = (select(StrategyInstance).where(StrategyInstance.account_id.in_(mine))
             .order_by(StrategyInstance.deployment_key, StrategyInstance.id))
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


# ---------- catalog: each strategy's page (description, rules, performance) ----------


def _catalog_dict(dep: Deployment) -> dict:
    strategy, values = dep.strategy, dep.params.model_dump(mode="json")
    params = [
        {"name": name, "title": f.title or name.replace("_", " ").capitalize(), "description": f.description,
         "value": values.get(name), "default": f.get_default(call_default_factory=True)}
        for name, f in strategy.Params.model_fields.items()
    ]
    for p in params:
        p["default"] = TypeAdapter(object).dump_python(p["default"], mode="json")
    return {
        "key": dep.key, "label": dep.label or dep.key, "strategy": strategy.name,
        "description": strategy.description, "tags": list(strategy.tags),
        "rules": strategy.rules(dep.params), "params": params,
        "mode": dep.mode, "default_symbol": dep.symbol, "default_timeframe": dep.timeframe,
        "timeframes": list(strategy.timeframes),
        "backtest_note": strategy.backtest_note, "capital_sizing": strategy.capital_sizing,
        "backtest_initial_capital": dep.backtest_initial_capital,
        "backtest_fixed_quantity": dep.backtest_fixed_quantity,
        "backtest_commission_per_unit": dep.backtest_commission_per_unit,
        **{k: v for k, v in dep.settings().items() if k in
           ("quantity", "max_order_qty", "max_position", "max_orders_per_day")},
    }


def _deployment(runner: StrategyRunner, key: str) -> Deployment:
    dep = runner.deployments.get(key)
    if dep is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No strategy {key!r}")
    return dep


async def _backtest(request: Request, user: User, key: str, account_id: int, symbol: str, timeframe: str,
                    start: date | None, end: date | None, capital: float, size_pct: float,
                    size_mode: str | None = None, quantity: int | None = None) -> Run:
    runner = _runner(request)
    dep = _deployment(runner, key)
    if not owns(user, await store.get_account(account_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such broker account")
    _check_timeframe(runner, key, timeframe)
    if size_mode == "fixed" and not quantity:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A fixed order size needs a quantity")
    fixed = quantity if size_mode == "fixed" else None if size_mode == "pct" else DEPLOYMENT_SIZE
    service = runner.service(account_id)
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Broker account not available")
    broker = broker_or_503(service)
    async with broker_errors():
        try:
            start, end = Backtester.check_range(start, end)
            return await runner.backtester.run(account_id, broker, dep, symbol.strip(), timeframe, start, end,
                                               capital, size_pct, fixed)
        except BacktestError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))


@router.get("/catalog")
async def list_catalog(request: Request, user: User = Depends(require_user)):
    """Every strategy defined in code, with its description, rules and settings."""
    return [_catalog_dict(d) for d in _runner(request).deployments.values()]


@router.get("/instruments/search")
async def instrument_search(request: Request, account_id: int, q: str = Query(min_length=1, max_length=64),
                            limit: int = Query(12, ge=1, le=30), user: User = Depends(require_user)):
    """NSE stocks and indexes in the selected broker's exact symbol format."""
    if not owns(user, await store.get_account(account_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such broker account")
    service = _runner(request).service(account_id)
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Broker account not available")
    broker = broker_or_503(service)
    async with broker_errors():
        instruments = await broker.get_instruments("NSE")
    return [item.__dict__ for item in search_instruments(instruments, q, limit)]


@router.get("/catalog/{key}")
async def get_catalog(request: Request, key: str, user: User = Depends(require_user)):
    return _catalog_dict(_deployment(_runner(request), key))


@router.get("/catalog/{key}/performance")
async def performance(request: Request, key: str, account_id: int, symbol: str = Query(min_length=1),
                      timeframe: str = Query(), start: date | None = None, end: date | None = None,
                      capital: float = Query(DEFAULT_CAPITAL, gt=0),
                      size_pct: float = Query(DEFAULT_SIZE_PCT, gt=0, le=100, description="Order size, % of equity"),
                      size_mode: Literal["pct", "fixed"] | None = Query(
                          None, description="pct: size_pct of equity; fixed: `quantity` units (default: the strategy's)"),
                      quantity: int | None = Query(None, gt=0, description="Units per entry when size_mode=fixed"),
                      user: User = Depends(require_user)):
    """Strategy tester: backtest on one timeframe over a date range (default: the last year) of
    the account's broker history. Entries use a fixed `quantity` or `size_pct` of equity
    (`size_mode`; by default the deployment's fixed tester quantity, if it has one). Summary for all / long / short trades, every simulated trade with
    its run-up and drawdown, and buy & hold for comparison. Cached for the IST day."""
    run = await _backtest(request, user, key, account_id, symbol, timeframe, start, end, capital, size_pct,
                          size_mode, quantity)
    return run.report()


@router.get("/catalog/{key}/performance/matrix")
async def performance_matrix(request: Request, key: str, account_id: int, symbol: str = Query(min_length=1),
                             start: date | None = None, end: date | None = None,
                             capital: float = Query(DEFAULT_CAPITAL, gt=0),
                             size_pct: float = Query(DEFAULT_SIZE_PCT, gt=0, le=100),
                             size_mode: Literal["pct", "fixed"] | None = None,
                             quantity: int | None = Query(None, gt=0),
                             user: User = Depends(require_user)):
    """The same range on every timeframe the strategy supports: summaries only."""
    dep = _deployment(_runner(request), key)
    out = {}
    for tf in dep.strategy.timeframes:
        run = await _backtest(request, user, key, account_id, symbol, tf, start, end, capital, size_pct,
                              size_mode, quantity)
        out[tf] = run.report()["summary"]["all"]
    return {"symbol": symbol.strip(), "timeframes": out}


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_instance(request: Request, body: InstanceCreate, user: User = Depends(require_user)):
    """Run a code-defined deployment on one of your broker accounts, symbol and timeframe.
    Starts stopped."""
    runner = _runner(request)
    if not owns(user, await store.get_account(body.account_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such broker account")
    symbol = body.symbol.strip()
    _check_timeframe(runner, body.deployment_key, body.timeframe)
    warning = await _check_symbol(runner, body.account_id, symbol)

    existing = await _conflict(body.account_id, body.deployment_key, symbol, body.timeframe)
    if existing is not None and existing.status != "archived":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"{body.deployment_key} already runs on {symbol} {body.timeframe} (#{existing.id})")
    dep = runner.deployments[body.deployment_key]
    sizing = _sizing(body, None, dep.settings(), dep)
    async with db.session() as s, s.begin():
        if existing is not None:  # bring the archived one back, with its history
            row = await s.get(StrategyInstance, existing.id)
            row.status, row.last_error = "stopped", None
            for k, v in sizing.items():
                setattr(row, k, v)
        else:
            row = StrategyInstance(account_id=body.account_id, deployment_key=dep.key, symbol=symbol,
                                   timeframe=body.timeframe, status="stopped", **dep.settings(), **sizing)
            s.add(row)
    return _instance_dict(row, runner) | {"warning": warning}


@router.post("/stop-all")
async def stop_all(request: Request, user: User = Depends(require_user)):
    """Kill switch: stop every running strategy on your accounts. Open orders are left as they are."""
    return {"stopped": await _runner(request).stop_all(set(await store.account_ids(user.id)))}


@router.get("/{instance_id}")
async def get_instance(request: Request, instance_id: int, user: User = Depends(require_user)):
    return _instance_dict(await _get(instance_id, user), _runner(request))


@router.patch("/{instance_id}")
async def update_instance(request: Request, instance_id: int, body: InstanceUpdate,
                          user: User = Depends(require_user)):
    """Change an instance's symbol, timeframe and/or position sizing. It must be stopped."""
    runner = _runner(request)
    row = await _get(instance_id, user)
    if instance_id in runner.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Stop the strategy before changing it")
    if row.status in HIDDEN:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This instance is {row.status}")
    symbol = (body.symbol or row.symbol).strip()
    timeframe = body.timeframe or row.timeframe
    _check_timeframe(runner, row.deployment_key, timeframe)
    warning = await _check_symbol(runner, row.account_id, symbol) if symbol != row.symbol else None
    other = await _conflict(row.account_id, row.deployment_key, symbol, timeframe, exclude_id=instance_id)
    if other is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Instance #{other.id} ({other.status}) already uses {symbol} {timeframe}")
    sizing = _sizing(body, row, {"max_order_qty": row.max_order_qty, "max_position": row.max_position},
                     runner.deployments.get(row.deployment_key))
    async with db.session() as s, s.begin():
        row = await s.get(StrategyInstance, instance_id)
        for k, v in sizing.items():
            setattr(row, k, v)
        if (row.symbol, row.timeframe) != (symbol, timeframe):
            # Its position history belongs to the old symbol: a different instrument now.
            row.symbol, row.timeframe, row.last_error = symbol, timeframe, None
    return _instance_dict(row, runner) | {"warning": warning}


@router.delete("/{instance_id}")
async def archive_instance(request: Request, instance_id: int, user: User = Depends(require_user)):
    """Remove an instance from the list. Its signal history is kept; adding the same
    account, symbol and timeframe again brings it back."""
    runner = _runner(request)
    await _get(instance_id, user)
    if instance_id in runner.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Stop the strategy before removing it")
    async with db.session() as s, s.begin():
        row = await s.get(StrategyInstance, instance_id)
        row.status = "archived"
    return _instance_dict(row, runner)


@router.post("/{instance_id}/start")
async def start_instance(request: Request, instance_id: int, user: User = Depends(require_user)):
    runner = _runner(request)
    await _get(instance_id, user)
    try:
        await runner.start(instance_id)
    except StrategyError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return _instance_dict(await _get(instance_id, user), runner)


@router.post("/{instance_id}/stop")
async def stop_instance(request: Request, instance_id: int, user: User = Depends(require_user)):
    runner = _runner(request)
    row = await _get(instance_id, user)
    if row.status not in HIDDEN:
        await runner.stop(instance_id)
    return _instance_dict(await _get(instance_id, user), runner)


@router.get("/{instance_id}/signals")
async def list_signals(instance_id: int, limit: int = Query(default=100, ge=1, le=1000),
                       user: User = Depends(require_user)):
    await _get(instance_id, user)
    async with db.session() as s:
        rows = await s.scalars(
            select(StrategySignal).where(StrategySignal.instance_id == instance_id)
            .order_by(StrategySignal.id.desc()).limit(limit)
        )
        return [signal_dict(r) for r in rows]
