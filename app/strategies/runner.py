"""Runs strategy instances: feeds them ticks, checks every trade they ask for, places orders.

Each instance runs on one broker account (its owner's): it sees only that account's ticks and
order updates, and its orders go to that account's broker, with that account's rate limit.

Every running instance places real orders on its account's broker (no dry-run mode: use a
paper account to try a strategy). Safety rules, applied to every buy/sell before it's sent:
  - the broker must be connected
  - one open order at a time per instance
  - max order quantity, max absolute position, max orders per day (IST) — except for orders
    that only shrink the position (exits are never blocked by limits)
  - live orders also pause while the trade ledger has updates it couldn't save
  - options are ordered at a limit price, never at market (brokers cap or refuse market
    orders on options): a little through the best ask / bid, repriced every few seconds while
    unfilled. Exits are chased until they fill; entries give up after a few tries or too far
    a move (see the option_* settings)
A strategy that raises an exception is stopped with status "error".

Each running instance has its own tick queue and worker task, so a strategy that is slow
(placing an order, loading history) only delays itself: the broker feed, other strategies and
clients keep going. If an instance falls `strategy_queue_size` ticks behind, its oldest ticks
are dropped (counted in /health). Order updates are applied straight away, never queued.
Every `strategy_clock_interval` seconds an idle instance also gets a clock event (on_clock)
through the same queue, so timed rules run even when no ticks arrive.
"""

import asyncio
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.brokers import OrderError
from app.brokers.base import Depth, OptionContract
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
# Option contracts end in strike + CE/PE on every broker: NSE:NIFTY25O2825000CE, NFO:NIFTY25OCT25000CE.
OPTION_SYMBOL = re.compile(r"\d(CE|PE)$")
OPTION_TICK = Decimal("0.05")


def option_limit_price(price: float, side: Side, buffer_pct: float) -> float:
    """A limit price `buffer_pct` % through `price` (above it to buy, below it to sell), on the
    0.05 tick, rounded away from the price so it still fills at once."""
    step = Decimal(str(buffer_pct)) / 100
    raw = Decimal(str(price)) * (1 + step if side == Side.BUY else 1 - step)
    ticks = (raw / OPTION_TICK).to_integral_value(ROUND_CEILING if side == Side.BUY else ROUND_FLOOR)
    return float(max(ticks, 1) * OPTION_TICK)


@dataclass
class Clock:
    """A clock event in an instance's tick queue (see StrategyRunner._clock)."""
    at: datetime


class StrategyError(ValueError):
    """A request about strategy instances that can't be done (bad params, not stopped, ...)."""


class BrokerUnavailable(StrategyError):
    """The broker couldn't be checked right now (not connected, a failed call, the ledger
    behind). Unlike a real mismatch this clears up on its own, so the instance is retried."""


# An instance stopped by a temporary broker problem keeps status "error" with this note on its
# last_error, is retried with backoff (and right after its account reconnects), and is resumed
# on the next server start too. Stopping it by hand ends the retries.
RETRY_NOTE = " (retrying automatically)"
RETRY_FIRST_DELAY = 30.0
RETRY_MAX_DELAY = 300.0


def _retryable(exc: BaseException) -> bool:
    # A real mismatch or bad state is a StrategyError and stays stopped; anything else that
    # broke a start (a network error fetching tick sizes, ...) is worth another try.
    return isinstance(exc, BrokerUnavailable) or not isinstance(exc, StrategyError)


def _with_retry_note(message: str) -> str:
    return message[: 1024 - len(RETRY_NOTE)] + RETRY_NOTE


def _today_ist() -> date:
    return datetime.now(IST).date()


def _start_of_today_utc() -> datetime:
    return datetime.combine(_today_ist(), time.min, tzinfo=IST).astimezone(ZoneInfo("UTC"))


