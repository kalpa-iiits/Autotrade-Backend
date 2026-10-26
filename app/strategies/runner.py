"""Runs strategy instances: feeds them ticks, checks every trade they ask for, places orders.

Each instance runs on one broker account (its owner's): it sees only that account's ticks and
order updates, and its orders go to that account's broker, with that account's rate limit.

Safety rules, applied to every buy/sell before anything reaches the broker:
  - mode "signal": nothing is sent; the trade is recorded and the strategy's position moves
    as if it filled at the last price, so a dry run behaves like the real thing
  - mode "live" also needs AUTOTRADE_STRATEGIES_LIVE_ENABLED=true (global switch, off by default)
  - one open order at a time per instance
  - max order quantity, max absolute position, max orders per day (IST) — except for orders
    that only shrink the position (exits are never blocked by limits)
  - live orders also pause while the trade ledger has updates it couldn't save
A strategy that raises an exception is stopped with status "error".

Each running instance has its own tick queue and worker task, so a strategy that is slow
(placing an order, loading history) only delays itself: the broker feed, other strategies and
clients keep going. If an instance falls `strategy_queue_size` ticks behind, its oldest ticks
are dropped (counted in /health). Order updates are applied straight away, never queued.
"""

import asyncio
import logging
from collections import OrderedDict
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.brokers import OrderError
from app.brokers.base import OptionContract
from app.core import db
from app.core.broker_service import BrokerService
from app.core.config import Settings
from app.core.db import utcnow
from app.orders.models import OrderRecord
from app.orders.schemas import Order, OrderStatus, OrderType, PlaceOrder, Side
from app.strategies.backtest import Backtester
from app.strategies.base import TIMEFRAMES, Strategy, Tick
from app.strategies.deployment import Deployment
from app.strategies.models import StrategyInstance, StrategySignal

if TYPE_CHECKING:
    from app.accounts.registry import AccountRegistry

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}

class StrategyError(ValueError):
    """A request about strategy instances that can't be done (bad params, not stopped, ...)."""


def _today_ist() -> date:
    return datetime.now(IST).date()


def _start_of_today_utc() -> datetime:
    return datetime.combine(_today_ist(), time.min, tzinfo=IST).astimezone(ZoneInfo("UTC"))


def signal_dict(s: StrategySignal) -> dict:
    return {
        "id": s.id, "instance_id": s.instance_id, "at": s.at.isoformat(), "symbol": s.symbol,
        "side": s.side, "quantity": s.quantity, "price": s.price, "reason": s.reason,
        "mode": s.mode, "outcome": s.outcome, "message": s.message, "order_id": s.order_id,
    }


def sized_quantity(config: StrategyInstance, price: float | None, lot: int = 1) -> int:
    """Units per trade under the instance's sizing: its fixed quantity (default: the code's),
    or as many whole lots as `capital_per_trade` pays for at `price`. Never above the code's
    max_order_qty / max_position. 0 when capital sizing has no price yet or can't pay for a lot."""
    if config.size_mode == "capital":
        if not config.capital_per_trade or not price or price <= 0:
            return 0
        units = int(Decimal(config.capital_per_trade) / Decimal(str(price)))
        units -= units % max(lot, 1)
    else:
        units = config.fixed_quantity or config.quantity
    return max(0, min(units, config.max_order_qty, config.max_position))


