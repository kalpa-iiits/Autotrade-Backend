"""Backtests (the strategy tester): run a deployment's strategy over past candles from the broker,
the way the runner runs it in signal mode, and report how it would have done. Strategy ports may
provide a candle replay hook when their source platform has materially different bar semantics.

The broker only gives daily and 1-minute candles, so every timeframe is replayed from
1-minute candles: each one becomes four ticks (open, then low and high in the order the candle
most likely took, then close), which the strategy's own BarBuilder groups into its bars, as it
does with live ticks. Generic fills are at the price of the tick that asked for them. A deployment
may configure commission; taxes and slippage are not modelled.

A test covers a date range (up to MAX_RANGE_DAYS) and starting capital. Trades are sized from
the capital like TradingView's "% of equity" order size unless the deployment specifies a fixed
backtest quantity. The deployment's max_orders_per_day still applies; live position limits don't.
"""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.brokers import Broker, OrderError
from app.brokers.base import Candle
from app.strategies.base import TIMEFRAMES, Tick
from app.strategies.deployment import Deployment

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_DAYS = 365  # range when none is given: the last year
MAX_RANGE_DAYS = 730
DEFAULT_CAPITAL = 500_000.0
DEFAULT_SIZE_PCT = 100.0
CHUNK_DAYS = 55  # 1-minute history per broker request (Kite allows 60, Fyers 100)
ASSUMPTIONS = [
    "Replayed from 1-minute candles: each candle is four ticks (open, low/high, close)",
    "Market fills at the triggering price; deployment-configured commission is included; taxes and slippage are not",
    "Entries use the deployment's fixed backtest quantity when configured, otherwise the order size % of equity",
    "A position still open at the end is closed at the last price",
]


class BacktestError(ValueError):
    """The backtest could not run (no history, bad range, the strategy raised, ...)."""


@dataclass
class SimTrade:
    direction: str  # LONG / SHORT
    quantity: int
    entry_at: datetime
    entry_price: float
    entry_reason: str | None
    exit_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    high: float = 0.0  # best and worst prices while it was open
    low: float = 0.0
    entry_fees: float = 0.0
    exit_fees: float = 0.0

    def __post_init__(self) -> None:
        self.high = self.high or self.entry_price
        self.low = self.low or self.entry_price

    @property
    def long(self) -> bool:
        return self.direction == "LONG"

    @property
    def pnl(self) -> float:
        gross = (1 if self.long else -1) * (self.exit_price - self.entry_price) * self.quantity
        return gross - self.entry_fees - self.exit_fees

    @property
    def commission(self) -> float:
        return self.entry_fees + self.exit_fees

    @property
    def run_up(self) -> float:
        """Most it was ever in profit (maximum favorable excursion)."""
        return max(0.0, ((self.high - self.entry_price) if self.long else (self.entry_price - self.low)) * self.quantity)

    @property
    def drawdown(self) -> float:
        """Most it was ever in loss (maximum adverse excursion), as a positive amount."""
        return max(0.0, ((self.entry_price - self.low) if self.long else (self.high - self.entry_price)) * self.quantity)


class BacktestContext:
    """The StrategyContext a strategy sees during a backtest: same calls, simulated."""

    def __init__(self, sim: "Simulation") -> None:
        self._sim = sim

    @property
    def symbol(self) -> str:
        return self._sim.symbol

    @property
    def position(self) -> int:
        return self._sim.position

    @property
    def last_price(self) -> float | None:
        return self._sim.last_price

    @property
    def timeframe(self) -> str:
        return self._sim.timeframe

    @property
    def quantity(self) -> int:
        return self._sim.quantity

    @property
    def average_entry_price(self) -> float | None:
        return self._sim._open.entry_price if self._sim._open else None

    @property
    def tick_size(self) -> float | None:
        """The instrument's minimum price step, or None if the broker doesn't say."""
        return self._sim.tick_size

    async def buy(self, quantity: int | None = None, reason: str | None = None) -> None:
        self._sim.request(quantity or self.quantity, reason)

    async def sell(self, quantity: int | None = None, reason: str | None = None) -> None:
        self._sim.request(-(quantity or self.quantity), reason)

    async def exit(self, reason: str | None = None) -> None:
        if self.position:
            self._sim.request(-self.position, reason)

    # Exact-price helpers are intentionally backtest-only. A strategy port can use them to
    # emulate resting stop orders from its source platform without changing live execution.
    async def buy_at(self, price: float, at: datetime, reason: str | None = None) -> None:
        self._sim.request_at(self.quantity, price, at, reason)

    async def sell_at(self, price: float, at: datetime, reason: str | None = None) -> None:
        self._sim.request_at(-self.quantity, price, at, reason)

    async def exit_at(self, price: float, at: datetime, reason: str | None = None) -> None:
        if self.position:
            self._sim.request_at(-self.position, price, at, reason)

    def mark(self, price: float, at: datetime) -> None:
        self._sim.mark(price, at)

    async def history(self, resolution: str = "D", days: int = 30) -> list:
        """Daily candles before the simulated day (never the day itself: its high and low
        are still in the future)."""
        if resolution != "D":
            raise OrderError("Backtests provide daily history only")
        today = self._sim.now.astimezone(IST).date()
        since = today - timedelta(days=days)
        return [c for c in self._sim.daily if since <= c.start.astimezone(IST).date() < today]

    def log(self, message: str) -> None:
        pass


