"""Combined NIFTY limited-trade intraday and overnight strategy.

This is a direct port of the supplied TradingView strategy.  Signals are calculated on
completed five-minute candles and become market orders on the next candle.  Each entry has
a fixed point stop and target, no more than three entries are queued per session, and any
remaining position is closed at the 15:15 open. It then applies the documented overnight
direction rule and holds that position until the next trading session's 09:15 open.
"""

from collections import deque
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from app.brokers import OrderError
from app.brokers.base import Candle
from app.orders.schemas import Product
from app.strategies.base import Bar, Strategy, Tick, before_market_open

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_LAST_BAR = time(15, 25)
SIGNALS_FROM = time(10, 45)
SIGNALS_UNTIL = time(14, 25)
EXIT_DECISION_BAR = time(15, 10)
SQUARE_OFF = time(15, 15)
EXIT_RETRY = timedelta(seconds=5)
HISTORY_RETRY = timedelta(seconds=5)


class NiftyIntradayLimited(Strategy):
    checkpoint_version = 3
    name = "nifty_intraday_limited"
    description = (
        "NIFTY intraday momentum entries from a session-reset z-score and EMA(4/10), "
        "followed by a 15:15 directional overnight position closed at the next session open."
    )
    tags = ("Intraday + overnight", "NIFTY", "Long & short", "Limited trades")
    timeframes = ("5m",)
    default_timeframe = "5m"
    capital_sizing = False
    backtest_note = (
        "Uses next-bar market fills and the documented conservative stop-first rule when both "
        "intraday exits occur in one candle. The first quiet session stays flat when the prior "
        "15:15 price is outside the requested history. Live market fills may include slippage."
    )

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        lookback: int = Field(18, ge=2, description="Session-reset SMA/z-score lookback")
        z_threshold: float = Field(1.0, ge=0.1, description="Absolute z-score entry threshold")
        stop_points: float = Field(50, gt=0, description="Stop distance in NIFTY points")
        target_points: float = Field(300, gt=0, description="Target distance in NIFTY points")
        max_entries_per_day: int = Field(3, ge=1, le=3, description="Maximum entries each session")

    @classmethod
    def rules(cls, p: "NiftyIntradayLimited.Params") -> list[dict]:
        return [
            {"kind": "filter", "title": "When it trades", "conditions": [
                "5-minute regular-session NIFTY candles only",
                f"Signals on candles from {SIGNALS_FROM:%H:%M} through {SIGNALS_UNTIL:%H:%M} IST; fills at the next candle open",
                f"At most {p.max_entries_per_day} entries per session",
            ]},
            {"kind": "entry_long", "title": "Buy", "conditions": [
                f"The {p.lookback}-bar session-reset z-score is at least {p.z_threshold:g}",
                "Session-reset EMA(4) is above EMA(10)",
                "Position is flat",
            ]},
            {"kind": "entry_short", "title": "Sell short", "conditions": [
                f"The {p.lookback}-bar session-reset z-score is at most -{p.z_threshold:g}",
                "Session-reset EMA(4) is below EMA(10)",
                "Position is flat",
            ]},
            {"kind": "risk", "title": "Fixed bracket", "conditions": [
                f"Stop-loss {p.stop_points:g} NIFTY points from the actual entry fill",
                f"Profit target {p.target_points:g} NIFTY points from the actual entry fill",
                "No pyramiding; stop or target closes the whole position",
            ]},
            {"kind": "exit", "title": "End of day", "conditions": [
                f"The close instruction after the {EXIT_DECISION_BAR:%H:%M} candle fills at the {SQUARE_OFF:%H:%M} open",
                "The intraday position is closed before the overnight decision is executed",
            ]},
            {"kind": "entry_long", "title": "Overnight long", "conditions": [
                "At 15:15, follow today's move when its absolute size is at least 60 points",
                "On a quieter day, follow the completed gap from the prior 15:15 price to today's 09:15 open",
                "Enter long when the selected value is positive; remain flat when it is zero",
            ]},
            {"kind": "entry_short", "title": "Overnight short", "conditions": [
                "Enter short when the selected 15:15 session move or prior overnight gap is negative",
                "One fixed position with no overnight stop or target",
            ]},
            {"kind": "exit", "title": "Overnight exit", "conditions": [
                "Exit unconditionally at the next complete trading session's 09:15 open",
                "Weekend and exchange-holiday gaps remain exposed",
            ]},
        ]

    async def on_start(self) -> None:
        self.day: date | None = None
        self.closes: deque[float] = deque(maxlen=self.params.lookback)
        self.ema4: float | None = None
        self.ema10: float | None = None
        self.entries_today = 0
        self.session_open: float | None = None
        self.previous_1515: float | None = None
        self.stop: float | None = None
        self.target: float | None = None
        self._direction = 0
        self._trade_kind: str | None = None
        self._entry_pending = 0
        self._overnight_pending = 0
        self._overnight_decided_day: date | None = None
        self._overnight_sent_at: datetime | None = None
        self._morning_exit_pending = False
        self._exit_pending = False
        self._exit_sent_at: datetime | None = None
        self._pending_bar_closes: list[datetime] = []
        self._last_completed_close: datetime | None = None
        self._history_retry_at: datetime | None = None
        self._last_checkpoint: dict | None = None
        restored = self._restore_checkpoint()
        self._last_checkpoint = self._checkpoint()
        if self.ctx.position:
            actual = 1 if self.ctx.position > 0 else -1
            intraday_restored = (self._trade_kind == "intraday" and self._direction == actual
                                 and self.stop is not None and self.target is not None)
            overnight_restored = self._trade_kind == "overnight" and self._direction == actual
            fill_pending = self._entry_pending == actual or self._overnight_pending == actual
            if not restored or not (intraday_restored or overnight_restored or fill_pending):
                raise RuntimeError(
                    "broker position exists but no compatible limited-trade checkpoint; "
                    "position cannot be managed safely"
                )

    # ---------- durable live state ----------

    def _checkpoint(self) -> dict:
        return {
            "version": self.checkpoint_version,
            "strategy": self.name,
            "bar_seconds": self.bar_seconds,
            "params": self.params.model_dump(mode="json"),
            "day": self.day.isoformat() if self.day else None,
            "closes": list(self.closes),
            "ema4": self.ema4,
            "ema10": self.ema10,
            "entries_today": self.entries_today,
            "session_open": self.session_open,
            "previous_1515": self.previous_1515,
            "stop": self.stop,
            "target": self.target,
            "direction": self._direction,
            "trade_kind": self._trade_kind,
            "entry_pending": self._entry_pending,
            "overnight_pending": self._overnight_pending,
            "overnight_decided_day": (self._overnight_decided_day.isoformat()
                                       if self._overnight_decided_day else None),
            "overnight_sent_at": (self._overnight_sent_at.isoformat()
                                   if self._overnight_sent_at else None),
            "morning_exit_pending": self._morning_exit_pending,
            "exit_pending": self._exit_pending,
            "exit_sent_at": self._exit_sent_at.isoformat() if self._exit_sent_at else None,
            "pending_bar_closes": [value.isoformat() for value in self._pending_bar_closes],
            "last_completed_close": (self._last_completed_close.isoformat()
                                     if self._last_completed_close else None),
        }

    def _restore_checkpoint(self) -> bool:
        state = getattr(self.ctx, "strategy_state", None)
        if not isinstance(state, dict) or not state:
            return False
        expected = {
            "version": self.checkpoint_version,
            "strategy": self.name,
            "bar_seconds": self.bar_seconds,
            "params": self.params.model_dump(mode="json"),
        }
        if any(state.get(key) != value for key, value in expected.items()):
            return False
        try:
            direction = int(state.get("direction", 0))
            entry_pending = int(state.get("entry_pending", 0))
            overnight_pending = int(state.get("overnight_pending", 0))
            trade_kind = state.get("trade_kind")
            if (direction not in (-1, 0, 1) or entry_pending not in (-1, 0, 1)
                    or overnight_pending not in (-1, 0, 1)
                    or trade_kind not in (None, "intraday", "overnight")):
                return False
            closes = [float(value) for value in state.get("closes", [])]
            if len(closes) > self.params.lookback:
                return False
            self.day = date.fromisoformat(state["day"]) if state.get("day") else None
            self.closes.extend(closes)
            self.ema4 = self._optional_float(state.get("ema4"))
            self.ema10 = self._optional_float(state.get("ema10"))
            self.entries_today = int(state.get("entries_today", 0))
            self.session_open = self._optional_float(state.get("session_open"))
            self.previous_1515 = self._optional_float(state.get("previous_1515"))
            self.stop = self._optional_float(state.get("stop"))
            self.target = self._optional_float(state.get("target"))
            self._direction = direction
            self._trade_kind = trade_kind
            self._entry_pending = entry_pending
            self._overnight_pending = overnight_pending
            self._overnight_decided_day = (date.fromisoformat(state["overnight_decided_day"])
                                           if state.get("overnight_decided_day") else None)
            self._overnight_sent_at = (datetime.fromisoformat(state["overnight_sent_at"])
                                       if state.get("overnight_sent_at") else None)
            self._morning_exit_pending = bool(state.get("morning_exit_pending"))
            self._exit_pending = bool(state.get("exit_pending"))
            self._exit_sent_at = (datetime.fromisoformat(state["exit_sent_at"])
                                  if state.get("exit_sent_at") else None)
            self._pending_bar_closes = [
                datetime.fromisoformat(value) for value in state.get("pending_bar_closes", [])
            ]
            self._last_completed_close = (datetime.fromisoformat(state["last_completed_close"])
                                          if state.get("last_completed_close") else None)
            if (any(value.tzinfo is None for value in self._pending_bar_closes)
                    or (self._last_completed_close is not None
                        and self._last_completed_close.tzinfo is None)):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _optional_float(value) -> float | None:
        return float(value) if value is not None else None

    async def _save_checkpoint(self) -> None:
        save = getattr(self.ctx, "save_strategy_state", None)
        if save is None:
            return
        checkpoint = self._checkpoint()
        if checkpoint != self._last_checkpoint:
            await save(checkpoint)
            self._last_checkpoint = checkpoint

    def _new_session(self, day: date) -> None:
        self.day = day
        self.closes.clear()
        self.ema4 = None
        self.ema10 = None
        self.entries_today = 0
        self.session_open = None
        self._entry_pending = 0
        self._pending_bar_closes.clear()
        self._last_completed_close = None
        self._history_retry_at = None

    def _clear_trade(self) -> None:
        self.stop = self.target = None
        self._direction = 0
        self._trade_kind = None
        self._exit_pending = False
        self._exit_sent_at = None

    # ---------- indicator and signal calculation ----------

    @staticmethod
    def _regular_bar(start: datetime) -> bool:
        value = start.astimezone(IST).time()
        return SESSION_OPEN <= value <= SESSION_LAST_BAR

    def _update_indicators(self, close: float) -> float | None:
        if self.ema4 is None:
            self.ema4 = self.ema10 = close
        else:
            self.ema4 = (2.0 / 5.0) * close + (3.0 / 5.0) * self.ema4
            self.ema10 = (2.0 / 11.0) * close + (9.0 / 11.0) * self.ema10
        self.closes.append(close)
        if len(self.closes) < self.params.lookback:
            return None
        mean = sum(self.closes) / self.params.lookback
        variance = max(sum(value * value for value in self.closes) / self.params.lookback - mean * mean, 0.0)
        sd = variance ** 0.5
        return (close - mean) / sd if sd > 0 else None

    def _signal(self, start: datetime, z_score: float | None, flat: bool) -> int:
        local_time = start.astimezone(IST).time()
        if (not flat or z_score is None or self.entries_today >= self.params.max_entries_per_day
                or not SIGNALS_FROM <= local_time <= SIGNALS_UNTIL):
            return 0
        if z_score >= self.params.z_threshold and self.ema4 > self.ema10:
            return 1
        if z_score <= -self.params.z_threshold and self.ema4 < self.ema10:
            return -1
        return 0

    def _install_bracket(self, fill: float, direction: int) -> None:
        self._direction = direction
        self._trade_kind = "intraday"
        if direction > 0:
            self.stop = fill - self.params.stop_points
            self.target = fill + self.params.target_points
        else:
            self.stop = fill + self.params.stop_points
            self.target = fill - self.params.target_points

    def _overnight_direction(self, close_1515: float) -> int:
        if self.session_open is None:
            return 0
        session_move = close_1515 - self.session_open
        if abs(session_move) >= 60:
            selected = session_move
        elif self.previous_1515 is not None:
            selected = self.session_open - self.previous_1515
        else:
            return 0
        return 1 if selected > 0 else -1 if selected < 0 else 0

    def _install_overnight(self, direction: int) -> None:
        self.stop = self.target = None
        self._direction = direction
        self._trade_kind = "overnight"
        self._overnight_pending = 0
        self._overnight_sent_at = None

    # ---------- TradingView-compatible tester replay ----------

    async def on_backtest_candle(self, candle: Candle) -> None:
        start = candle.start.astimezone(IST)
        if not self._regular_bar(start):
            return
        if start.date() != self.day:
            if self.ctx.position:
                self.ctx.mark(candle.open, start)
                await self.ctx.exit_at(candle.open, start, "overnight exit at 09:15")
                self._clear_trade()
            self._new_session(start.date())
            self.session_open = candle.open

        self.ctx.mark(candle.open, start)
        if self._exit_pending and self.ctx.position:
            await self.ctx.exit_at(candle.open, start, f"square-off at {SQUARE_OFF:%H:%M}")
            self._clear_trade()
        elif self._entry_pending and self.ctx.position == 0:
            direction, self._entry_pending = self._entry_pending, 0
            if direction > 0:
                await self.ctx.buy_at(candle.open, start, "long z-score/EMA signal")
            else:
                await self.ctx.sell_at(candle.open, start, "short z-score/EMA signal")
            if self.ctx.position:
                self._install_bracket(candle.open, direction)

        if start.time() == SQUARE_OFF and self._overnight_decided_day != start.date():
            direction = self._overnight_direction(candle.open)
            self.previous_1515 = candle.open
            self._overnight_decided_day = start.date()
            if direction and self.ctx.position == 0:
                if direction > 0:
                    await self.ctx.buy_at(candle.open, start, "overnight long at 15:15")
                else:
                    await self.ctx.sell_at(candle.open, start, "overnight short at 15:15")
                if self.ctx.position:
                    self._install_overnight(direction)

        # The research specification deliberately resolves an ambiguous candle against the
        # strategy: when both bracket prices occur in one 5-minute candle, record the stop.
        both_hit = False
        if self._trade_kind == "intraday" and self.ctx.position > 0:
            both_hit = candle.low <= self.stop and candle.high >= self.target
        elif self._trade_kind == "intraday" and self.ctx.position < 0:
            both_hit = candle.high >= self.stop and candle.low <= self.target
        if both_hit:
            fill = (candle.open if (self.ctx.position > 0 and candle.open <= self.stop)
                    or (self.ctx.position < 0 and candle.open >= self.stop) else self.stop)
            self.ctx.mark(fill, start)
            await self.ctx.exit_at(fill, start, f"stop {self.stop:.2f}; stop-first ambiguous candle")
            self._clear_trade()

        first, second = ((candle.high, candle.low)
                         if abs(candle.open - candle.high) < abs(candle.open - candle.low)
                         else (candle.low, candle.high))
        previous: float | None = None
        for price in (candle.open, first, second, candle.close):
            exit_price: float | None = None
            reason: str | None = None
            if self._trade_kind == "intraday" and self.ctx.position > 0:
                if price <= self.stop:
                    exit_price, reason = (price if previous is None else self.stop), f"stop {self.stop:.2f}"
                elif price >= self.target:
                    exit_price, reason = (price if previous is None else self.target), f"target {self.target:.2f}"
            elif self._trade_kind == "intraday" and self.ctx.position < 0:
                if price >= self.stop:
                    exit_price, reason = (price if previous is None else self.stop), f"stop {self.stop:.2f}"
                elif price <= self.target:
                    exit_price, reason = (price if previous is None else self.target), f"target {self.target:.2f}"
            if exit_price is not None:
                self.ctx.mark(exit_price, start)
                await self.ctx.exit_at(exit_price, start, reason)
                self._clear_trade()
            self.ctx.mark(price, start)
            previous = price

        z_score = self._update_indicators(candle.close)
        if (start.time() == EXIT_DECISION_BAR and self.ctx.position
                and self._trade_kind == "intraday"):
            self._exit_pending = True
        elif start.time() < SQUARE_OFF and self.ctx.position == 0 and not self._entry_pending:
            signal = self._signal(start, z_score, flat=True)
            if signal:
                self._entry_pending = signal
                self.entries_today += 1

    # ---------- live execution ----------

    async def on_tick(self, tick: Tick) -> None:
        if before_market_open(tick.at):
            return
        now = tick.at.astimezone(IST)
        if now.date() != self.day:
            self._new_session(now.date())
            self.session_open = tick.price
            if self.ctx.position:
                self._morning_exit_pending = True
                await self._save_checkpoint()
                await self._request_exit(now, "overnight exit at 09:15")
            else:
                self._clear_trade()
            await self._save_checkpoint()
        elif self.session_open is None:
            self.session_open = tick.price
            await self._save_checkpoint()
        if bar := self._bars.add(tick):
            await self.on_bar(bar)
        else:
            await self._retry_pending_bars(now)
        await self._reconcile_entry(tick.price)
        await self._reconcile_overnight(tick.price)
        await self._reconcile_morning_exit()
        if now.time() >= SQUARE_OFF:
            await self._begin_overnight(tick.price, now)
        await self._manage_price(tick.price, now)

    async def on_bar(self, bar: Bar) -> None:
        closed_at = (bar.start + timedelta(seconds=bar.seconds)).astimezone(IST)
        start = bar.start.astimezone(IST)
        if not self._regular_bar(start) or start.date() != self.day:
            return
        self._queue_closed_bar(closed_at)
        await self._save_checkpoint()
        await self._retry_pending_bars(closed_at)

    def _queue_closed_bar(self, closed_at: datetime) -> None:
        if self._last_completed_close is not None and closed_at <= self._last_completed_close:
            return
        if closed_at not in self._pending_bar_closes:
            self._pending_bar_closes.append(closed_at)
            self._pending_bar_closes.sort()

    async def _retry_pending_bars(self, now: datetime) -> None:
        if not self._pending_bar_closes:
            return
        if self._history_retry_at is not None and now < self._history_retry_at:
            return
        try:
            candles = await self.ctx.history("1", days=1)
        except OrderError as error:
            self.ctx.log(f"completed candle history unavailable ({error}); retrying")
            self._history_retry_at = now + HISTORY_RETRY
            return

        while self._pending_bar_closes:
            closed_at = self._pending_bar_closes[0]
            completed = self._history_bar(candles, closed_at)
            if completed is None:
                self.ctx.log(f"completed 5-minute candle ending {closed_at:%H:%M} not published yet; retrying")
                self._history_retry_at = now + HISTORY_RETRY
                return
            self._pending_bar_closes.pop(0)
            await self._on_completed_bar(completed)
            self._last_completed_close = closed_at
            await self._save_checkpoint()
        self._history_retry_at = None

    def _history_bar(self, candles: list[Candle], closed_at: datetime) -> Bar | None:
        start = closed_at - timedelta(seconds=self.bar_seconds)
        rows = sorted(
            (candle for candle in candles
             if start <= candle.start.astimezone(IST) < closed_at),
            key=lambda candle: candle.start,
        )
        expected = self.bar_seconds // 60
        if len(rows) != expected:
            return None
        expected_starts = [start + timedelta(minutes=index) for index in range(expected)]
        if [row.start.astimezone(IST) for row in rows] != expected_starts:
            return None
        return Bar(
            self.ctx.symbol,
            start,
            self.bar_seconds,
            rows[0].open,
            max(row.high for row in rows),
            min(row.low for row in rows),
            rows[-1].close,
            len(rows) * 4,
        )

    async def _on_completed_bar(self, bar: Bar) -> None:
        start = bar.start.astimezone(IST)
        if start.time() == SESSION_OPEN:
            self.session_open = bar.open
        z_score = self._update_indicators(bar.close)

        if start.time() == EXIT_DECISION_BAR:
            self._entry_pending = 0
            if self.ctx.position and self._trade_kind == "intraday":
                self._exit_pending = True
                await self._request_exit(start + timedelta(seconds=bar.seconds), "square-off at 15:15")
            await self._save_checkpoint()
            return

        if (start.time() < SQUARE_OFF and self.ctx.position == 0 and not self.ctx.order_pending
                and not self._entry_pending and not self._morning_exit_pending):
            signal = self._signal(start, z_score, flat=True)
            if signal:
                self.entries_today += 1
                self._entry_pending = signal
                await self._save_checkpoint()
                if signal > 0:
                    await self.ctx.buy(
                        reason="long z-score/EMA signal", product=Product.INTRADAY,
                    )
                else:
                    await self.ctx.sell(
                        reason="short z-score/EMA signal", product=Product.INTRADAY,
                    )
                await self._reconcile_entry(getattr(self.ctx, "last_price", None) or bar.close)
        await self._save_checkpoint()

    async def _reconcile_entry(self, price: float) -> None:
        if not self._entry_pending:
            return
        actual = 1 if self.ctx.position > 0 else -1 if self.ctx.position < 0 else 0
        if actual == self._entry_pending:
            fill = getattr(self.ctx, "last_fill_price", None) or price
            self._install_bracket(fill, actual)
            self._entry_pending = 0
            await self._save_checkpoint()
        elif not self.ctx.order_pending:
            self._entry_pending = 0  # synchronously rejected or cancelled
            await self._save_checkpoint()

    async def _begin_overnight(self, price: float, now: datetime) -> None:
        if self.session_open is None:
            return
        if self._overnight_decided_day != now.date():
            self._overnight_pending = self._overnight_direction(price)
            self.previous_1515 = price
            self._overnight_decided_day = now.date()
            self._overnight_sent_at = None
            if self.ctx.position and self._trade_kind == "intraday":
                self._exit_pending = True
                await self._save_checkpoint()
                await self._request_exit(now, "square-off at 15:15 before overnight entry")
            else:
                await self._save_checkpoint()
        await self._maybe_open_overnight(now)

    async def _maybe_open_overnight(self, now: datetime) -> None:
        direction = self._overnight_pending
        if not direction or self.ctx.position or self.ctx.order_pending or self._morning_exit_pending:
            return
        if self._overnight_sent_at is not None and now - self._overnight_sent_at < EXIT_RETRY:
            return
        self._overnight_sent_at = now
        await self._save_checkpoint()
        if direction > 0:
            await self.ctx.buy(
                reason="overnight long at 15:15", product=Product.DELIVERY,
            )
        else:
            await self.ctx.sell(
                reason="overnight short at 15:15", product=Product.DELIVERY,
            )
        await self._reconcile_overnight(getattr(self.ctx, "last_price", None) or 0)

    async def _reconcile_overnight(self, price: float) -> None:
        if not self._overnight_pending:
            return
        actual = 1 if self.ctx.position > 0 else -1 if self.ctx.position < 0 else 0
        if actual == self._overnight_pending:
            self._install_overnight(actual)
            await self._save_checkpoint()

    async def _reconcile_morning_exit(self) -> None:
        if self._morning_exit_pending and self.ctx.position == 0 and not self.ctx.order_pending:
            self._morning_exit_pending = False
            self._clear_trade()
            await self._save_checkpoint()

    async def _manage_price(self, price: float, now: datetime) -> None:
        if self.ctx.position == 0:
            if self._direction and not self.ctx.order_pending and not self._overnight_pending:
                self._clear_trade()
                await self._save_checkpoint()
            return
        if self._trade_kind == "overnight":
            if self._morning_exit_pending:
                await self._request_exit(now, "overnight exit at 09:15")
            return
        if self._exit_pending:
            if now.time() >= SQUARE_OFF:
                await self._request_exit(now, f"square-off at {SQUARE_OFF:%H:%M}")
            return
        if self.stop is None or self.target is None or self.ctx.order_pending:
            return
        if self.ctx.position > 0:
            reason = (f"stop {self.stop:.2f} hit" if price <= self.stop else
                      f"target {self.target:.2f} hit" if price >= self.target else None)
        else:
            reason = (f"stop {self.stop:.2f} hit" if price >= self.stop else
                      f"target {self.target:.2f} hit" if price <= self.target else None)
        if reason:
            await self._request_exit(now, reason)

    async def _request_exit(self, now: datetime, reason: str) -> None:
        if self.ctx.order_pending:
            return
        if self._exit_sent_at is not None and now - self._exit_sent_at < EXIT_RETRY:
            return
        self._exit_sent_at = now
        await self._save_checkpoint()
        product = (Product.DELIVERY if self._trade_kind == "overnight"
                   else Product.INTRADAY)
        await self.ctx.exit(reason=reason, product=product)

    async def on_clock(self, now: datetime) -> None:
        local = now.astimezone(IST)
        if local.date() != self.day and local.time() >= SESSION_OPEN:
            self._new_session(local.date())
            if self.ctx.position:
                self._morning_exit_pending = True
                await self._save_checkpoint()
                await self._request_exit(local, "overnight exit at 09:15")
            else:
                self._clear_trade()
                await self._save_checkpoint()
        if local.date() == self.day:
            timestamp = int(local.timestamp()) // self.bar_seconds * self.bar_seconds
            closed_at = datetime.fromtimestamp(timestamp, tz=IST)
            candidate_start = closed_at - timedelta(seconds=self.bar_seconds)
            if self._regular_bar(candidate_start) and candidate_start.date() == self.day:
                self._queue_closed_bar(closed_at)
                await self._save_checkpoint()
            await self._retry_pending_bars(local)
            if self.ctx.last_price is not None:
                await self._reconcile_entry(self.ctx.last_price)
        await self._reconcile_morning_exit()
        if local.time() >= SQUARE_OFF and self.ctx.last_price is not None:
            await self._begin_overnight(self.ctx.last_price, local)
