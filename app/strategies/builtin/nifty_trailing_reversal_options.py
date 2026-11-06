"""NIFTY percentage-reversal signals executed with long CE/PE options.

The NIFTY index supplies historical candles and live ticks.  Scheduled trailing updates use
broker-published completed one-minute OHLC aggregated to five minutes, while stop hits remain
tick-by-tick. A long signal holds an in-the-money call; a short signal holds an in-the-money
put. A reversal first sells the held option, then buys the opposite leg after that exit is
terminal. The runner therefore applies the same depth-priced limit orders and repricing policy
used by the intraday NIFTY options strategy.
"""

from datetime import datetime, timedelta

from pydantic import Field

from app.brokers import OrderError
from app.brokers.base import Candle
from app.strategies.base import Bar
from app.strategies.builtin.intraday_breakout_options import pick_contract
from app.strategies.builtin.nifty_trailing_reversal import IST, NiftyTrailingReversal


class _ReplayContext:
    """Minimal no-order context used to reconstruct the Pine state from historical candles."""

    def __init__(self, quantity: int, tick_size: float | None) -> None:
        self.position = 0
        self.quantity = quantity
        self.tick_size = tick_size

    def mark(self, price, at) -> None:
        pass

    async def buy_at(self, price, at, reason=None, quantity=None) -> None:
        self.position += quantity or self.quantity

    async def sell_at(self, price, at, reason=None, quantity=None) -> None:
        self.position -= quantity or self.quantity


def _complete_bars(candles: list[Candle], now: datetime, seconds: int) -> list[Candle]:
    """Aggregate chronological one-minute history to complete selected-timeframe bars."""
    grouped: dict[int, list[Candle]] = {}
    current_bucket = int(now.timestamp()) // seconds
    for candle in sorted(candles, key=lambda c: c.start):
        bucket = int(candle.start.timestamp()) // seconds
        if bucket < current_bucket:
            grouped.setdefault(bucket, []).append(candle)

    out: list[Candle] = []
    for bucket, rows in sorted(grouped.items()):
        rows.sort(key=lambda c: c.start)
        if len(rows) != seconds // 60:
            continue
        out.append(Candle(
            datetime.fromtimestamp(bucket * seconds, tz=rows[0].start.tzinfo),
            rows[0].open,
            max(c.high for c in rows),
            min(c.low for c in rows),
            rows[-1].close,
            sum(c.volume for c in rows),
        ))
    return out


class ReversalOptionLegs:
    """Expose the desired NIFTY direction while sequencing the physical option orders."""

    def __init__(self, ctx, itm_points: float) -> None:
        self._ctx = ctx
        self._itm = itm_points
        self.held = ctx.held_symbol
        self.held_expiry = None
        self.target = (-1 if self.held and self.held.endswith("PE") else
                       1 if self.held and self.held.endswith("CE") else 0)
        self.reason: str | None = None

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    @property
    def position(self) -> int:
        return self.target * self._ctx.quantity

    @property
    def last_fill_price(self) -> None:
        """Stops are in NIFTY points, never in the held option's premium."""
        return None

    async def buy(self, quantity=None, reason=None) -> None:
        self.target, self.reason = 1, reason
        await self.advance()

    async def sell(self, quantity=None, reason=None) -> None:
        self.target, self.reason = -1, reason
        await self.advance()

    async def advance(self) -> None:
        """Move one safe step toward the requested leg: exit first, then enter."""
        if self._ctx.order_pending:
            return

        actual = self._ctx.position
        today = datetime.now(IST).date()
        held_kind = ("PE" if self.held and self.held.endswith("PE") else
                     "CE" if self.held and self.held.endswith("CE") else None)
        wanted_kind = "CE" if self.target > 0 else "PE" if self.target < 0 else None
        if actual:
            if self.held_expiry is None and self.held:
                contracts = await self._ctx.option_contracts()
                held_contract = next((c for c in contracts if c.symbol == self.held), None)
                self.held_expiry = held_contract.expiry if held_contract else None
            expires_today = self.held_expiry is not None and self.held_expiry <= today
            if held_kind == wanted_kind and not expires_today:
                return
            if not self.held:
                self._ctx.log("cannot reverse option: held contract is unknown")
                return
            reason = (f"roll {held_kind} expiring {self.held_expiry:%d %b}"
                      if expires_today else self.reason or "reverse: exit current option")
            if expires_today:
                self.reason = f"maintain {'LONG' if self.target > 0 else 'SHORT'} signal after expiry roll"
            await self._ctx.sell(actual, reason, symbol=self.held)
            if self._ctx.position == 0 and not self._ctx.order_pending:
                self.held, self.held_expiry = None, None
            return

        if wanted_kind is None:
            return
        spot = self._ctx.last_price
        if not spot:
            return  # startup warm-up can finish before the first live index tick
        contracts = await self._ctx.option_contracts()
        # This is positional: unlike the intraday strategy, never open an option expiring today.
        contract = pick_contract(contracts, wanted_kind, spot, self._itm, today + timedelta(days=1))
        if contract is None:
            self._ctx.log(f"cannot enter: no {wanted_kind} contract found near {spot}")
            return
        quantity = self._ctx.quantity // contract.lot * contract.lot
        if quantity == 0:
            self._ctx.log(f"cannot enter: {self._ctx.quantity} units is less than one lot of {contract.lot}")
            return
        self.held = contract.symbol
        self.held_expiry = contract.expiry
        await self._ctx.buy(quantity, self.reason, symbol=contract.symbol)


