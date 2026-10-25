"""Intraday 1-minute breakout, ported from the TradingView script "NIFTY 1-minute breakout".

From 10:30, at each 1-minute close it arms a buy level at the high and a sell level at the low
of the last 5 one-minute candles (today's only). Whichever breaks first during the next candle
is traded and the other is dropped; levels live for one candle. The stop is wide and trails
behind the best price; there is no target. After a stop-out the next breakout is taken again,
up to the daily cap. Everything is squared off at 15:15.

All distances are fractions of the average daily range (high - low) of the previous 14 days,
so they widen and narrow with volatility.

Differences from the Pine version (which simulates resting stop orders on bar data):
  - Entries and exits are market orders sent when a tick reaches the level, so live fills
    include slippage; the stop is measured from the price of the triggering tick.
  - The stop and trail are checked on every tick instead of once per bar.
  - Commission is not modelled (the broker charges the real one).
  - The daily cap counts entries requested, including ones a risk check blocks.
"""

from collections import deque
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from app.brokers import OrderError
from app.strategies.base import Bar, Strategy, Tick

IST = ZoneInfo("Asia/Kolkata")
HISTORY_RETRY = timedelta(seconds=60)  # wait before asking the broker for daily candles again
EXIT_RETRY = timedelta(seconds=5)  # don't repeat an exit request while it's being filled


