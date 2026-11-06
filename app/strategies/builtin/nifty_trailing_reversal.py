"""Persistent NIFTY percentage-stop reversal strategy.

This ports the supplied TradingView "Explicit orders v3" script.  By default the first
loaded regular-session candle queues the initial order, which fills at the next candle open.
Exact-seed mode instead queues on the candle ending at the configured seed time.  Stops are
armed after a fill candle closes, ratchet from scheduled closes, and carry overnight.
"""

from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.brokers.base import Candle
from app.strategies.base import TIMEFRAMES, Bar, Strategy, Tick

IST = ZoneInfo("Asia/Kolkata")


class NiftyTrailingReversal(Strategy):
    checkpoint_version = 1
    name = "nifty_trailing_reversal"
    description = (
        "Always-in NIFTY long/short reversal: a 0.8% protective stop ratcheted to 0.4% "
        "behind scheduled closes, with positions carried overnight."
    )
    tags = ("Positional", "NIFTY", "Long & short", "Trailing stop")
    timeframes = tuple(TIMEFRAMES)
    default_timeframe = "5m"
    capital_sizing = True
    backtest_note = (
        "The 5-minute result follows the Pine script's completed-candle approximation. Other "
        "timeframes are supported variants: a trail update occurs when their completed bar "
        "crosses a scheduled cadence boundary. At most one reversal is processed per candle."
    )

    class Params(BaseModel):
        initial_stop_pct: float = Field(0.8, gt=0, lt=100, description="Initial stop, % from each fill")
        trailing_stop_pct: float = Field(0.4, gt=0, lt=100, description="Scheduled-close trail, %")
        cadence_minutes: int = Field(10, ge=5, multiple_of=5, description="Trail update cadence")
        initialization: Literal["first_available_bar", "exact_seed_time"] = "first_available_bar"
        seed_date: date = Field(date(2021, 11, 8), description="Historical seed date (IST)")
        seed_time: time = Field(time(9, 20), description="Exact seed fill time (IST)")
        start_long: bool = Field(True, description="Initial direction is long; false starts short")
        round_to_tick: bool = Field(False, description="Round stop prices to the symbol tick")

    @classmethod
    def rules(cls, p: "NiftyTrailingReversal.Params") -> list[dict]:
        return [
            {"kind": "filter", "title": "When it runs", "conditions": [
                "Selected-timeframe NIFTY candles; one position at a time (5-minute preserves the Pine behavior)",
                ("The first loaded regular-session candle queues the initial order"
                 if p.initialization == "first_available_bar" else
                 f"The candle before {p.seed_time:%H:%M} IST on {p.seed_date:%-d %b %Y} queues the initial order"),
                "The position and stop carry overnight; there is no daily reset or square-off",
            ]},
            {"kind": "entry_long", "title": "Long / reverse to long", "conditions": [
                "The initial order fills at the candle open after it is queued",
                "While short, reaching the active stop closes the short and opens long",
            ]},
            {"kind": "entry_short", "title": "Reverse to short", "conditions": [
                "While long, reaching the active stop closes the long and opens short",
                "A gap beyond the stop fills at the adverse candle open in historical replay",
            ]},
            {"kind": "risk", "title": "Protective and trailing stop", "conditions": [
                f"After its fill candle, each position gets a stop {p.initial_stop_pct:g}% from the fill",
                f"Every {p.cadence_minutes} minutes from 09:15, plus 15:30 IST, ratchet to {p.trailing_stop_pct:g}% behind the completed close",
                "The stop only tightens while the direction is unchanged; there is no profit target",
            ]},
            {"kind": "exit", "title": "Close and reverse", "conditions": [
                "A stop hit reverses the full position; it never remains flat by design",
                "Historical replay permits at most one reversal in each 5-minute candle",
                "The tester closes the final open position at the last available candle close",
            ]},
        ]

    async def on_start(self) -> None:
        self.seeded = self.ctx.position != 0
        self.stop: float | None = None
        self._direction = 1 if self.ctx.position > 0 else -1 if self.ctx.position < 0 else 0
        self._seed_pending = False
        self._reversal_pending = False
        self._fill_bucket: int | None = None
        self._checkpoint_restored = self._restore_checkpoint()

    def _restore_checkpoint(self) -> bool:
        """Restore exact live decision state; incompatible checkpoints are ignored safely."""
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
        direction = state.get("direction")
        stop = state.get("stop")
        fill_bucket = state.get("fill_bucket")
        if direction not in (-1, 0, 1):
            return False
        if stop is not None and not isinstance(stop, (int, float)):
            return False
        if fill_bucket is not None and not isinstance(fill_bucket, int):
            return False
        self.seeded = bool(state.get("seeded"))
        self.stop = float(stop) if stop is not None else None
        self._direction = direction
        self._seed_pending = bool(state.get("seed_pending"))
        self._reversal_pending = bool(self.ctx.order_pending)
        self._fill_bucket = fill_bucket
        return True

    def _checkpoint(self) -> dict:
        return {
            "version": self.checkpoint_version,
            "strategy": self.name,
            "bar_seconds": self.bar_seconds,
            "params": self.params.model_dump(mode="json"),
            "seeded": self.seeded,
            "direction": self._direction,
            "stop": self.stop,
            "seed_pending": self._seed_pending,
            "fill_bucket": self._fill_bucket,
        }

    async def _save_checkpoint(self) -> None:
        save = getattr(self.ctx, "save_strategy_state", None)
        if save is not None:
            await save(self._checkpoint())

    @property
    def _initial_fraction(self) -> float:
        return self.params.initial_stop_pct / 100

    @property
    def _trail_fraction(self) -> float:
        return self.params.trailing_stop_pct / 100

    def _initial_stop(self, fill: float, long: bool) -> float:
        return self._price_level(fill * (1 - self._initial_fraction if long else 1 + self._initial_fraction))

    def _price_level(self, value: float) -> float:
        tick = self.ctx.tick_size if self.params.round_to_tick else None
        if not tick:
            return value
        steps = (Decimal(str(value)) / Decimal(str(tick))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return float(steps * Decimal(str(tick)))

    def _ratchet(self, close: float, long: bool) -> None:
        candidate = self._price_level(close * (1 - self._trail_fraction if long else 1 + self._trail_fraction))
        if self.stop is None:
            self.stop = candidate
        elif long:
            self.stop = max(self.stop, candidate)
        else:
            self.stop = min(self.stop, candidate)

    def _regular_bar(self, start: datetime) -> bool:
        local_start = start.astimezone(IST)
        local_end = local_start + timedelta(seconds=self.bar_seconds)
        session_start = datetime.combine(local_start.date(), time(9, 15), tzinfo=IST)
        session_end = datetime.combine(local_start.date(), time(15, 30), tzinfo=IST)
        return local_start < session_end and local_end > session_start

    def _scheduled_close(self, start: datetime) -> bool:
        if not self._regular_bar(start):
            return False
        local_start = start.astimezone(IST)
        local_end = local_start + timedelta(seconds=self.bar_seconds)
        session_start = datetime.combine(local_start.date(), time(9, 15), tzinfo=IST)
        session_end = datetime.combine(local_start.date(), time(15, 30), tzinfo=IST)
        due = session_start + timedelta(minutes=self.params.cadence_minutes)
        while due < session_end:
            if local_start < due <= local_end:
                return True
            due += timedelta(minutes=self.params.cadence_minutes)
        return local_start < session_end <= local_end

    def _queues_seed(self, start: datetime) -> bool:
        if not self._regular_bar(start):
            return False
        if self.params.initialization == "first_available_bar":
            return True
        seed_at = datetime.combine(self.params.seed_date, self.params.seed_time, tzinfo=IST)
        return start < seed_at <= start + timedelta(seconds=self.bar_seconds)

    async def _open_seed_at(self, price: float, at: datetime) -> None:
        reason = "initial LONG" if self.params.start_long else "initial SHORT"
        if self.params.start_long:
            await self.ctx.buy_at(price, at, reason)
        else:
            await self.ctx.sell_at(price, at, reason)

    async def on_backtest_candle(self, candle: Candle) -> None:
        """Apply Pine's next-open fills and one-confirmed-calculation-per-bar model."""
        self.ctx.mark(candle.open, candle.start)

        if not self.seeded:
            if self._seed_pending:
                await self._open_seed_at(candle.open, candle.start)
                self.seeded, self._seed_pending = True, False
                # Pine learns the fill and installs the first stop at this candle's close.
                self.stop = None
            else:
                self.ctx.mark(candle.close, candle.start + timedelta(seconds=self.bar_seconds))
                if self._queues_seed(candle.start):
                    self._seed_pending = True
                return

        if self.ctx.position > 0 and self.stop is not None and candle.low <= self.stop:
            fill = candle.open if candle.open <= self.stop else self.stop
            self.ctx.mark(fill, candle.start)
            await self.ctx.sell_at(
                fill, candle.start, f"long stop {self.stop:.2f}; reverse short",
                quantity=abs(self.ctx.position) + self.ctx.quantity,
            )
            self.stop = self._initial_stop(fill, long=False)
        elif self.ctx.position < 0 and self.stop is not None and candle.high >= self.stop:
            fill = candle.open if candle.open >= self.stop else self.stop
            self.ctx.mark(fill, candle.start)
            await self.ctx.buy_at(
                fill, candle.start, f"short stop {self.stop:.2f}; reverse long",
                quantity=abs(self.ctx.position) + self.ctx.quantity,
            )
            self.stop = self._initial_stop(fill, long=True)

        # No stop exists during the seed fill candle, and a reversal stop is not retested in
        # its fill candle. At the close Pine initializes it, then applies any due ratchet.
        close_at = candle.start + timedelta(seconds=self.bar_seconds)
        self.ctx.mark(candle.close, close_at)
        if self.ctx.position and self.stop is None:
            self.stop = self._initial_stop(candle.open, long=self.ctx.position > 0)
        if self.ctx.position and self._scheduled_close(candle.start):
            self._ratchet(candle.close, long=self.ctx.position > 0)

    async def on_tick(self, tick: Tick) -> None:
        """Live mode: bar-close ratchets, but protective stops remain active each tick."""
        if bar := self._bars.add(tick):
            await self.on_bar(bar)

        position = self.ctx.position
        direction = 1 if position > 0 else -1 if position < 0 else 0
        if direction and direction != self._direction:
            self._direction, self._reversal_pending = direction, False
            if self._seed_pending:
                self.seeded, self._seed_pending = True, False
            self.stop = self._initial_stop(tick.price, long=direction > 0)
            self._fill_bucket = int(tick.at.timestamp()) // self.bar_seconds
            await self._save_checkpoint()

        if not self.seeded and position == 0:
            return

        if position and self.stop is None:
            # A resumed position has no persisted strategy-local stop. Protect it immediately
            # from the first available price; do not leave it naked while reconstructing state.
            self.stop = self._initial_stop(tick.price, long=position > 0)
            await self._save_checkpoint()

        bucket = int(tick.at.timestamp()) // self.bar_seconds
        if not position or self.stop is None or self._reversal_pending or bucket == self._fill_bucket:
            return
        if position > 0 and tick.price <= self.stop:
            old_stop = self.stop
            old_fill_bucket = self._fill_bucket
            desired_direction = -1
            self._direction = desired_direction
            self.stop = self._initial_stop(tick.price, long=False)
            self._fill_bucket = bucket
            self._reversal_pending = True
            # Durable intent comes first. If the process dies after the broker accepts the
            # order, restart recovery must continue toward SHORT rather than restore LONG.
            await self._save_checkpoint()
            await self.ctx.sell(quantity=abs(position) + self.ctx.quantity,
                                reason=f"long stop {old_stop:.2f}; reverse short")
        elif position < 0 and tick.price >= self.stop:
            old_stop = self.stop
            old_fill_bucket = self._fill_bucket
            desired_direction = 1
            self._direction = desired_direction
            self.stop = self._initial_stop(tick.price, long=True)
            self._fill_bucket = bucket
            self._reversal_pending = True
            await self._save_checkpoint()
            await self.ctx.buy(quantity=abs(position) + self.ctx.quantity,
                               reason=f"short stop {old_stop:.2f}; reverse long")
        else:
            return

        new_direction = 1 if self.ctx.position > 0 else -1 if self.ctx.position < 0 else 0
        if new_direction == desired_direction:  # filled synchronously, or option target changed
            self._reversal_pending = self.ctx.order_pending
        elif self.ctx.order_pending:
            # Direct-instrument broker order accepted but has not filled yet.
            self._reversal_pending = True
        else:
            # Rejected synchronously: restore the still-live side and its stop so another tick
            # can retry. (Option legs expose their desired direction, so they use the first case.)
            self._direction, self.stop = direction, old_stop
            self._fill_bucket = old_fill_bucket
            self._reversal_pending = False
        await self._save_checkpoint()

    async def on_bar(self, bar: Bar) -> None:
        if not self.seeded and not self._seed_pending and self.ctx.position == 0 and self._queues_seed(bar.start):
            self._seed_pending = True
            if self.params.start_long:
                await self.ctx.buy(reason="initial LONG at next bar open")
            else:
                await self.ctx.sell(reason="initial SHORT at next bar open")
            if self.ctx.position == 0 and not self.ctx.order_pending:
                self._seed_pending = False  # rejected/cancelled synchronously: retry next bar
            await self._save_checkpoint()
            return
        if self.ctx.position and self._scheduled_close(bar.start):
            old_stop = self.stop
            self._ratchet(bar.close, long=self.ctx.position > 0)
            if self.stop != old_stop:
                await self._save_checkpoint()