class NiftyTrailingReversalOptions(NiftyTrailingReversal):
    name = "nifty_trailing_reversal_options"
    description = (
        "Persistent NIFTY percentage-reversal signals, executed by holding an ITM call while "
        "long and an ITM put while short."
    )
    tags = ("Positional", "NIFTY", "Options", "Long CE/PE", "Trailing stop")
    capital_sizing = False
    backtest_note = (
        "Live startup reconstructs the selected timeframe from recent one-minute NIFTY history. Backtests "
        "report NIFTY index points × quantity, not historical option-premium P&L. Live trailing "
        "updates use broker-published completed candles; stop hits still react to every tick."
    )

    class Params(NiftyTrailingReversal.Params):
        itm_points: float = Field(300, ge=0, description="Option distance in the money, index points")
        history_days: int = Field(7, ge=1, le=60, description="One-minute history used at live startup")

    @classmethod
    def rules(cls, p: "NiftyTrailingReversalOptions.Params") -> list[dict]:
        rules = super().rules(p)
        for rule in rules:
            if rule["kind"] == "entry_long":
                rule["title"] = "Buy an ITM call (CE)"
                rule["conditions"].append(f"Uses the next non-expiring CE about {p.itm_points:g} points ITM")
            elif rule["kind"] == "entry_short":
                rule["title"] = "Buy an ITM put (PE)"
                rule["conditions"].append(f"Uses the next non-expiring PE about {p.itm_points:g} points ITM")
            elif rule["kind"] == "exit":
                rule["conditions"].append("A reversal sells the held option before buying the opposite leg")
        return rules

    async def on_start(self) -> None:
        real_ctx = self.ctx
        if not hasattr(real_ctx, "option_contracts"):  # backtest: retain index-point simulation
            await super().on_start()
            return
        self._real_ctx = real_ctx
        self.ctx = ReversalOptionLegs(real_ctx, self.params.itm_points)
        await super().on_start()
        self._warmup_retry_at: datetime | None = None
        if self._checkpoint_restored:
            self._history_warmed = True
            self.ctx.target = self._direction
            self.ctx.reason = "resume durable NIFTY reversal state"
            self._real_ctx.log(
                f"restored {'LONG' if self._direction > 0 else 'SHORT'} signal; "
                f"stop {self.stop:.2f}" if self.stop is not None else
                "restored NIFTY reversal state without an armed stop"
            )
            await self.ctx.advance()
            return
        self._history_warmed = False
        await self._warm_from_history(datetime.now(IST))

    async def _warm_from_history(self, now: datetime) -> bool:
        """Reconstruct signal state; retry later when history is temporarily unavailable."""
        if self._history_warmed:
            return True
        if self._warmup_retry_at and now < self._warmup_retry_at:
            return False
        try:
            history = await self._real_ctx.history("1", days=self.params.history_days)
        except OrderError as exc:
            self._warmup_retry_at = now + timedelta(seconds=30)
            self._real_ctx.log(f"could not load reversal warm-up history ({exc}); retrying")
            return False

        bars = _complete_bars(history, now, self.bar_seconds)
        if not bars:
            self._warmup_retry_at = now + timedelta(seconds=30)
            self._real_ctx.log("no complete NIFTY history bars for reversal warm-up; retrying")
            return False

        replay_ctx = _ReplayContext(self._real_ctx.quantity, self._real_ctx.tick_size)
        replay = NiftyTrailingReversal(self.params, replay_ctx, self.bar_seconds)
        await replay.on_start()
        for candle in bars:
            await replay.on_backtest_candle(candle)
        if not replay.seeded or not replay_ctx.position:
            self._warmup_retry_at = now + timedelta(seconds=30)
            self._real_ctx.log("history does not contain the configured reversal seed; retrying")
            return False

        direction = 1 if replay_ctx.position > 0 else -1
        self.seeded = True
        self.stop = replay.stop
        self._direction = direction
        self.ctx.target = direction
        self.ctx.reason = f"align with {len(bars)} historical NIFTY candles"
        self._history_warmed = True
        self._warmup_retry_at = None
        self._real_ctx.log(
            f"reconstructed {'LONG' if direction > 0 else 'SHORT'} signal from "
            f"{len(bars)} candles; stop {self.stop:.2f}")
        await self._save_checkpoint()
        await self.ctx.advance()
        return True

    async def on_tick(self, tick) -> None:
        if isinstance(self.ctx, ReversalOptionLegs):
            if not await self._warm_from_history(tick.at.astimezone(IST)):
                return
            await self.ctx.advance()
        await super().on_tick(tick)
        if isinstance(self.ctx, ReversalOptionLegs):
            await self.ctx.advance()

    async def on_bar(self, bar: Bar) -> None:
        """Ratchet only from the broker's completed OHLC, matching commit 3f9c332.

        The streaming bar is used only as the clock telling us a five-minute bucket ended.
        Missing or not-yet-published minute candles skip that update rather than substituting
        an incomplete tick-built close.
        """
        if not self.ctx.position or not self._scheduled_close(bar.start):
            return
        closed_at = (bar.start + timedelta(seconds=bar.seconds)).astimezone(IST)
        completed = await self._completed_history_bar(closed_at)
        if completed is not None:
            old_stop = self.stop
            self._ratchet(completed.close, long=self.ctx.position > 0)
            if self.stop != old_stop:
                await self._save_checkpoint()