class IntradayBreakout(Strategy):
    name = "intraday_breakout"
    description = (
        "Breakout of the last N candles' high/low (built for 1m), wide trailing stop scaled to "
        "the average daily range, intraday only (square-off at 15:15)."
    )
    timeframes = ("1m", "3m", "5m", "10m", "15m")  # intraday; longer bars leave too few per day

    class Params(BaseModel):
        lookback: int = Field(5, ge=2, description="Breakout lookback (1-minute candles)")
        max_width: float = Field(
            0.4, ge=0, description="Skip if the lookback range is wider than this x avg daily range (0 = off)")
        avg_days: int = Field(14, ge=2, description="Days in the average daily range")
        stop_k: float = Field(0.25, gt=0, description="Initial stop, x avg daily range")
        trail_k: float = Field(0.40, ge=0, description="Trailing stop, x avg daily range (0 = off)")
        max_trades_per_day: int = Field(8, ge=0, description="Max entries per day (0 = no limit)")
        entries_from: time = Field(time(10, 30), description="First entry (IST)")
        entries_until: time = Field(time(14, 45), description="Last entry (IST)")
        square_off: time = Field(time(15, 15), description="Close everything at (IST)")

        @model_validator(mode="after")
        def _times_in_order(self):
            if not (self.entries_from <= self.entries_until < self.square_off):
                raise ValueError("need entries_from <= entries_until < square_off")
            return self

    async def on_start(self) -> None:
        self.day: date | None = None
        self.day_range: float | None = None
        self._range_retry_at: datetime | None = None
        self._new_day(None)
        self._clear_trade()

    def _new_day(self, day: date | None) -> None:
        self.day = day
        self.bars_today: deque[Bar] = deque(maxlen=self.params.lookback)
        self.trades_today = 0
        self.levels: tuple[float, float, float] | None = None  # (buy at, sell at, day range)
        self.day_range = None
        self._range_retry_at = None

    def _clear_trade(self) -> None:
        self.stop: float | None = None
        self.best: float | None = None
        self.trade_range: float | None = None
        self._exit_sent_at: datetime | None = None

    # ---------- events ----------

    async def on_tick(self, tick: Tick) -> None:
        now = tick.at.astimezone(IST)
        if now.date() != self.day:
            self._new_day(now.date())
        if bar := self._bars.add(tick):
            await self.on_bar(bar)
        await self._on_price(tick.price, now)

    async def on_bar(self, bar: Bar) -> None:
        """A 1-minute candle closed: arm or clear the breakout levels for the next one."""
        closed_at = (bar.start + timedelta(seconds=bar.seconds)).astimezone(IST)
        if bar.start.astimezone(IST).date() != self.day:
            return  # yesterday's last candle; the lookback only uses today's
        self.bars_today.append(bar)
        await self._load_day_range(closed_at)

        p = self.params
        self.levels = None
        if self.ctx.position != 0 or self.day_range is None:
            return
        if len(self.bars_today) < p.lookback:
            return
        t = closed_at.time()
        if not (p.entries_from <= t <= p.entries_until and t < p.square_off):
            return
        if p.max_trades_per_day and self.trades_today >= p.max_trades_per_day:
            return
        high = max(b.high for b in self.bars_today)
        low = min(b.low for b in self.bars_today)
        if p.max_width and high - low > p.max_width * self.day_range:
            return
        self.levels = (high, low, self.day_range)

    # ---------- trading ----------

    async def _on_price(self, price: float, now: datetime) -> None:
        if self.ctx.position == 0:
            self._exit_sent_at = None
            if self.stop is not None:
                self._clear_trade()
            if now.time() >= self.params.square_off:
                self.levels = None
            elif self.levels is not None:
                await self._maybe_enter(price)
            return
        await self._manage(price, now)

    async def _maybe_enter(self, price: float) -> None:
        buy_at, sell_at, rng = self.levels
        if sell_at < price < buy_at:
            return
        self.levels = None  # one breakout per candle; the other side is cancelled
        self.trades_today += 1
        self.trade_range, self.best = rng, price
        if price >= buy_at:
            self.stop = price - self.params.stop_k * rng
            await self.ctx.buy(reason=f"broke above {buy_at:.2f}; stop {self.stop:.2f}")
        else:
            self.stop = price + self.params.stop_k * rng
            await self.ctx.sell(reason=f"broke below {sell_at:.2f}; stop {self.stop:.2f}")

    async def _manage(self, price: float, now: datetime) -> None:
        p = self.params
        long = self.ctx.position > 0
        if now.time() >= p.square_off:
            await self._exit(now, f"square-off at {p.square_off:%H:%M}")
            return

        if self.stop is None:
            # Position we didn't open in this run (e.g. after a restart): protect it from here.
            rng = self.day_range
            if rng is None:
                return
            self.trade_range, self.best = rng, price
            self.stop = price - p.stop_k * rng if long else price + p.stop_k * rng

        self.best = max(self.best, price) if long else min(self.best, price)
        if p.trail_k > 0:
            trail = self.best - p.trail_k * self.trade_range if long else self.best + p.trail_k * self.trade_range
            self.stop = max(self.stop, trail) if long else min(self.stop, trail)

        if (long and price <= self.stop) or (not long and price >= self.stop):
            await self._exit(now, f"stop {self.stop:.2f} hit at {price:.2f}")

    async def _exit(self, now: datetime, reason: str) -> None:
        if self._exit_sent_at and now - self._exit_sent_at < EXIT_RETRY:
            return  # already asked; waiting for the fill
        self._exit_sent_at = now
        self.levels = None
        await self.ctx.exit(reason=reason)

    # ---------- volatility ----------

    async def _load_day_range(self, now: datetime) -> None:
        """Average (high - low) of the previous `avg_days` completed daily candles."""
        if self.day_range is not None or (self._range_retry_at and now < self._range_retry_at):
            return
        try:
            candles = await self.ctx.history("D", days=self.params.avg_days * 2 + 10)
        except OrderError as e:
            self._range_retry_at = now + HISTORY_RETRY
            self.ctx.log(f"no daily history yet ({e}); retrying")
            return
        previous = [c for c in candles if c.start.astimezone(IST).date() < now.date()]
        if len(previous) < self.params.avg_days:
            self._range_retry_at = now + HISTORY_RETRY
            self.ctx.log(f"only {len(previous)} daily candles; need {self.params.avg_days}")
            return
        last = previous[-self.params.avg_days:]
        self.day_range = sum(c.high - c.low for c in last) / len(last)
        self.ctx.log(f"average daily range over {len(last)} days: {self.day_range:.2f}")