def signal_dict(s: StrategySignal) -> dict:
    return {
        "id": s.id, "instance_id": s.instance_id, "at": s.at.isoformat(), "symbol": s.symbol,
        "side": s.side, "quantity": s.quantity, "price": s.price, "reason": s.reason,
        "outcome": s.outcome, "message": s.message, "order_id": s.order_id,
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
    def tick_size(self) -> float | None:
        """The instrument's minimum price step, or None when the broker does not provide it."""
        return self._inst.tick_size

    @property
    def last_fill_price(self) -> float | None:
        """Broker-reported average price of the most recently filled order."""
        return self._inst.last_fill_price

    @property
    def order_pending(self) -> bool:
        """Whether this strategy instance currently has a non-terminal broker order."""
        return self._inst.pending_order_id is not None

    @property
    def strategy_state(self) -> dict:
        """Last durable strategy checkpoint loaded when this instance started."""
        return dict(self._inst.strategy_state)

    async def save_strategy_state(self, state: dict) -> None:
        """Persist a decision-state checkpoint. This is for transitions, not market ticks."""
        await self._runner.save_strategy_state(self._inst, state)

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
        self.tick_size: float | None = None
        self.last_fill_price: float | None = None
        self.held_symbol: str | None = None  # another instrument it bought (an option), if any
        self.strategy_state: dict = dict(config.strategy_state or {})
        self.pending_order_id: str | None = None
        self.filled: dict[str, int] = {}  # order id -> filled qty already counted
        self._orders_day = _today_ist()
        self._orders_count = 0
        self.lock = asyncio.Lock()
        self.ticks: asyncio.Queue[Tick | Clock] = asyncio.Queue(queue_size)
        self.worker: asyncio.Task | None = None
        self.clock: asyncio.Task | None = None
        self.dropped_ticks = 0

    def enqueue(self, tick: Tick | Clock) -> None:
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
        # Taken out of `running` but status not yet saved: drain() must still wait for these.
        self._stopping: dict[int, RunningInstance] = {}
        self.deployments: dict[str, Deployment] = {}
        self.backtester = Backtester()  # the strategy pages' performance reports
        # (account id, order id) -> instance that placed it. Order ids are only unique per account.
        self._order_owner: dict[tuple[int, str], int] = {}
        # Order updates can arrive before place_order returns (and we learn the id).
        self._recent_orders: OrderedDict[tuple[int, str], Order] = OrderedDict()
        self._chases: set[asyncio.Task] = set()  # repricing of unfilled option orders
        self._reconcile_task: asyncio.Task | None = None
        self._start_locks: dict[int, asyncio.Lock] = {}  # a retry and a manual start never overlap
        self._retries: dict[int, tuple[float, float]] = {}  # instance id -> (next try, delay)
        self._retry_wake = asyncio.Event()
        self._retry_task: asyncio.Task | None = None

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
            rows = await s.execute(
                select(StrategyInstance.id, StrategyInstance.status, StrategyInstance.last_error)
                .where(StrategyInstance.status.in_(("running", "error")))
            )
            ids = [i for i, status, error in rows
                   if status == "running" or (error or "").endswith(RETRY_NOTE)]
        for instance_id in ids:
            try:
                await self.start(instance_id)
            except Exception as e:
                await self._start_failed(instance_id, e)

    async def start(self, instance_id: int) -> None:
        async with self._start_locks.setdefault(instance_id, asyncio.Lock()):
            await self._start(instance_id)
        self._retries.pop(instance_id, None)

    async def _start(self, instance_id: int) -> None:
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

        # Paper positions intentionally live only in this process. Real accounts must prove
        # that the database execution state still matches the broker before strategy-local
        # checkpoints (stop, direction, etc.) are trusted.
        if service.name != "paper":
            if not service.connected:
                raise BrokerUnavailable(f"Broker not connected: {service.not_connected_reason}")
            await service.resync()
            if service.last_resync_error:
                raise BrokerUnavailable(f"Broker order reconciliation failed: {service.last_resync_error}")
            if not service.records_reliable():
                raise BrokerUnavailable("Trade ledger has unsaved broker updates")
            await self._reconcile_broker_state(config, service)

        inst = RunningInstance(config, self.settings.strategy_queue_size)
        if service.broker is not None:
            inst.tick_size = await service.broker.get_tick_size(config.symbol)
            if config.size_mode == "capital":
                inst.lot_size = await service.broker.get_lot_size(config.symbol) or 1
        (inst.position, inst.filled, inst.pending_order_id,
         inst.last_fill_price) = await self._load_position(config)
        inst.orders_today = await self._orders_today(instance_id)
        if inst.position:
            inst.held_symbol = await self._held_symbol(config)
        inst.strategy = dep.strategy(dep.params, StrategyContext(self, inst), TIMEFRAMES[config.timeframe])
        try:
            await inst.strategy.on_start()
        except StrategyError:
            raise
        except Exception as exc:
            raise StrategyError(f"Strategy could not initialize safely: {exc}") from exc
        self.running[instance_id] = inst
        inst.worker = asyncio.create_task(self._work(inst))
        inst.clock = asyncio.create_task(self._clock(inst))
        if self.settings.strategy_reconcile_interval > 0 and self._reconcile_task is None:
            self._reconcile_task = asyncio.create_task(self._reconciliation_loop())
        await self._set_status(instance_id, "running", None)
        await self._resume_pending_option_chase(inst)

        if service.connected:
            try:
                await service.broker.subscribe([config.symbol])
            except Exception as e:
                # Stays running; the broker re-subscribes watched symbols when it (re)connects.
                log.warning("Strategy %s: could not subscribe %s: %s", instance_id, config.symbol, e)

    async def stop(self, instance_id: int, status: str = "stopped", error: str | None = None) -> None:
        inst = self.running.pop(instance_id, None)
        if inst is not None:
            self._stopping[instance_id] = inst
        try:
            if inst is not None:
                await self._end_worker(inst)
                if inst.strategy is not None:
                    try:
                        await inst.strategy.on_stop()
                    except Exception:
                        log.exception("Strategy %s on_stop failed", instance_id)
            await self._set_status(instance_id, status, error)
        finally:
            if inst is not None and self._stopping.get(instance_id) is inst:
                del self._stopping[instance_id]
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
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            self._reconcile_task = None
        if self._retry_task is not None:
            # Waiting instances keep their retry note, so the next start resumes them too.
            self._retry_task.cancel()
            self._retry_task = None
        for task in list(self._chases):
            task.cancel()
        for inst in self.running.values():
            await self._end_worker(inst)
            if inst.strategy is not None:
                try:
                    await inst.strategy.on_stop()
                except Exception:
                    log.exception("Strategy %s on_stop failed", inst.id)
        self.running.clear()

    async def _reconciliation_loop(self) -> None:
        """Periodically prove that every real-broker strategy still matches broker truth."""
        while True:
            await asyncio.sleep(self.settings.strategy_reconcile_interval)
            try:
                await self._reconcile_running()
            except Exception:
                # Per-account failures are handled below. Keep an unexpected bug from silently
                # killing all future safety checks.
                log.exception("Periodic strategy reconciliation failed")

    async def _reconcile_running(self) -> None:
        by_account: dict[int, list[RunningInstance]] = {}
        for inst in list(self.running.values()):
            service = self.service(inst.account_id)
            if service is not None and service.name != "paper":
                by_account.setdefault(inst.account_id, []).append(inst)

        for account_id, instances in by_account.items():
            service = self.service(account_id)
            failure: str | None = None
            retry = False
            acquired: list[asyncio.Lock] = []
            try:
                # Freeze strategy order submission on this account while broker and database
                # snapshots are compared, avoiding a false mismatch mid-placement.
                for inst in sorted(instances, key=lambda item: item.id):
                    await inst.lock.acquire()
                    acquired.append(inst.lock)
                if service is None or not service.connected:
                    reason = service.not_connected_reason if service else "account unavailable"
                    raise BrokerUnavailable(f"Periodic broker reconciliation unavailable: {reason}")
                await service.resync()
                if service.last_resync_error:
                    raise BrokerUnavailable(f"Periodic order reconciliation failed: {service.last_resync_error}")
                if not service.records_reliable():
                    raise BrokerUnavailable("Trade ledger has unsaved broker updates")
                broker = service.require()
                snapshot = await asyncio.gather(broker.get_positions(), broker.get_orders())
                for inst in instances:
                    if self.running.get(inst.id) is inst:
                        await self._reconcile_broker_state(inst.config, service, snapshot)
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
                retry = _retryable(exc)
            finally:
                for lock in reversed(acquired):
                    lock.release()

            if failure:
                for inst in instances:
                    if self.running.get(inst.id) is inst:
                        await self.stop(inst.id, "error", _with_retry_note(failure) if retry else failure)
                        if retry:
                            self._schedule_retry(inst.id)

    # ---------- retrying instances stopped by a temporary broker problem ----------

    async def _start_failed(self, instance_id: int, exc: Exception) -> None:
        message = f"Could not resume: {exc}"
        if _retryable(exc):
            log.warning("Strategy %s: %s; will retry", instance_id, message)
            await self._set_status(instance_id, "error", _with_retry_note(message))
            self._schedule_retry(instance_id)
        else:
            self._retries.pop(instance_id, None)
            await self._set_status(instance_id, "error", message)

    def _schedule_retry(self, instance_id: int) -> None:
        loop = asyncio.get_running_loop()
        previous = self._retries.get(instance_id)
        delay = RETRY_FIRST_DELAY if previous is None else min(previous[1] * 2, RETRY_MAX_DELAY)
        self._retries[instance_id] = (loop.time() + delay, delay)
        if self._retry_task is None or self._retry_task.done():
            self._retry_task = asyncio.create_task(self._retry_loop(), name="strategy-resume-retry")
        self._retry_wake.set()

    def retry_now(self) -> None:
        """An account (re)connected: try every waiting instance now instead of at its next slot."""
        if not self._retries:
            return
        now = asyncio.get_running_loop().time()
        self._retries = {i: (now, delay) for i, (_, delay) in self._retries.items()}
        self._retry_wake.set()

    async def _retry_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            self._retry_wake.clear()
            now = loop.time()
            due = [i for i, (at, _) in self._retries.items() if at <= now]
            for instance_id in due:
                try:
                    await self._retry_one(instance_id)
                except Exception:
                    log.exception("Strategy %s: resume retry failed", instance_id)
            if due:
                continue
            next_at = min((at for at, _ in self._retries.values()), default=None)
            timeout = None if next_at is None else max(0.0, next_at - loop.time())
            try:
                await asyncio.wait_for(self._retry_wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    async def _retry_one(self, instance_id: int) -> None:
        if instance_id in self.running:
            self._retries.pop(instance_id, None)
            return
        async with db.session() as s:
            row = await s.get(StrategyInstance, instance_id)
        if row is None or row.status != "error" or not (row.last_error or "").endswith(RETRY_NOTE):
            self._retries.pop(instance_id, None)  # stopped, started or changed since: leave it
            return
        try:
            await self.start(instance_id)
        except Exception as e:
            await self._start_failed(instance_id, e)  # backs off, or stops retrying for good
        else:
            log.warning("Strategy %s resumed after a temporary broker problem: %s",
                        instance_id, row.last_error)

    async def _work(self, inst: RunningInstance) -> None:
        """The instance's worker: hands it its ticks one at a time, in order."""
        while True:
            tick = await inst.ticks.get()
            try:
                if isinstance(tick, Clock):
                    await inst.strategy.on_clock(tick.at)
                else:
                    inst.last_price = tick.price
                    await inst.strategy.on_tick(tick)
            except Exception as e:
                log.exception("Strategy %s failed on tick", inst.id)
                try:
                    # Keep the queue item unfinished until the error state is persisted so
                    # drain() cannot return while the instance still appears to be running.
                    await self.stop(inst.id, "error", f"{type(e).__name__}: {e}")
                finally:
                    inst.ticks.task_done()
                return
            inst.ticks.task_done()

    async def _clock(self, inst: RunningInstance) -> None:
        """Queues a clock event every `strategy_clock_interval` seconds while the instance has
        no ticks waiting (queued ticks wake it anyway), whether or not the feed is sending."""
        while True:
            await asyncio.sleep(self.settings.strategy_clock_interval)
            if inst.ticks.empty():
                inst.enqueue(Clock(utcnow()))

    async def _end_worker(self, inst: RunningInstance) -> None:
        clock, inst.clock = inst.clock, None
        if clock is not None:
            clock.cancel()
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
        # A worker stopping itself after an error keeps its current tick unfinished until the
        # error status is saved, so joining here waits for that. (An outside stop() has already
        # emptied the queue, so this returns at once.)
        for inst in list(self._stopping.values()):
            await inst.ticks.join()

    def health(self) -> dict:
        return {
            "running": len(self.running),
            "dropped_ticks": sum(i.dropped_ticks for i in self.running.values()),
            "queued_ticks": sum(i.ticks.qsize() for i in self.running.values()),
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
            if order.filled_price is not None:
                inst.last_fill_price = float(order.filled_price)
        if order.status in TERMINAL and inst.pending_order_id == order.id:
            inst.pending_order_id = None

    # ---------- trade requests ----------

    def _check(self, inst: RunningInstance, side: Side, quantity: int) -> str | None:
        """The reason a trade is blocked, or None if it may go ahead."""
        cfg = inst.config
        service = self.service(inst.account_id)
        if service is None:
            return "Broker account not available"
        if not service.connected:
            return f"Broker not connected: {service.not_connected_reason}"
        if not service.records_reliable():
            return "Trade ledger has unsaved order updates (see /health); orders paused"
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
        """Place an order on `symbol` (default the instance's own) and record it as a signal.
        The instance keeps one net position whichever symbol it trades."""
        if quantity <= 0:
            return
        symbol = symbol or inst.symbol
        async with inst.lock:
            cfg = inst.config
            price = inst.last_price if symbol == inst.symbol else await self._quote(inst.account_id, symbol)
            signal = StrategySignal(
                instance_id=inst.id, symbol=symbol, side=side.value, quantity=quantity,
                price=price, reason=(reason or "")[:512] or None,
            )
            if blocked := self._check(inst, side, quantity):
                signal.outcome, signal.message = "blocked", blocked
            else:
                inst.last_fill_price = None
                await self._place(inst, side, quantity, signal)
            if symbol != inst.symbol and side == Side.BUY and signal.outcome == "placed":
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

    async def _depth(self, account_id: int, symbol: str) -> Depth | None:
        service = self.service(account_id)
        try:
            return await service.require().get_depth(symbol) if service else None
        except Exception as e:
            log.info("No market depth for %s: %s", symbol, e)
            return None

    async def _option_price(self, account_id: int, symbol: str, side: Side,
                            last: float | None = None) -> tuple[float, str] | None:
        """Limit price for an option order, and what it's based on: the best ask (buying) or
        bid (selling), else the last price (`last`, or a fresh quote). None with neither."""
        depth = await self._depth(account_id, symbol)
        best = (depth.ask if side == Side.BUY else depth.bid) if depth else 0
        if best:
            basis = f"{'ask' if side == Side.BUY else 'bid'} {best:g}"
            return option_limit_price(best, side, self.settings.option_limit_buffer_pct), basis
        last = last or await self._quote(account_id, symbol)
        if last:
            return option_limit_price(last, side, self.settings.option_ltp_buffer_pct), f"last {last:g}"
        return None

    async def _place(self, inst: RunningInstance, side: Side, quantity: int, signal: StrategySignal) -> None:
        service = self.service(inst.account_id)
        signal.broker = service.name
        order_type, limit_price = OrderType.MARKET, None
        closing = inst.position > 0 if side == Side.SELL else inst.position < 0
        if OPTION_SYMBOL.search(signal.symbol):
            if priced := await self._option_price(inst.account_id, signal.symbol, side, signal.price):
                order_type = OrderType.LIMIT
                limit_price, basis = priced
                signal.message = f"Limit {limit_price:.2f} ({basis})"
            else:  # nothing to price it from: the broker's own market protection applies
                log.warning("Strategy %s: no price for %s; placing a market order", inst.id, signal.symbol)
        try:
            order = await service.place_order(PlaceOrder(
                action="place_order", symbol=signal.symbol, side=side, quantity=quantity,
                order_type=order_type, limit_price=limit_price,
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
            if order_type == OrderType.LIMIT:
                task = asyncio.create_task(self._chase(inst, order.id, signal.symbol, side, limit_price, closing))
                self._chases.add(task)
                task.add_done_callback(self._chases.discard)
        # Apply what we know now, plus any update that raced ahead of place_order returning.
        self._apply_order(inst, order)
        if early := self._recent_orders.get((inst.account_id, order.id)):
            self._apply_order(inst, early)

    async def _chase(self, inst: RunningInstance, order_id: str, symbol: str, side: Side,
                     first_limit: float, closing: bool) -> None:
        """Keep an unfilled option order where it can fill. Every option_reprice_interval, if
        the best ask (buying) or bid (selling) + buffer has moved past its limit, move the limit
        there. An exit (`closing`) is chased until it fills. An entry is cancelled, and the trade
        skipped, after option_entry_max_reprices checks or rather than go more than
        option_entry_max_chase_pct past `first_limit`. While it's open the instance can't place
        another order. Ends when the order is done or the instance stops."""
        cfg = self.settings
        sign = 1 if side == Side.BUY else -1  # "further" = higher to buy, lower to sell
        cap = first_limit * (1 + sign * cfg.option_entry_max_chase_pct / 100)
        limit, checks = first_limit, 0
        while True:
            await asyncio.sleep(cfg.option_reprice_interval)
            if inst.pending_order_id != order_id or self.running.get(inst.id) is not inst:
                return  # filled, cancelled or rejected; or the strategy stopped
            service = self.service(inst.account_id)
            if service is None or not service.connected:
                continue
            checks += 1
            try:
                priced = await self._option_price(inst.account_id, symbol, side)
                target = priced[0] if priced else None
                if not closing:
                    if checks > cfg.option_entry_max_reprices:
                        await self._cancel_unfilled(inst, order_id, f"entry unfilled after {checks - 1} checks")
                        return
                    if target is not None and sign * (target - cap) > 0:
                        await self._cancel_unfilled(
                            inst, order_id, f"entry would chase to {target:.2f}, past {cap:.2f} "
                                            f"({cfg.option_entry_max_chase_pct:g}% from {first_limit:.2f})")
                        return
                if target is None or sign * (target - limit) <= 0:
                    continue  # still priced to fill (or nothing to price from): give it time
                try:
                    order = await service.modify_order(order_id, target)
                except Exception as e:  # e.g. the broker's limit on modifications per order
                    if inst.pending_order_id == order_id:
                        await self._cancel_unfilled(inst, order_id, f"could not reprice ({e})")
                    return  # an exit is sent again, fresh, by the strategy's own retry
                log.info("Strategy %s: repriced %s order %s from %.2f to %.2f (%s)", inst.id,
                         "exit" if closing else "entry", order_id, limit, target, priced[1])
                limit = target
                self._apply_order(inst, order)  # it may have filled straight away
            except Exception:
                log.exception("Strategy %s: repricing order %s failed; trying again", inst.id, order_id)

    async def _cancel_unfilled(self, inst: RunningInstance, order_id: str, why: str) -> None:
        """Cancel an order the instance gave up on. What filled before stays counted."""
        try:
            order = await self.service(inst.account_id).cancel_order(order_id)
        except Exception as e:  # e.g. it filled just now: its update clears pending_order_id
            log.info("Strategy %s: could not cancel order %s (%s): %s", inst.id, order_id, why, e)
            return
        log.warning("Strategy %s: cancelled order %s: %s", inst.id, order_id, why)
        self._apply_order(inst, order)

    async def _resume_pending_option_chase(self, inst: RunningInstance) -> None:
        """A deployment restart must not leave an open option limit order unmanaged."""
        if not inst.pending_order_id:
            return
        async with db.session() as s:
            row = await s.get(OrderRecord, (inst.account_id, inst.pending_order_id))
        if (row is None or row.order_type != OrderType.LIMIT.value or
                not OPTION_SYMBOL.search(row.symbol) or row.limit_price is None):
            return
        side = Side(row.side)
        task = asyncio.create_task(self._chase(
            inst, row.id, row.symbol, side, float(row.limit_price), side == Side.SELL,
        ))
        self._chases.add(task)
        task.add_done_callback(self._chases.discard)
        log.info("Strategy %s: resumed repricing pending option order %s", inst.id, row.id)

    # ---------- persistence ----------

    async def _reconcile_broker_state(
        self, config: StrategyInstance, service: BrokerService,
        snapshot: tuple[list, list] | list | None = None,
    ) -> None:
        """Require recorded strategy execution state to match live broker truth.

        Positions are account-level at a broker, so expected quantities include every strategy
        instance on the account, including stopped instances that still own an open position.
        Open-order comparison is scoped to this instance's recorded order ids.
        """
        if snapshot is None:
            broker = service.require()
            try:
                positions, orders = await asyncio.gather(broker.get_positions(), broker.get_orders())
            except Exception as exc:
                raise BrokerUnavailable(f"Could not read broker positions/orders: {exc}") from exc
        else:
            positions, orders = snapshot

        async with db.session() as s:
            rows = (await s.execute(
                select(
                    StrategySignal.instance_id, StrategySignal.symbol, OrderRecord.id,
                    OrderRecord.side, OrderRecord.filled_quantity, OrderRecord.status,
                )
                .join(StrategyInstance, StrategyInstance.id == StrategySignal.instance_id)
                .join(OrderRecord, (StrategySignal.order_id == OrderRecord.id)
                      & (OrderRecord.account_id == StrategyInstance.account_id))
                .where(StrategyInstance.account_id == config.account_id,
                       StrategySignal.outcome == "placed")
            )).all()

        expected: dict[str, int] = {}
        all_owned_ids: set[str] = set()
        owned_ids: set[str] = set()
        recorded_open: set[str] = set()
        instance_symbols: set[str] = set()
        terminal = {status.value for status in TERMINAL}
        for owner, symbol, order_id, side, filled, order_status in rows:
            all_owned_ids.add(order_id)
            quantity = filled or 0
            expected[symbol] = expected.get(symbol, 0) + (
                quantity if side == Side.BUY.value else -quantity
            )
            if owner == config.id:
                instance_symbols.add(symbol)
                owned_ids.add(order_id)
                if order_status not in terminal:
                    recorded_open.add(order_id)

        # Direct-instrument strategies trade their configured symbol. Options strategies use
        # it only as the signal underlying. For those, reconcile every option position on the
        # account: an unrecorded CE/PE may be an order accepted just before a process crash.
        options_strategy = config.strategy.endswith("_options")
        if options_strategy:
            instance_symbols.update(symbol for symbol in expected if OPTION_SYMBOL.search(symbol))
            instance_symbols.update(
                position.symbol for position in positions if OPTION_SYMBOL.search(position.symbol)
            )
        else:
            instance_symbols.add(config.symbol)
            expected.setdefault(config.symbol, 0)

        actual = {position.symbol: position.quantity for position in positions if position.quantity}
        mismatches = [
            f"{symbol}: database {expected.get(symbol, 0)}, broker {actual.get(symbol, 0)}"
            for symbol in sorted(instance_symbols)
            if expected.get(symbol, 0) != actual.get(symbol, 0)
        ]
        broker_open = {
            order.id for order in orders
            if order.id in owned_ids and order.status not in TERMINAL
        }
        if broker_open != recorded_open:
            mismatches.append(
                f"open orders: database {sorted(recorded_open)}, broker {sorted(broker_open)}"
            )
        unmatched_open = sorted(
            order.id for order in orders
            if order.status not in TERMINAL
            and (order.symbol in instance_symbols or
                 (options_strategy and OPTION_SYMBOL.search(order.symbol)))
            and order.id not in all_owned_ids
        )
        if unmatched_open:
            mismatches.append(f"unmatched broker open orders: {unmatched_open}")
        if mismatches:
            raise StrategyError("Broker state mismatch; strategy not resumed (" + "; ".join(mismatches) + ")")

    async def save_strategy_state(self, inst: RunningInstance, state: dict) -> None:
        """Atomically replace an instance's durable checkpoint and its in-memory copy."""
        checkpoint = dict(state)
        if checkpoint == inst.strategy_state:
            return
        async with db.session() as s, s.begin():
            row = await s.get(StrategyInstance, inst.id)
            if row is None:
                raise StrategyError(f"No strategy instance {inst.id}")
            row.strategy_state = checkpoint
        inst.strategy_state = checkpoint
        inst.config.strategy_state = checkpoint

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
                       StrategySignal.outcome == "placed")
                .order_by(StrategySignal.id.desc()).limit(1)
            )
        return symbol if symbol and symbol != config.symbol else None

    async def _load_position(
        self, config: StrategyInstance
    ) -> tuple[int, dict[str, int], str | None, float | None]:
        """Rebuild position, pending order and latest actual fill after a restart."""
        async with db.session() as s:
            rows = (await s.execute(
                select(OrderRecord.id, OrderRecord.side, OrderRecord.filled_quantity,
                       OrderRecord.status, OrderRecord.filled_price)
                .join(StrategySignal, (StrategySignal.order_id == OrderRecord.id)
                      & (OrderRecord.account_id == config.account_id))
                .where(StrategySignal.instance_id == config.id, StrategySignal.outcome == "placed")
                .order_by(StrategySignal.id)
            )).all()
        position, filled, pending, last_fill = 0, {}, None, None
        for order_id, side, qty, status, fill_price in rows:
            filled[order_id] = qty or 0
            position += (qty or 0) if side == Side.BUY.value else -(qty or 0)
            if qty and fill_price is not None:
                last_fill = float(fill_price)
            if status not in {s.value for s in TERMINAL}:
                self._order_owner[(config.account_id, order_id)] = config.id
                pending = order_id
        return position, filled, pending, last_fill

    async def _orders_today(self, instance_id: int) -> int:
        async with db.session() as s:
            return await s.scalar(
                select(func.count()).select_from(StrategySignal).where(
                    StrategySignal.instance_id == instance_id,
                    StrategySignal.outcome == "placed",
                    StrategySignal.at >= _start_of_today_utc(),
                )
            )
