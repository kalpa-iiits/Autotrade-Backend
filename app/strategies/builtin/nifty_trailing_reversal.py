"""Persistent NIFTY percentage-stop reversal strategy.

This ports the supplied TradingView "Explicit orders v3" script. The first loaded
regular-session candle queues the initial long order, which fills at the next candle open.
Stops are armed from the fill, ratchet from scheduled closes, and carry overnight.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from app.brokers import OrderError
from app.brokers.base import Candle
from app.strategies.base import Bar, Strategy, Tick

IST = ZoneInfo("Asia/Kolkata")


class NiftyTrailingReversal(Strategy):
    checkpoint_version = 1
    name = "nifty_trailing_reversal"
    description = (
        "Always-in NIFTY long/short reversal: a 0.8% protective stop ratcheted to 0.4% "
        "behind scheduled closes, with positions carried overnight."
    )
    tags = ("Positional", "NIFTY", "Long & short", "Trailing stop")
    timeframes = ("5m",)
    default_timeframe = "5m"
    capital_sizing = True
    backtest_note = (
        "The result follows the Pine script's completed-candle approximation. At most one "
        "reversal is processed per 5-minute candle."
    )
    initial_stop_pct = 0.8
    trailing_stop_pct = 0.4
    cadence_minutes = 10

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

    @classmethod
    def rules(cls, p: "NiftyTrailingReversal.Params") -> list[dict]:
        return [
            {"kind": "filter", "title": "When it runs", "conditions": [
                "5-minute NIFTY candles; one position at a time",
                "The first loaded regular-session candle queues the initial long order",
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
                f"From its fill, each position gets a stop {cls.initial_stop_pct:g}% from the fill",
                f"Every {cls.cadence_minutes} minutes from 09:15 IST, ratchet to {cls.trailing_stop_pct:g}% behind the completed close",
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
        self._pre_reversal_stop: float | None = None
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
        if any(state.get(key) != value for key, value in expected.items() if key != "params"):
            return False
        if not self._checkpoint_params_match(state.get("params")):
            return False
        direction = state.get("direction")
        stop = state.get("stop")
        fill_bucket = state.get("fill_bucket")
        pre_reversal_stop = state.get("pre_reversal_stop")
        if direction not in (-1, 0, 1):
            return False
        if stop is not None and not isinstance(stop, (int, float)):
            return False
        if fill_bucket is not None and not isinstance(fill_bucket, int):
            return False
        if pre_reversal_stop is not None and not isinstance(pre_reversal_stop, (int, float)):
            return False
        self.seeded = bool(state.get("seeded"))
        self.stop = float(stop) if stop is not None else None
        self._direction = direction
        self._seed_pending = bool(state.get("seed_pending"))
        # Persist the intent as well as consulting the broker. An accepted reversal can finish
        # while the process is down; in that case there is no longer an open broker order, but
        # the first tick after restart must still install the stop from its recorded fill.
        self._reversal_pending = bool(state.get("reversal_pending")) or bool(self.ctx.order_pending)
        self._pre_reversal_stop = (float(pre_reversal_stop)
                                   if pre_reversal_stop is not None else None)
        self._fill_bucket = fill_bucket
        return True

    def _checkpoint_params_match(self, saved) -> bool:
        """Accept checkpoints from the former configurable form only when it used these rules."""
        if not isinstance(saved, dict):
            return False
        legacy_fixed = {
            "initial_stop_pct": 0.8,
            "trailing_stop_pct": 0.4,
            "cadence_minutes": 10,
            "initialization": "first_available_bar",
            "seed_date": "2021-11-08",
            "seed_time": "09:20:00",
            "start_long": True,
            "round_to_tick": False,
        }
        remaining = dict(saved)
        for key, expected in legacy_fixed.items():
            if key in remaining and remaining.pop(key) != expected:
                return False
        return remaining == self.params.model_dump(mode="json")

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
            "reversal_pending": self._reversal_pending,
            "pre_reversal_stop": self._pre_reversal_stop,
            "fill_bucket": self._fill_bucket,
        }

    async def _save_checkpoint(self) -> None:
        save = getattr(self.ctx, "save_strategy_state", None)
        if save is not None:
            await save(self._checkpoint())

    @property
    def _initial_fraction(self) -> float:
        return self.initial_stop_pct / 100

    @property
    def _trail_fraction(self) -> float:
        return self.trailing_stop_pct / 100

    def _initial_stop(self, fill: float, long: bool) -> float:
        return fill * (1 - self._initial_fraction if long else 1 + self._initial_fraction)

    def _ratchet(self, close: float, long: bool) -> None:
        candidate = close * (1 - self._trail_fraction if long else 1 + self._trail_fraction)
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
        due = session_start + timedelta(minutes=self.cadence_minutes)
        while due < session_end:
            if local_start < due <= local_end:
                return True
            due += timedelta(minutes=self.cadence_minutes)
        return False

    def _queues_seed(self, start: datetime) -> bool:
        return self._regular_bar(start)

    async def _open_seed_at(self, price: float, at: datetime) -> None:
        await self.ctx.buy_at(price, at, "initial LONG")

    async def on_backtest_candle(self, candle: Candle) -> None:
        """Apply Pine's next-open fills and one-confirmed-calculation-per-bar model."""
        self.ctx.mark(candle.open, candle.start)

        if not self.seeded:
            if self._seed_pending:
                await self._open_seed_at(candle.open, candle.start)
                self.seeded, self._seed_pending = True, False
                # The entry is filled at this candle's open, so its protective stop is active
                # for the rest of this candle. The single if/elif below still permits at most
                # one reversal in the candle.
                self.stop = self._initial_stop(candle.open, long=self.ctx.position > 0)
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

        # A reversal stop is not retested in its fill candle. At the close, apply any due
        # completed-candle ratchet.
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
        bucket = int(tick.at.timestamp()) // self.bar_seconds
        if direction and direction != self._direction and not self._reversal_pending:
            seed_fill = self._seed_pending
            self._direction, self._reversal_pending = direction, False
            if self._seed_pending:
                self.seeded, self._seed_pending = True, False
            # Direct-instrument live orders use the broker's reported fill. Option legs expose
            # no underlying fill price, so their NIFTY signal correctly falls back to this tick.
            fill = getattr(self.ctx, "last_fill_price", None) or tick.price
            self.stop = self._initial_stop(fill, long=direction > 0)
            # The initial entry's stop is active immediately. A reversal fill, however, blocks
            # another reversal until the next candle, preserving the one-reversal rule.
            self._fill_bucket = None if seed_fill else bucket
            await self._save_checkpoint()
        elif direction and self._reversal_pending and not self.ctx.order_pending:
            # The intended reversal was recorded before an asynchronous broker fill. Now that
            # the order is terminal, either install its actual-fill stop or restore the old side
            # after a rejection/cancellation.
            if direction == self._direction:
                fill = getattr(self.ctx, "last_fill_price", None) or tick.price
                self.stop = self._initial_stop(fill, long=direction > 0)
                self._fill_bucket = bucket
            else:
                self._direction = direction
                if self._pre_reversal_stop is not None:
                    self.stop = self._pre_reversal_stop
                self._fill_bucket = None
            self._reversal_pending = False
            self._pre_reversal_stop = None
            await self._save_checkpoint()

        if not self.seeded and position == 0:
            return

        if position and self.stop is None:
            # A resumed position has no persisted strategy-local stop. Protect it immediately
            # from the first available price; do not leave it naked while reconstructing state.
            self.stop = self._initial_stop(tick.price, long=position > 0)
            await self._save_checkpoint()

        if not position or self.stop is None or self._reversal_pending or bucket == self._fill_bucket:
            return
        if position > 0 and tick.price <= self.stop:
            old_stop = self.stop
            old_fill_bucket = self._fill_bucket
            desired_direction = -1
            self._pre_reversal_stop = old_stop
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
            self._pre_reversal_stop = old_stop
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
            if not self._reversal_pending:
                fill = getattr(self.ctx, "last_fill_price", None) or tick.price
                self.stop = self._initial_stop(fill, long=desired_direction > 0)
                self._fill_bucket = bucket
                self._pre_reversal_stop = None
        elif self.ctx.order_pending:
            # Direct-instrument broker order accepted but has not filled yet.
            self._reversal_pending = True
        else:
            # Rejected synchronously: restore the still-live side and its stop so another tick
            # can retry. (Option legs expose their desired direction, so they use the first case.)
            self._direction, self.stop = direction, old_stop
            self._fill_bucket = old_fill_bucket
            self._reversal_pending = False
            self._pre_reversal_stop = None
        await self._save_checkpoint()

    async def on_bar(self, bar: Bar) -> None:
        if not self.seeded and not self._seed_pending and self.ctx.position == 0 and self._queues_seed(bar.start):
            self._seed_pending = True
            await self.ctx.buy(reason="initial LONG at next bar open")
            if self.ctx.position == 0 and not self.ctx.order_pending:
                self._seed_pending = False  # rejected/cancelled synchronously: retry next bar
            await self._save_checkpoint()
            return
        if self.ctx.position and self._scheduled_close(bar.start):
            # Match the backtest input: use the broker's completed one-minute OHLC aggregated
            # to the strategy's five-minute candle. The streamed bar is only the clock that
            # tells us the candle has ended; its last received tick can differ from history.
            completed = await self._completed_history_bar(
                (bar.start + timedelta(seconds=bar.seconds)).astimezone(IST), bar,
            )
            if completed is None:
                return
            old_stop = self.stop
            self._ratchet(completed.close, long=self.ctx.position > 0)
            if self.stop != old_stop:
                await self._save_checkpoint()

    async def _completed_history_bar(
        self, closed_at: datetime, fallback: Bar | None = None,
    ) -> Candle | Bar | None:
        """Return the exact completed candle used by replay, or skip an incomplete publication.

        Small unit-test contexts do not expose history; their supplied completed Bar remains a
        faithful fallback. Real StrategyContext objects always expose broker history.
        """
        history = getattr(self.ctx, "history", None)
        if history is None:
            return fallback
        try:
            candles = await history("1", days=1)
        except OrderError as exc:
            self.ctx.log(f"no intraday candle history yet ({exc}); skipping trail update")
            return None

        bucket = int(closed_at.timestamp()) // self.bar_seconds - 1
        rows = sorted(
            (c for c in candles if int(c.start.timestamp()) // self.bar_seconds == bucket
             and c.start.astimezone(IST) < closed_at),
            key=lambda c: c.start,
        )
        expected = self.bar_seconds // 60
        if len(rows) != expected:
            self.ctx.log(
                f"only {len(rows)}/{expected} minute candles for the bar ending "
                f"{closed_at:%H:%M}; skipping trail update")
            return None
        return Candle(
            datetime.fromtimestamp(bucket * self.bar_seconds, tz=rows[0].start.tzinfo),
            rows[0].open,
            max(c.high for c in rows),
            min(c.low for c in rows),
            rows[-1].close,
            sum(c.volume for c in rows),
        )
