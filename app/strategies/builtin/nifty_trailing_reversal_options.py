"""NIFTY percentage-reversal signals executed with long CE/PE options.

The NIFTY index supplies historical candles and live ticks.  Scheduled trailing updates use
broker-published completed one-minute OHLC aggregated to five minutes, while stop hits remain
tick-by-tick. A long signal holds an in-the-money call; a short signal holds an in-the-money
put. Entries use the current weekly expiry. A reversal first sells the held option, then buys
the opposite leg after that exit is terminal; at 15:00 IST on expiry day, the same signal is
rolled to the following weekly contract. Every physical option order explicitly uses the broker's
overnight carry-forward product. The runner applies the same depth-priced limit orders and
repricing policy used by the intraday NIFTY options strategy.
"""

from datetime import datetime, time, timedelta

from pydantic import Field

from app.brokers import OrderError
from app.brokers.base import Candle
from app.orders.schemas import Product
from app.strategies.builtin.intraday_breakout_options import pick_contract
from app.strategies.builtin.nifty_trailing_reversal import IST, NiftyTrailingReversal
from app.strategies.base import before_market_open

EXPIRY_ROLLOVER_TIME = time(15, 0)
OPTION_CHAIN_RETRY = timedelta(seconds=5)


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
        self._market_now: datetime | None = None
        self._contracts_retry_at: datetime | None = None

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

    def _defer_contracts(self, local_now: datetime, message: str) -> None:
        self._contracts_retry_at = local_now + OPTION_CHAIN_RETRY
        self._ctx.log(f"{message}; retrying option chain in {OPTION_CHAIN_RETRY.seconds} seconds")

    async def _option_contracts(self, local_now: datetime):
        """Fetch the chain without allowing a temporary broker/master failure to stop the strategy."""
        if self._contracts_retry_at is not None and local_now < self._contracts_retry_at:
            return None
        try:
            contracts = await self._ctx.option_contracts()
        except Exception as exc:
            self._defer_contracts(local_now, f"option chain unavailable ({exc})")
            return None
        if not contracts:
            self._defer_contracts(local_now, "option chain is empty")
            return None
        self._contracts_retry_at = None
        return contracts

    async def advance(self, now: datetime | None = None) -> None:
        """Move one safe step toward the requested leg: exit first, then enter."""
        if now is not None:
            self._market_now = now.astimezone(IST)
        local_now = self._market_now or datetime.now(IST)
        if self._ctx.order_pending:
            return

        actual = self._ctx.position
        today = local_now.date()
        held_kind = ("PE" if self.held and self.held.endswith("PE") else
                     "CE" if self.held and self.held.endswith("CE") else None)
        wanted_kind = "CE" if self.target > 0 else "PE" if self.target < 0 else None
        if actual:
            roll_due = False
            if held_kind != wanted_kind:
                # A fresh strategy reversal gets one immediate chain attempt for its opposite
                # leg even if an earlier same-direction expiry lookup was deferred.
                self._contracts_retry_at = None
            if held_kind == wanted_kind:
                # The chain is only needed to decide a same-direction expiry roll. A genuine
                # signal reversal must still exit immediately if chain retrieval is down.
                if self.held_expiry is None and self.held:
                    contracts = await self._option_contracts(local_now)
                    if contracts is None:
                        return
                    held_contract = next((c for c in contracts if c.symbol == self.held), None)
                    if held_contract is None:
                        self._defer_contracts(
                            local_now, f"held contract {self.held} is absent from the option chain",
                        )
                        return
                    self.held_expiry = held_contract.expiry
                roll_due = self.held_expiry is not None and (
                    self.held_expiry < today
                    or self.held_expiry == today and local_now.time() >= EXPIRY_ROLLOVER_TIME
                )
            if held_kind == wanted_kind and not roll_due:
                return
            if not self.held:
                self._ctx.log("cannot reverse option: held contract is unknown")
                return
            reason = (f"roll {held_kind} expiring {self.held_expiry:%d %b} at 15:00 IST"
                      if roll_due else self.reason or "reverse: exit current option")
            if roll_due:
                self.reason = f"maintain {'LONG' if self.target > 0 else 'SHORT'} signal after expiry roll"
            await self._ctx.sell(
                actual, reason, symbol=self.held, product=Product.DELIVERY,
            )
            if self._ctx.position == 0 and not self._ctx.order_pending:
                self.held, self.held_expiry = None, None
            return

        if wanted_kind is None:
            return
        spot = self._ctx.last_price
        if not spot:
            return  # startup warm-up can finish before the first live index tick
        contracts = await self._option_contracts(local_now)
        if contracts is None:
            return
        # Keep using the expiring weekly contract until 15:00 IST. From the rollover time
        # onward, skip today's expiry and select the following weekly contract.
        first_expiry = (today + timedelta(days=1)
                        if local_now.time() >= EXPIRY_ROLLOVER_TIME else today)
        contract = pick_contract(contracts, wanted_kind, spot, self._itm, first_expiry)
        if contract is None:
            self._defer_contracts(
                local_now, f"no eligible {wanted_kind} contract found near {spot}",
            )
            return
        quantity = self._ctx.quantity // contract.lot * contract.lot
        if quantity == 0:
            self._ctx.log(f"cannot enter: {self._ctx.quantity} units is less than one lot of {contract.lot}")
            return
        self.held = contract.symbol
        self.held_expiry = contract.expiry
        await self._ctx.buy(
            quantity, self.reason, symbol=contract.symbol, product=Product.DELIVERY,
        )


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
        itm_points: float = Field(1000, ge=0, description="Option distance in the money, index points")
        history_days: int = Field(7, ge=1, le=60, description="One-minute history used at live startup")

    @classmethod
    def rules(cls, p: "NiftyTrailingReversalOptions.Params") -> list[dict]:
        rules = super().rules(p)
        for rule in rules:
            if rule["kind"] == "filter":
                rule["conditions"].append(
                    "Every option entry, exit, reversal and roll uses the overnight carry-forward product"
                )
            elif rule["kind"] == "entry_long":
                rule["title"] = "Buy an ITM call (CE)"
                rule["conditions"].append(
                    f"Uses the current-week CE about {p.itm_points:g} points ITM"
                )
            elif rule["kind"] == "entry_short":
                rule["title"] = "Buy an ITM put (PE)"
                rule["conditions"].append(
                    f"Uses the current-week PE about {p.itm_points:g} points ITM"
                )
            elif rule["kind"] == "exit":
                rule["conditions"].append("A reversal sells the held option before buying the opposite leg")
                rule["conditions"].append(
                    "At 15:00 IST on expiry day, rolls the same signal to the next weekly contract"
                )
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
        await self.ctx.advance(now)
        return True

    async def on_tick(self, tick) -> None:
        if before_market_open(tick.at):
            return  # nothing to buy or sell before 09:15: don't move the option legs either
        if isinstance(self.ctx, ReversalOptionLegs):
            if not await self._warm_from_history(tick.at.astimezone(IST)):
                return
            await self.ctx.advance(tick.at)
        await super().on_tick(tick)
        if isinstance(self.ctx, ReversalOptionLegs):
            await self.ctx.advance(tick.at)