@dataclass
class Simulation:
    deployment: Deployment
    symbol: str
    timeframe: str
    daily: list[Candle]
    capital: float = DEFAULT_CAPITAL
    size_pct: float = DEFAULT_SIZE_PCT
    tick_size: float | None = None
    position: int = 0
    last_price: float | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trades: list[SimTrade] = field(default_factory=list)
    blocked: int = 0
    _open: SimTrade | None = None
    _orders_day: date | None = None
    _orders_count: int = 0

    def __post_init__(self) -> None:
        self.max_orders_per_day = self.deployment.settings()["max_orders_per_day"]
        self.commission_per_unit = self.deployment.backtest_commission_per_unit
        self.realized = 0.0

    @property
    def quantity(self) -> int:
        """Units `size_pct` of the current equity buys at the last price (what buy() and sell() use)."""
        if self.deployment.backtest_fixed_quantity is not None:
            return self.deployment.backtest_fixed_quantity
        if not self.last_price:
            return 0
        return max(0, int((self.capital + self.realized) * self.size_pct / 100 // self.last_price))

    # ---------- orders ----------

    def request(self, delta: int, reason: str | None) -> None:
        """Buy (delta > 0) or sell (< 0) at the last price. Exits always go through; new
        exposure needs the equity to pay for it and must stay within orders per day."""
        if self.last_price is None:
            return
        if delta == 0:  # buy() / sell() when the equity can't pay for a single unit
            self.blocked += 1
            return
        new = self.position + delta
        reduces_only = abs(new) < abs(self.position) and new * self.position >= 0
        day = self.now.astimezone(IST).date()
        if day != self._orders_day:
            self._orders_day, self._orders_count = day, 0
        if not reduces_only and (abs(new) > max(self.quantity, abs(self.position))
                                 or self._orders_count >= self.max_orders_per_day):
            self.blocked += 1
            return
        self._orders_count += 1
        self._fill(delta, self.last_price, reason)

    def request_at(self, delta: int, price: float, at: datetime, reason: str | None) -> None:
        """The same risk-checked request, filled at an emulated resting-order price/time."""
        old_now, old_price = self.now, self.last_price
        self.now, self.last_price = at, price
        try:
            self.request(delta, reason)
        finally:
            self.now, self.last_price = old_now, old_price

    def mark(self, price: float, at: datetime) -> None:
        self.now, self.last_price = at, price
        if self._open:
            self._open.high = max(self._open.high, price)
            self._open.low = min(self._open.low, price)

    def _fill(self, delta: int, price: float, reason: str | None) -> None:
        pos = self.position
        if pos and (pos > 0) != (delta > 0):  # closes some or all of the open trade
            closing = min(abs(delta), abs(pos))
            t = self._open
            if closing < t.quantity:
                fee_share = t.entry_fees * closing / t.quantity
                self.trades.append(SimTrade(t.direction, closing, t.entry_at, t.entry_price, t.entry_reason,
                                            self.now, price, reason, t.high, t.low, fee_share,
                                            closing * self.commission_per_unit))
                t.quantity -= closing
                t.entry_fees -= fee_share
            else:
                t.exit_at, t.exit_price, t.exit_reason = self.now, price, reason
                t.exit_fees = closing * self.commission_per_unit
                self.trades.append(t)
                self._open = None
            self.realized += self.trades[-1].pnl
        self.position += delta
        if self.position and self._open is None:
            self._open = SimTrade("LONG" if self.position > 0 else "SHORT", abs(self.position),
                                  self.now, price, reason,
                                  entry_fees=abs(self.position) * self.commission_per_unit)
        elif self.position and abs(self.position) > self._open.quantity:  # added to it
            t, added = self._open, abs(self.position) - self._open.quantity
            t.entry_price = (t.entry_price * t.quantity + price * added) / abs(self.position)
            t.quantity = abs(self.position)
            t.entry_fees += added * self.commission_per_unit

    # ---------- replay ----------

    async def run(self, candles: list[Candle]) -> None:
        dep = self.deployment
        strategy = dep.strategy(dep.params, BacktestContext(self), TIMEFRAMES[self.timeframe])
        try:
            await strategy.on_start()
            candle_replay = getattr(strategy, "on_backtest_candle", None)
            if candle_replay is not None:
                for c in _aggregate(candles, TIMEFRAMES[self.timeframe]):
                    self.now, self.last_price = c.start, c.open
                    await candle_replay(c)
            else:
                for c in candles:
                    for at, price in _ticks(c):
                        self.mark(price, at)
                        await strategy.on_tick(Tick(self.symbol, price, at))
            await strategy.on_stop()
        except Exception as e:
            raise BacktestError(f"{dep.strategy.name} raised {type(e).__name__}: {e}") from e
        if self.position:
            self._fill(-self.position, self.last_price, "end of backtest")


def _ticks(c: Candle) -> list[tuple[datetime, float]]:
    """A 1-minute candle as four ticks inside its minute: a green candle most likely made its
    low before its high, a red one the other way round."""
    first, second = (c.low, c.high) if c.close >= c.open else (c.high, c.low)
    s = c.start
    return [(s, c.open), (s + timedelta(seconds=20), first), (s + timedelta(seconds=40), second),
            (s + timedelta(seconds=59), c.close)]


def _aggregate(candles: list[Candle], seconds: int) -> list[Candle]:
    """Aggregate chronological 1-minute candles into epoch-aligned strategy candles."""
    if seconds == 60:
        return candles
    out: list[Candle] = []
    bucket: int | None = None
    current: Candle | None = None
    for c in candles:
        next_bucket = int(c.start.timestamp()) // seconds
        if next_bucket != bucket:
            if current is not None:
                out.append(current)
            bucket = next_bucket
            start = datetime.fromtimestamp(next_bucket * seconds, tz=c.start.tzinfo)
            current = Candle(start, c.open, c.high, c.low, c.close)
        else:
            current = Candle(current.start, current.open, max(current.high, c.high),
                             min(current.low, c.low), c.close, current.volume + c.volume)
    if current is not None:
        out.append(current)
    return out


# ---------- report ----------


def _r(v: float | None) -> float | None:
    return round(v, 2) if v is not None else None


def summarize(trades: list[SimTrade], capital: float = DEFAULT_CAPITAL) -> dict:
    """Performance of these trades, percentages against `capital`."""
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    cum = peak = drawdown = drawdown_pct = run_up = 0.0
    trough = 0.0
    streak = worst_streak = 0
    for trade, p in zip(trades, pnls):
        # TradingView measures each trade against the greatest *closed* equity seen
        # before it opened, then adds that trade's maximum adverse excursion.
        # It does not promote an unrealized intratrade high to the equity peak.
        intrabar_drawdown = peak - cum + trade.drawdown + trade.commission
        drawdown = max(drawdown, intrabar_drawdown)
        drawdown_pct = max(drawdown_pct, intrabar_drawdown / (capital + peak) * 100)
        cum += p
        peak = max(peak, cum)
        drawdown = max(drawdown, peak - cum)
        drawdown_pct = max(drawdown_pct, (peak - cum) / (capital + peak) * 100)
        trough = min(trough, cum)
        run_up = max(run_up, cum - trough)
        streak = streak + 1 if p <= 0 else 0
        worst_streak = max(worst_streak, streak)
    gross_profit, gross_loss = sum(wins), -sum(losses)
    holds = [(t.exit_at - t.entry_at).total_seconds() / 60 for t in trades]
    days = {t.entry_at.astimezone(IST).date() for t in trades}
    net = sum(pnls)
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": _r(len(wins) / len(trades) * 100) if trades else None,
        "net_pnl": _r(net), "net_pnl_pct": _r(net / capital * 100),
        "gross_profit": _r(gross_profit), "gross_loss": _r(gross_loss),
        "profit_factor": _r(gross_profit / gross_loss) if gross_loss else None,
        "avg_trade": _r(net / len(pnls)) if pnls else None,
        "avg_win": _r(gross_profit / len(wins)) if wins else None,
        "avg_loss": _r(-gross_loss / len(losses)) if losses else None,
        "ratio_avg_win_loss": _r((gross_profit / len(wins)) / (gross_loss / len(losses))) if wins and gross_loss else None,
        "best": _r(max(pnls)) if pnls else None, "worst": _r(min(pnls)) if pnls else None,
        "max_drawdown": _r(drawdown), "max_drawdown_pct": _r(drawdown_pct),
        "max_run_up": _r(run_up), "max_run_up_pct": _r(run_up / capital * 100),
        "avg_hold_minutes": _r(sum(holds) / len(holds)) if holds else None,
        "max_consecutive_losses": worst_streak, "trading_days": len(days),
    }


def trade_rows(trades: list[SimTrade]) -> list[dict]:
    cum, rows = 0.0, []
    for n, t in enumerate(trades, 1):
        cum += t.pnl
        notional = t.entry_price * t.quantity
        rows.append({
            "number": n, "direction": t.direction, "quantity": t.quantity,
            "entry_at": t.entry_at.isoformat(), "entry_price": round(t.entry_price, 4),
            "exit_at": t.exit_at.isoformat(), "exit_price": round(t.exit_price, 4),
            "notional": _r(notional), "pnl": _r(t.pnl), "return_pct": _r(t.pnl / notional * 100) if notional else None,
            "commission": _r(t.commission),
            "run_up": _r(t.run_up), "drawdown": _r(t.drawdown), "cum_pnl": _r(cum),
            "entry_reason": t.entry_reason, "exit_reason": t.exit_reason,
        })
    return rows


@dataclass
class Run:
    """One simulation over a range, with its capital and order size."""
    deployment_key: str
    symbol: str
    timeframe: str
    capital: float
    size_pct: float
    start: date
    end: date
    candles: int
    data_from: datetime
    data_to: datetime
    blocked: int
    trades: list[SimTrade]
    closes: list[tuple[datetime, float]]  # last price of each day, for buy & hold
    fixed_quantity: int | None = None
    commission_per_unit: float = 0.0

    def report(self) -> dict:
        capital = self.capital
        longs = [t for t in self.trades if t.long]
        shorts = [t for t in self.trades if not t.long]
        first = self.closes[0][1] if self.closes else None
        units = capital / first if first else 0
        return {
            "deployment_key": self.deployment_key, "symbol": self.symbol, "timeframe": self.timeframe,
            "capital": capital, "size_pct": self.size_pct,
            "fixed_quantity": self.fixed_quantity,
            "commission_per_unit": self.commission_per_unit,
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "data_from": self.data_from.isoformat(), "data_to": self.data_to.isoformat(),
            "candles": self.candles, "blocked_signals": self.blocked, "assumptions": ASSUMPTIONS,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {"all": summarize(self.trades, capital), "long": summarize(longs, capital),
                        "short": summarize(shorts, capital)},
            "trades": trade_rows(self.trades),
            # What buying `capital` worth at the first close and holding would have made.
            "buy_hold": [{"t": at.isoformat(), "close": c, "pnl": _r((c - first) * units)} for at, c in self.closes],
        }


# ---------- running, with caches ----------


async def fetch_minutes(broker: Broker, symbol: str, first: date, last: date) -> tuple[list[Candle], bool]:
    """1-minute candles from `first` to `last`, in chunks the brokers accept, newest first.
    Also returns whether the broker ran out of history before `first`."""
    chunks: list[list[Candle]] = []
    end = last
    exhausted = False
    while end >= first:
        start = max(first, end - timedelta(days=CHUNK_DAYS - 1))
        try:
            got = await broker.get_history(symbol, "1", start, end)
        except OrderError:
            if not chunks:
                raise
            exhausted = True
            break  # older history isn't available
        if not got and chunks:
            exhausted = True
            break
        chunks.append(got)
        end = start - timedelta(days=1)
    return [c for chunk in reversed(chunks) for c in sorted(chunk, key=lambda c: c.start)], exhausted


@dataclass
class History:
    """The 1-minute and daily candles fetched for a symbol today; grows backwards on demand."""
    minutes: list[Candle]
    daily: list[Candle]
    since: date  # minutes are complete from here
    exhausted: bool = False
    tick_size: float | None = None


class Backtester:
    """Runs backtests off the event loop (they are CPU-bound and the loop runs live strategies),
    one at a time, and remembers results for the rest of the IST day."""

    def __init__(self, max_results: int = 64, max_histories: int = 3) -> None:
        self._results: OrderedDict[tuple, Run] = OrderedDict()
        self._histories: OrderedDict[tuple, History] = OrderedDict()
        self._inflight: dict[tuple, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self.max_results, self.max_histories = max_results, max_histories

    @staticmethod
    def check_range(start: date | None, end: date | None) -> tuple[date, date]:
        today = datetime.now(IST).date()
        end = min(end or today, today)
        start = start or end - timedelta(days=DEFAULT_DAYS)
        if start > end:
            raise BacktestError("The start date is after the end date")
        if (end - start).days > MAX_RANGE_DAYS:
            raise BacktestError(f"Pick a range of at most {MAX_RANGE_DAYS} days")
        return start, end

    async def run(self, account_id: int, broker: Broker, dep: Deployment, symbol: str, timeframe: str,
                  start: date, end: date, capital: float = DEFAULT_CAPITAL, size_pct: float = DEFAULT_SIZE_PCT) -> Run:
        today = datetime.now(IST).date()
        backtest_settings = (dep.backtest_fixed_quantity, dep.backtest_commission_per_unit)
        key = (account_id, dep.key, repr(dep.settings()), backtest_settings,
               symbol, timeframe, start, end, capital, size_pct, today)
        if key in self._results:
            self._results.move_to_end(key)
            return self._results[key]
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            async with self._lock:
                history = await self._history(account_id, broker, symbol, today, start)
                result = await asyncio.to_thread(_run_sync, dep, symbol, timeframe, history, start, end, capital, size_pct)
            self._results[key] = result
            while len(self._results) > self.max_results:
                self._results.popitem(last=False)
            future.set_result(result)
            return result
        except BaseException as e:
            future.set_exception(e)
            future.exception()  # retrieved: no "never retrieved" warning when nobody else waits
            raise
        finally:
            del self._inflight[key]

    async def _history(self, account_id: int, broker: Broker, symbol: str, today: date, start: date) -> History:
        key = (account_id, symbol, today)
        h = self._histories.get(key)
        if h is None:
            minutes, exhausted = await fetch_minutes(broker, symbol, start, today)
            if not minutes:
                raise BacktestError(f"The broker has no 1-minute history for {symbol}")
            daily = await broker.get_history(symbol, "D", today - timedelta(days=MAX_RANGE_DAYS + 60), today)
            h = self._histories[key] = History(minutes, daily, start, exhausted, await broker.get_tick_size(symbol))
            while len(self._histories) > self.max_histories:
                self._histories.popitem(last=False)
        elif start < h.since and not h.exhausted:
            older, h.exhausted = await fetch_minutes(broker, symbol, start, h.since - timedelta(days=1))
            first = h.minutes[0].start if h.minutes else None
            h.minutes = [c for c in older if first is None or c.start < first] + h.minutes
            h.since = start
        self._histories.move_to_end(key)
        return h


def _run_sync(dep: Deployment, symbol: str, timeframe: str, history: History, start: date, end: date,
              capital: float, size_pct: float) -> Run:
    candles = [c for c in history.minutes if start <= c.start.astimezone(IST).date() <= end]
    if not candles:
        raise BacktestError(f"No 1-minute history for {symbol} between {start} and {end}")
    sim = Simulation(dep, symbol, timeframe, history.daily, capital, size_pct, history.tick_size)
    asyncio.run(sim.run(candles))
    closes: dict[date, tuple[datetime, float]] = {}
    for c in candles:
        closes[c.start.astimezone(IST).date()] = (c.start, c.close)
    return Run(dep.key, symbol, timeframe, capital, size_pct, start, end, len(candles), candles[0].start,
               candles[-1].start, sim.blocked, sim.trades, list(closes.values()),
               dep.backtest_fixed_quantity, dep.backtest_commission_per_unit)