class StrategyContext:
    """What a running strategy can see and do."""

    def __init__(self, runner: "StrategyRunner", inst: "RunningInstance") -> None:
        self._runner = runner
        self._inst = inst

    @property
    def symbol(self) -> str:
        return self._inst.symbol

    @property
    def position(self) -> int:
        """This instance's net quantity: >0 long, <0 short (virtual in signal mode)."""
        return self._inst.position

    @property
    def last_price(self) -> float | None:
        return self._inst.last_price

    @property
    def timeframe(self) -> str:
        return self._inst.config.timeframe

    @property
    def quantity(self) -> int:
        """The instance's trade size at the last price (see sized_quantity)."""
        return sized_quantity(self._inst.config, self._inst.last_price, self._inst.lot_size)

    @property
    def held_symbol(self) -> str | None:
        """The other instrument (e.g. an option) this instance last bought, while it holds one."""
        return self._inst.held_symbol if self._inst.position else None

    async def buy(self, quantity: int | None = None, reason: str | None = None, symbol: str | None = None) -> None:
        """Buy `symbol` (default: the instance's own symbol, whose ticks it receives)."""
        await self._runner.request(self._inst, Side.BUY, quantity or self._sized(reason), reason, symbol)

    async def sell(self, quantity: int | None = None, reason: str | None = None, symbol: str | None = None) -> None:
        await self._runner.request(self._inst, Side.SELL, quantity or self._sized(reason), reason, symbol)

    async def option_contracts(self) -> list[OptionContract]:
        """Options listed on the instance's symbol, from its broker."""
        service = self._runner.service(self._inst.account_id)
        if service is None:
            raise OrderError("Broker account not available")
        return await service.require().get_option_contracts(self.symbol)

    def _sized(self, reason: str | None) -> int:
        quantity = self.quantity
        cfg = self._inst.config
        if quantity == 0 and cfg.size_mode == "capital":
            self.log(f"skipped ({reason or 'entry'}): ₹{cfg.capital_per_trade} per trade doesn't buy one lot "
                     f"of {self.symbol} at {self._inst.last_price}")
        return quantity

    async def exit(self, reason: str | None = None) -> None:
        """Flatten the position."""
        if self.position > 0:
            await self.sell(self.position, reason)
        elif self.position < 0:
            await self.buy(-self.position, reason)

    async def history(self, resolution: str = "D", days: int = 30) -> list:
        """Candles for this symbol over the last `days` calendar days (oldest first), from the
        broker. Raises OrderError if the broker isn't connected or has no history."""
        from datetime import timedelta

        today = datetime.now(IST).date()
        service = self._runner.service(self._inst.account_id)
        if service is None:
            raise OrderError("Broker account not available")
        broker = service.require()
        return await broker.get_history(self.symbol, resolution, today - timedelta(days=days), today)

    def log(self, message: str) -> None:
        log.info("[strategy %s %s] %s", self._inst.id, self._inst.config.label, message)


class RunningInstance:
    def __init__(self, config: StrategyInstance, queue_size: int = 1000) -> None:
        self.config = config
        self.id = config.id
        self.account_id = config.account_id
        self.symbol = config.symbol
        self.strategy: Strategy | None = None
        self.position = 0
        self.last_price: float | None = None
        self.lot_size = 1  # the instrument's trading lot, for capital sizing
        self.held_symbol: str | None = None  # another instrument it bought (an option), if any
        self.pending_order_id: str | None = None
        self.filled: dict[str, int] = {}  # order id -> filled qty already counted
        self._orders_day = _today_ist()
        self._orders_count = 0
        self.lock = asyncio.Lock()
        self.ticks: asyncio.Queue[Tick] = asyncio.Queue(queue_size)
        self.worker: asyncio.Task | None = None
        self.dropped_ticks = 0

    def enqueue(self, tick: Tick) -> None:
        if self.ticks.full():
            # Behind: the oldest tick matters least.
            self.ticks.get_nowait()
            self.ticks.task_done()
            self.dropped_ticks += 1
            if self.dropped_ticks % 100 == 1:
                log.warning("Strategy %s is falling behind; %d ticks dropped", self.id, self.dropped_ticks)
        self.ticks.put_nowait(tick)

    @property
    def orders_today(self) -> int:
        """Orders counted against max_orders_per_day. Starts again at 0 each IST day, also
        for an instance that keeps running past midnight."""
        return self._orders_count if self._orders_day == _today_ist() else 0

    @orders_today.setter
    def orders_today(self, value: int) -> None:
        self._orders_day, self._orders_count = _today_ist(), value


