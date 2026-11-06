"""Persistent NIFTY percentage-stop reversal strategy.

The historical replay follows the supplied reference rules exactly: seed one long at the
09:20 candle open on 8 November 2021, reverse at a 0.8% stop (gap-aware), and ratchet the stop
to 0.4% behind scheduled 5-minute closes.  There is no daily reset or square-off.

Live execution keeps the new stop actionable on every tick.  This is deliberately safer
than the historical convention, which tests at most one reversal in a candle, and consequently
will not reproduce the quoted historical result in live trading.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.brokers.base import Candle
from app.strategies.base import Bar, Strategy, Tick

IST = ZoneInfo("Asia/Kolkata")


class NiftyTrailingReversal(Strategy):
    name = "nifty_trailing_reversal"
    description = (
        "Always-in NIFTY long/short reversal: a 0.8% protective stop ratcheted to 0.4% "
        "behind scheduled closes, with positions carried overnight."
    )
    tags = ("Positional", "NIFTY", "Long & short", "Trailing stop")
    timeframes = ("5m",)
    default_timeframe = "5m"
    capital_sizing = False
    backtest_note = (
        "Historical reproduction requires data beginning before the fixed seed at 09:20 IST "
        "on 8 Nov 2021. The tester's shorter date ranges cannot reconstruct an already-open "
        "position. Live execution keeps the reversed side protected within the same candle, "
        "so it intentionally differs from the historical one-reversal-per-candle approximation."
    )

    class Params(BaseModel):
        initial_stop_pct: float = Field(0.8, gt=0, lt=100, description="Initial stop, % from each fill")
        trailing_stop_pct: float = Field(0.4, gt=0, lt=100, description="Scheduled-close trail, %")
        seed_date: date = Field(date(2021, 11, 8), description="Historical seed date (IST)")
        seed_time: time = Field(time(9, 20), description="Historical/live seed time (IST)")

    @classmethod
    def rules(cls, p: "NiftyTrailingReversal.Params") -> list[dict]:
        return [
            {"kind": "filter", "title": "When it runs", "conditions": [
                "5-minute NIFTY candles; one position at a time",
                f"Historical replay seeds LONG at {p.seed_time:%H:%M} IST on {p.seed_date:%-d %b %Y}",
                "The position and stop carry overnight; there is no daily reset or square-off",
            ]},
            {"kind": "entry_long", "title": "Long / reverse to long", "conditions": [
                "The fixed historical seed opens long at the seed candle's open",
                "While short, reaching the active stop closes the short and opens long",
            ]},
            {"kind": "entry_short", "title": "Reverse to short", "conditions": [
                "While long, reaching the active stop closes the long and opens short",
                "A gap beyond the stop fills at the adverse candle open in historical replay",
            ]},
            {"kind": "risk", "title": "Protective and trailing stop", "conditions": [
                f"Each fill resets the stop {p.initial_stop_pct:g}% away from the fill",
                f"At 09:25, 09:35, ..., 15:25 and 15:30 IST, ratchet to {p.trailing_stop_pct:g}% behind the completed close",
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

    @property
    def _initial_fraction(self) -> float:
        return self.params.initial_stop_pct / 100

    @property
    def _trail_fraction(self) -> float:
        return self.params.trailing_stop_pct / 100

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

    @staticmethod
    def _scheduled_close(at: datetime) -> bool:
        t = at.astimezone(IST).time().replace(tzinfo=None)
        if t == time(15, 30):
            return True
        return time(9, 25) <= t <= time(15, 25) and t.minute % 10 == 5

    def _is_seed_candle(self, start: datetime) -> bool:
        local = start.astimezone(IST)
        return local.date() == self.params.seed_date and local.time().replace(tzinfo=None) == self.params.seed_time

    async def on_backtest_candle(self, candle: Candle) -> None:
        """Apply the reference OHLC convention without inventing an intrabar path."""
        self.ctx.mark(candle.open, candle.start)

        if not self.seeded:
            if not self._is_seed_candle(candle.start):
                self.ctx.mark(candle.close, candle.start + timedelta(minutes=5))
                return
            await self.ctx.buy_at(candle.open, candle.start, "fixed historical long seed")
            self.seeded = True
            self.stop = self._initial_stop(candle.open, long=True)

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

        # The new stop is intentionally not tested again after a reversal in this candle.
        close_at = candle.start + timedelta(seconds=self.bar_seconds)
        self.ctx.mark(candle.close, close_at)
        if self.ctx.position and self._scheduled_close(close_at):
            self._ratchet(candle.close, long=self.ctx.position > 0)

    async def on_tick(self, tick: Tick) -> None:
        """Live mode: bar-close ratchets, but protective stops remain active each tick."""
        if bar := self._bars.add(tick):
            await self.on_bar(bar)

        now = tick.at.astimezone(IST)
        position = self.ctx.position
        direction = 1 if position > 0 else -1 if position < 0 else 0
        if direction and direction != self._direction:
            self._direction, self._reversal_pending = direction, False
            if self._seed_pending:
                self.seeded, self._seed_pending = True, False
            self.stop = self._initial_stop(tick.price, long=direction > 0)

        if not self.seeded and position == 0:
            # A newly deployed live instance seeds only in its 09:20 candle. This is a
            # practical start rule; the reference explicitly says a new live start is unvalidated.
            if not self._seed_pending and now.date() >= self.params.seed_date \
                    and now.time().replace(tzinfo=None).hour == self.params.seed_time.hour \
                    and now.time().replace(tzinfo=None).minute == self.params.seed_time.minute:
                self._seed_pending = True
                await self.ctx.buy(reason="live long seed at the 09:20 candle")
                self.seeded = self.ctx.position != 0
                if self.seeded:
                    self._seed_pending = False
                    self._direction = 1
                    self.stop = self._initial_stop(tick.price, long=True)
            return

        if position and self.stop is None:
            # A resumed position has no persisted strategy-local stop. Protect it immediately
            # from the first available price; do not leave it naked while reconstructing state.
            self.stop = self._initial_stop(tick.price, long=position > 0)

        if not position or self.stop is None or self._reversal_pending:
            return
        if position > 0 and tick.price <= self.stop:
            old_stop = self.stop
            await self.ctx.sell(quantity=abs(position) + self.ctx.quantity,
                                reason=f"long stop {old_stop:.2f}; reverse short")
        elif position < 0 and tick.price >= self.stop:
            old_stop = self.stop
            await self.ctx.buy(quantity=abs(position) + self.ctx.quantity,
                               reason=f"short stop {old_stop:.2f}; reverse long")
        else:
            return

        self._reversal_pending = True
        new_direction = 1 if self.ctx.position > 0 else -1 if self.ctx.position < 0 else 0
        if new_direction and new_direction != direction:  # test contexts may fill synchronously
            self._direction, self._reversal_pending = new_direction, False
            self.stop = self._initial_stop(tick.price, long=new_direction > 0)

    async def on_bar(self, bar: Bar) -> None:
        closed_at = bar.start + timedelta(seconds=bar.seconds)
        if self.ctx.position and self._scheduled_close(closed_at):
            self._ratchet(bar.close, long=self.ctx.position > 0)