class StrategyRunner:
    def __init__(self, accounts: "AccountRegistry", settings: Settings):
        self.accounts = accounts
        self.settings = settings
        self.running: dict[int, RunningInstance] = {}
        self.deployments: dict[str, Deployment] = {}
        self.backtester = Backtester()  # the strategy pages' performance reports
        # (account id, order id) -> instance that placed it. Order ids are only unique per account.
        self._order_owner: dict[tuple[int, str], int] = {}
        # Order updates can arrive before place_order returns (and we learn the id).
        self._recent_orders: OrderedDict[tuple[int, str], Order] = OrderedDict()

    def service(self, account_id: int) -> BrokerService | None:
        runtime = self.accounts.get(account_id)
        return runtime.service if runtime is not None else None

    # ---------- lifecycle ----------

    def symbols(self, account_id: int) -> set[str]:
        """Symbols this account's running strategies need; kept subscribed on its broker."""
        return {inst.symbol for inst in self.running.values() if inst.account_id == account_id}

    async def sync(self, deployments: list[Deployment]) -> None:
        """At startup: make strategy_instances match the deployments declared in code. Code
        defines what can run; users add instances of it on their own accounts in the app, so
        nothing is created here."""
        keys = [d.key for d in deployments]
        if len(keys) != len(set(keys)):
            raise ValueError(f"Duplicate deployment keys in deployments.py: {keys}")
        self.deployments = {d.key: d for d in deployments}
        async with db.session() as s, s.begin():
            rows = list(await s.scalars(select(StrategyInstance)))
            by_key: dict[str, list[StrategyInstance]] = {}
            for row in rows:
                by_key.setdefault(row.deployment_key, []).append(row)

            for dep in deployments:
                wanted = dep.settings()
                for row in by_key.get(dep.key, []):
                    changed = any(getattr(row, k) != v for k, v in wanted.items())
                    for k, v in wanted.items():
                        setattr(row, k, v)
                    if row.status == "removed":
                        row.status, row.last_error = "stopped", None
                    elif changed and row.status in ("running", "error"):
                        # Never resume with different settings (e.g. switched to live) unattended.
                        row.status = "stopped"
                        row.last_error = "Settings changed in code; start it again"

            for row in rows:
                if row.deployment_key not in self.deployments and row.status not in ("removed", "archived"):
                    row.status, row.last_error = "removed", "No longer in deployments.py"

    async def resume(self) -> None:
        """At startup: restart instances that were running when the server stopped."""
        async with db.session() as s:
            ids = list(await s.scalars(
                select(StrategyInstance.id).where(StrategyInstance.status == "running")
            ))
        for instance_id in ids:
            try:
                await self.start(instance_id)
            except Exception as e:
                await self._set_status(instance_id, "error", f"Could not resume: {e}")

    async def start(self, instance_id: int) -> None:
        if instance_id in self.running:
            return
        async with db.session() as s:
            config = await s.get(StrategyInstance, instance_id)
        if config is None:
            raise StrategyError(f"No strategy instance {instance_id}")
        if config.status == "archived":
            raise StrategyError("This instance is archived; add it again to use it")
        dep = self.deployments.get(config.deployment_key)
        if dep is None:
            raise StrategyError(f"{config.deployment_key!r} is not in deployments.py")
        try:
            dep.check_timeframe(config.timeframe)
        except ValueError as e:
            raise StrategyError(str(e))
        service = self.service(config.account_id)
        if service is None:
            raise StrategyError("Its broker account is not available (deleted?)")

        inst = RunningInstance(config, self.settings.strategy_queue_size)
        if config.size_mode == "capital" and service.broker is not None:
            inst.lot_size = await service.broker.get_lot_size(config.symbol) or 1
        inst.position, inst.filled, inst.pending_order_id = await self._load_position(config)
        inst.orders_today = await self._orders_today(instance_id)
        if inst.position:
            inst.held_symbol = await self._held_symbol(config)
        inst.strategy = dep.strategy(dep.params, StrategyContext(self, inst), TIMEFRAMES[config.timeframe])
        await inst.strategy.on_start()
        self.running[instance_id] = inst
        inst.worker = asyncio.create_task(self._work(inst))
        await self._set_status(instance_id, "running", None)

        if service.connected:
            try:
                await service.broker.subscribe([config.symbol])
            except Exception as e:
                # Stays running; the broker re-subscribes watched symbols when it (re)connects.
                log.warning("Strategy %s: could not subscribe %s: %s", instance_id, config.symbol, e)

    async def stop(self, instance_id: int, status: str = "stopped", error: str | None = None) -> None:
        inst = self.running.pop(instance_id, None)
        if inst is not None:
            await self._end_worker(inst)
            if inst.strategy is not None:
                try:
                    await inst.strategy.on_stop()
                except Exception:
                    log.exception("Strategy %s on_stop failed", instance_id)
        await self._set_status(instance_id, status, error)
        service = self.service(inst.account_id) if inst is not None else None
        if service is not None and service.connected and inst.symbol not in service.watched_symbols():
            try:
                await service.broker.unsubscribe([inst.symbol])
            except Exception:
                pass

    async def stop_all(self, account_ids: set[int] | None = None) -> list[int]:
        """Kill switch: stop every running instance (of these accounts only, if given)."""
        ids = [i for i, inst in self.running.items() if account_ids is None or inst.account_id in account_ids]
        for instance_id in ids:
            await self.stop(instance_id)
        return ids

    async def shutdown(self) -> None:
        """Server stopping: leave statuses as "running" so they resume on the next start."""
        for inst in self.running.values():
            await self._end_worker(inst)
            if inst.strategy is not None:
                try:
                    await inst.strategy.on_stop()
                except Exception:
                    log.exception("Strategy %s on_stop failed", inst.id)
        self.running.clear()

    async def _work(self, inst: RunningInstance) -> None:
        """The instance's worker: hands it its ticks one at a time, in order."""
        while True:
            tick = await inst.ticks.get()
            try:
                inst.last_price = tick.price
                await inst.strategy.on_tick(tick)
            except Exception as e:
                log.exception("Strategy %s failed on tick", inst.id)
                inst.ticks.task_done()
                await self.stop(inst.id, "error", f"{type(e).__name__}: {e}")
                return
            inst.ticks.task_done()

    async def _end_worker(self, inst: RunningInstance) -> None:
        worker, inst.worker = inst.worker, None
        if worker is not None and worker is not asyncio.current_task():
            # request() holds the lock from placing an order until its signal is saved: wait
            # for that, so stopping never leaves a placed order unrecorded.
            async with inst.lock:
                worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        while not inst.ticks.empty():  # unblock anyone waiting in drain()
            inst.ticks.get_nowait()
            inst.ticks.task_done()

    async def drain(self) -> None:
        """Wait until every running instance has handled the ticks queued so far."""
        for inst in list(self.running.values()):
            if inst.worker is not None and not inst.worker.done():
                await inst.ticks.join()

    def health(self) -> dict:
        return {
            "running": len(self.running),
            "dropped_ticks": sum(i.dropped_ticks for i in self.running.values()),
            "queued_ticks": sum(i.ticks.qsize() for i in self.running.values()),
            "live_enabled": self.settings.strategies_live_enabled,
        }

    # ---------- broker events ----------

    async def on_tick(self, account_id: int, symbol: str, price: float) -> None:
        tick = None
        for inst in list(self.running.values()):
            if inst.account_id != account_id or inst.symbol != symbol:
                continue
            tick = tick or Tick(symbol=symbol, price=price, at=utcnow())
            inst.enqueue(tick)

    async def on_order(self, account_id: int, order: Order) -> None:
        key = (account_id, order.id)
        self._recent_orders[key] = order
        self._recent_orders.move_to_end(key)
        while len(self._recent_orders) > 500:
            self._recent_orders.popitem(last=False)
        owner = self._order_owner.get(key)
        if owner is not None and owner in self.running:
            self._apply_order(self.running[owner], order)

    def _apply_order(self, inst: RunningInstance, order: Order) -> None:
        filled = order.filled_quantity or (order.quantity if order.status == OrderStatus.FILLED else 0)
        new = filled - inst.filled.get(order.id, 0)
        if new > 0:
            inst.filled[order.id] = filled
            inst.position += new if order.side == Side.BUY else -new
        if order.status in TERMINAL and inst.pending_order_id == order.id:
            inst.pending_order_id = None

    # ---------- trade requests ----------

    def _check(self, inst: RunningInstance, side: Side, quantity: int) -> str | None:
        """The reason a trade is blocked, or None if it may go ahead."""
        cfg = inst.config
        if cfg.mode == "live":
            service = self.service(inst.account_id)
            if not self.settings.strategies_live_enabled:
                return "Live strategy trading is off (set AUTOTRADE_STRATEGIES_LIVE_ENABLED=true)"
            if service is None:
                return "Broker account not available"
            if not service.connected:
                return f"Broker not connected: {service.not_connected_reason}"
            if not service.records_reliable():
                return "Trade ledger has unsaved order updates (see /health); live orders paused"
            if inst.pending_order_id:
                return f"Order {inst.pending_order_id} is still open"
        new_position = inst.position + (quantity if side == Side.BUY else -quantity)
        reduces_only = abs(new_position) < abs(inst.position) and (new_position * inst.position >= 0)
        if reduces_only:
            return None  # limits must never stop a strategy from getting out of a position
        if quantity > cfg.max_order_qty:
            return f"Quantity {quantity} is above max_order_qty {cfg.max_order_qty}"
        if abs(new_position) > cfg.max_position:
            return f"Position would be {new_position}; max_position is {cfg.max_position}"
        if inst.orders_today >= cfg.max_orders_per_day:
            return f"Reached max_orders_per_day ({cfg.max_orders_per_day})"
        return None

    async def request(self, inst: RunningInstance, side: Side, quantity: int, reason: str | None,
                      symbol: str | None = None) -> None:
        """Signal (and in live mode place) a market order on `symbol`, default the instance's own.
        The instance keeps one net position whichever symbol it trades."""
        if quantity <= 0:
            return
        symbol = symbol or inst.symbol
        async with inst.lock:
            cfg = inst.config
            price = inst.last_price if symbol == inst.symbol else await self._quote(inst.account_id, symbol)
            signal = StrategySignal(
                instance_id=inst.id, symbol=symbol, side=side.value, quantity=quantity,
                price=price, reason=(reason or "")[:512] or None, mode=cfg.mode,
            )
            if blocked := self._check(inst, side, quantity):
                signal.outcome, signal.message = "blocked", blocked
            elif cfg.mode != "live":
                signal.outcome = "signal_only"
                inst.position += quantity if side == Side.BUY else -quantity
                inst.orders_today += 1
            else:
                await self._place(inst, side, quantity, signal)
            if symbol != inst.symbol and side == Side.BUY and signal.outcome in ("signal_only", "placed"):
                inst.held_symbol = symbol
            await self._record(signal, inst.account_id)

    async def _quote(self, account_id: int, symbol: str) -> float | None:
        """Last price of an instrument the instance doesn't stream (e.g. the option it trades)."""
        service = self.service(account_id)
        try:
            return (await service.require().get_quotes([symbol])).get(symbol) if service else None
        except Exception as e:
            log.info("No quote for %s: %s", symbol, e)
            return None

    async def _place(self, inst: RunningInstance, side: Side, quantity: int, signal: StrategySignal) -> None:
        service = self.service(inst.account_id)
        signal.broker = service.name
        try:
            order = await service.place_order(PlaceOrder(
                action="place_order", symbol=signal.symbol, side=side, quantity=quantity,
                order_type=OrderType.MARKET,
            ))
        except OrderError as e:
            signal.outcome, signal.message = "rejected", str(e)
            return
        except Exception as e:
            signal.outcome, signal.message = "rejected", f"Broker error: {e}"
            return

        inst.orders_today += 1
        signal.order_id = order.id
        if order.status == OrderStatus.REJECTED:
            signal.outcome, signal.message = "rejected", order.reason
            return
        signal.outcome = "placed"
        self._order_owner[(inst.account_id, order.id)] = inst.id
        if order.status not in TERMINAL:
            inst.pending_order_id = order.id
        # Apply what we know now, plus any update that raced ahead of place_order returning.
        self._apply_order(inst, order)
        if early := self._recent_orders.get((inst.account_id, order.id)):
            self._apply_order(inst, early)

    # ---------- persistence ----------

    async def _record(self, signal: StrategySignal, account_id: int) -> None:
        async with db.session() as s, s.begin():
            s.add(signal)
        await self.accounts.publish(account_id, {"type": "strategy_signal", "signal": signal_dict(signal)})

    async def _set_status(self, instance_id: int, status: str, error: str | None) -> None:
        async with db.session() as s, s.begin():
            row = await s.get(StrategyInstance, instance_id)
            if row is None:
                return
            row.status, row.last_error = status, error
            account_id = row.account_id
        await self.accounts.publish(account_id, {"type": "strategy_status", "instance_id": instance_id,
                                                 "status": status, "error": error})

    async def _held_symbol(self, config: StrategyInstance) -> str | None:
        """After a restart: the other instrument its latest accepted buy was for, if any."""
        async with db.session() as s:
            symbol = await s.scalar(
                select(StrategySignal.symbol)
                .where(StrategySignal.instance_id == config.id, StrategySignal.side == Side.BUY.value,
                       StrategySignal.outcome.in_(("signal_only", "placed")))
                .order_by(StrategySignal.id.desc()).limit(1)
            )
        return symbol if symbol and symbol != config.symbol else None

    async def _load_position(
        self, config: StrategyInstance
    ) -> tuple[int, dict[str, int], str | None]:
        """Rebuild the instance's position after a restart, from its own history."""
        async with db.session() as s:
            if config.mode != "live":
                rows = await s.execute(
                    select(StrategySignal.side, func.sum(StrategySignal.quantity))
                    .where(StrategySignal.instance_id == config.id,
                           StrategySignal.outcome == "signal_only")
                    .group_by(StrategySignal.side)
                )
                totals = dict(rows.all())
                return int(totals.get("BUY", 0) - totals.get("SELL", 0)), {}, None
            rows = (await s.execute(
                select(OrderRecord.id, OrderRecord.side, OrderRecord.filled_quantity, OrderRecord.status)
                .join(StrategySignal, (StrategySignal.order_id == OrderRecord.id)
                      & (OrderRecord.account_id == config.account_id))
                .where(StrategySignal.instance_id == config.id, StrategySignal.outcome == "placed")
            )).all()
        position, filled, pending = 0, {}, None
        for order_id, side, qty, status in rows:
            filled[order_id] = qty or 0
            position += (qty or 0) if side == Side.BUY.value else -(qty or 0)
            if status not in {s.value for s in TERMINAL}:
                self._order_owner[(config.account_id, order_id)] = config.id
                pending = order_id
        return position, filled, pending

    async def _orders_today(self, instance_id: int) -> int:
        async with db.session() as s:
            return await s.scalar(
                select(func.count()).select_from(StrategySignal).where(
                    StrategySignal.instance_id == instance_id,
                    StrategySignal.outcome.in_(("placed", "signal_only")),
                    StrategySignal.at >= _start_of_today_utc(),
                )
            )
