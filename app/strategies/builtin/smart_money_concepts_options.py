"""Independent 500-ITM option legs for the supplied Internal CHoCH 25% / 3R scripts.

The call and put systems are deliberately separate strategy classes and deployments. The
runner tracks one physical option symbol per instance, so separate instances let both legs be
held at once without netting a call against a put or sharing their bracket state.
"""

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from app.brokers.base import Candle
from app.orders.schemas import Product
from app.strategies.base import Bar, Strategy, Tick, before_market_open
from app.strategies.builtin.intraday_breakout_options import IST, pick_contract


@dataclass
class _Pivot:
    level: float | None = None
    index: int | None = None
    crossed: bool = False
    # Lowest low since a high pivot, or highest high since a low pivot.
    extreme: float | None = None


@dataclass
class _Bracket:
    entry: float
    stop: float
    target: float
    created: int


class _OptionLeg:
    """Map an underlying long/short position to a physically long CE/PE option."""

    def __init__(self, ctx, kind: Literal["CE", "PE"], itm_points: float) -> None:
        self.ctx = ctx
        self.kind = kind
        self.itm_points = itm_points
        self.held = ctx.held_symbol

    @property
    def position(self) -> int:
        units = self.ctx.position
        return units if self.kind == "CE" else -units

    async def open(self, at: datetime, reason: str) -> bool:
        if self.ctx.position or self.ctx.order_pending:
            return False
        spot = self.ctx.last_price
        contracts = await self.ctx.option_contracts()
        contract = (pick_contract(contracts, self.kind, spot, self.itm_points,
                                  at.astimezone(IST).date()) if spot else None)
        if contract is None:
            self.ctx.log(
                f"skipped ({reason}): no {self.kind} contract found "
                f"{self.itm_points:g} points ITM near {spot}"
            )
            return False
        quantity = self.ctx.quantity // contract.lot * contract.lot
        if quantity == 0:
            self.ctx.log(
                f"skipped ({reason}): {self.ctx.quantity} units is less than one lot "
                f"of {contract.lot}"
            )
            return False
        self.held = contract.symbol
        await self.ctx.buy(
            quantity,
            f"{reason}; buy {self.kind} {contract.strike:g} {contract.expiry:%d %b}",
            symbol=contract.symbol,
            product=Product.DELIVERY,
        )
        return True

    async def exit(self, reason: str) -> None:
        if self.ctx.position and self.held:
            await self.ctx.sell(
                self.ctx.position, reason, symbol=self.held, product=Product.DELIVERY)


class InternalChoch3ROptions(Strategy):
    """Shared signal and bracket engine; subclasses select the independent option direction."""

    checkpoint_version = 1
    direction: ClassVar[Literal[1, -1]]
    option_kind: ClassVar[Literal["CE", "PE"]]
    capital_sizing = False
    tags = ("Internal CHoCH", "Options", "25% retracement", "3R target")
    backtest_note = (
        "Backtests replay the underlying-index entries and brackets, not historical option "
        "premiums. Run both catalog entries to operate the call and put legs simultaneously."
    )

    class Params(BaseModel):
        retrace: float = Field(
            0.25, ge=0, le=1, description="Entry fraction from signal candle extreme")
        risk_reward: float = Field(3.0, ge=0.25, description="Target as a multiple of risk")
        expiry_bars: int = Field(10, ge=1, description="Pending limit-order expiry in bars")
        itm_points: float = Field(500, ge=0, description="Option depth in the money, in index points")

    @classmethod
    def rules(cls, p: "InternalChoch3ROptions.Params") -> list[dict]:
        bullish = cls.direction == 1
        side, kind = ("bullish", "call (CE)") if bullish else ("bearish", "put (PE)")
        entry_from = "high" if bullish else "low"
        stop_at = "lowest low since the broken internal high" if bullish else (
            "highest high since the broken internal low")
        return [
            {"kind": "filter", "title": "Signal", "conditions": [
                f"Uses confirmed internal {side} CHoCH events only",
                "Internal pivots confirm after 5 newer candles",
                f"Each pending order expires after {p.expiry_bars} completed bars",
            ]},
            {"kind": "entry_long" if bullish else "entry_short",
             "title": f"Buy the {kind}", "conditions": [
                f"Underlying limit is {p.retrace:g} of the signal candle range from its {entry_from}",
                f"On a fill, buys the nearest-expiry {kind} {p.itm_points:g} points ITM",
                "This leg is independent of the opposite option strategy",
            ]},
            {"kind": "risk", "title": "Bracket", "conditions": [
                f"Underlying stop is the {stop_at}",
                f"Underlying target is {p.risk_reward:g}R from the limit entry",
                "The option is sold when either underlying level is reached",
            ]},
            {"kind": "exit", "title": "Independent exit", "conditions": [
                "Exiting this option does not close an option held by the opposite leg",
                "No forced intraday square-off; a filled leg may be held overnight",
            ]},
        ]

    async def on_start(self) -> None:
        self._bars: deque[Bar] = deque(maxlen=6)
        self._bar_index = -1
        self._leg = 0
        self._trend = 0
        self._high = _Pivot()
        self._low = _Pivot()
        self._previous_close: float | None = None
        self._pending: list[_Bracket] = []
        self._submitted: _Bracket | None = None
        self._active: _Bracket | None = None
        self._exit_pending = False
        self._option = (_OptionLeg(self.ctx, self.option_kind, self.params.itm_points)
                        if hasattr(self.ctx, "option_contracts") else None)
        self._restore_checkpoint()
        self._sync_orders()
        if self._position and self._active is None:
            raise RuntimeError(
                "option position exists but no compatible CHoCH bracket checkpoint was found"
            )

    @property
    def _position(self) -> int:
        return self._option.position if self._option else self.ctx.position

    @property
    def _order_pending(self) -> bool:
        return bool(getattr(self.ctx, "order_pending", False))

    def _sync_orders(self) -> None:
        if self._submitted and self._position:
            self._active, self._submitted = self._submitted, None
        elif self._submitted and not self._position and not self._order_pending:
            self._submitted = None  # rejected/cancelled physical entry
        if self._exit_pending and not self._position and not self._order_pending:
            self._active = None
            self._exit_pending = False

    def _update_structure(self) -> None:
        if len(self._bars) < 6:
            return
        rows = list(self._bars)
        candidate, newer = rows[0], rows[1:]
        if candidate.high > max(bar.high for bar in newer):
            new_leg = 0
        elif candidate.low < min(bar.low for bar in newer):
            new_leg = 1
        else:
            new_leg = self._leg
        if new_leg == self._leg:
            return
        self._leg = new_leg
        if new_leg == 1:
            self._low = _Pivot(candidate.low, self._bar_index - 5, False,
                               max(bar.high for bar in rows))
        else:
            self._high = _Pivot(candidate.high, self._bar_index - 5, False,
                                min(bar.low for bar in rows))

    def _update_extremes(self, bar: Bar) -> None:
        if self._high.level is not None:
            self._high.extreme = (bar.low if self._high.extreme is None else
                                  min(self._high.extreme, bar.low))
        if self._low.level is not None:
            self._low.extreme = (bar.high if self._low.extreme is None else
                                 max(self._low.extreme, bar.high))

    def _choch(self, close: float) -> _Pivot | None:
        previous = self._previous_close
        if previous is None:
            return None
        selected: _Pivot | None = None
        if (self._high.level is not None and not self._high.crossed
                and previous <= self._high.level < close):
            choch = self._trend == -1
            self._high.crossed = True
            self._trend = 1
            if choch and self.direction == 1:
                selected = self._high
        if (self._low.level is not None and not self._low.crossed
                and previous >= self._low.level > close):
            choch = self._trend == 1
            self._low.crossed = True
            self._trend = -1
            if choch and self.direction == -1:
                selected = self._low
        return selected

    def _new_bracket(self, bar: Bar, pivot: _Pivot) -> _Bracket | None:
        if pivot.extreme is None:
            return None
        width = bar.high - bar.low
        if self.direction == 1:
            entry = bar.high - self.params.retrace * width
            stop = pivot.extreme
            risk = entry - stop
            target = entry + self.params.risk_reward * risk
        else:
            entry = bar.low + self.params.retrace * width
            stop = pivot.extreme
            risk = stop - entry
            target = entry - self.params.risk_reward * risk
        tick = getattr(self.ctx, "tick_size", None) or 0
        return _Bracket(entry, stop, target, self._bar_index) if risk > tick else None

    async def _process_bar(self, bar: Bar) -> None:
        self._bar_index += 1
        self._pending = [order for order in self._pending
                         if self._bar_index <= order.created + self.params.expiry_bars]
        self._bars.append(bar)
        self._update_structure()
        self._update_extremes(bar)
        pivot = self._choch(bar.close)
        self._previous_close = bar.close
        if pivot is not None and not self._position and self._submitted is None:
            bracket = self._new_bracket(bar, pivot)
            if bracket:
                self._pending.append(bracket)
        await self._save_checkpoint()

    def _entry_hit(self, price: float, order: _Bracket) -> bool:
        return price <= order.entry if self.direction == 1 else price >= order.entry

    async def _check_entry(self, price: float, at: datetime, *, exact: bool = False,
                           gap: bool = False) -> None:
        if self._position or self._submitted or self._order_pending or not self._pending:
            return
        order = next((value for value in self._pending if self._entry_hit(price, value)), None)
        if order is None:
            return
        reason = (
            f"internal {'bullish' if self.direction == 1 else 'bearish'} CHoCH; "
            f"25% limit {order.entry:.2f}, stop {order.stop:.2f}, target {order.target:.2f}"
        )
        submitted = False
        if self._option:
            submitted = await self._option.open(at, reason)
        elif exact:
            quantity = self.ctx.quantity
            fill = price if gap else order.entry
            if self.direction == 1:
                await self.ctx.buy_at(fill, at, reason, quantity=quantity)
            else:
                await self.ctx.sell_at(fill, at, reason, quantity=quantity)
            submitted = True
        else:
            if self.direction == 1:
                await self.ctx.buy(reason=reason)
            else:
                await self.ctx.sell(reason=reason)
            submitted = True
        if submitted:
            self._submitted = order
            self._pending.clear()
            self._sync_orders()
            await self._save_checkpoint()

    def _exit_hit(self, price: float) -> tuple[str, float] | None:
        if not self._active or not self._position:
            return None
        if self.direction == 1:
            if price <= self._active.stop:
                return "stop", self._active.stop
            if price >= self._active.target:
                return "target", self._active.target
        else:
            if price >= self._active.stop:
                return "stop", self._active.stop
            if price <= self._active.target:
                return "target", self._active.target
        return None

    async def _check_exit(self, price: float, at: datetime, *, exact: bool = False,
                          gap: bool = False) -> None:
        if self._exit_pending or self._order_pending:
            return
        hit = self._exit_hit(price)
        if hit is None:
            return
        kind, level = hit
        reason = f"underlying {kind} at {level:.2f}"
        if self._option:
            await self._option.exit(reason)
        elif exact:
            await self.ctx.exit_at(price if gap else level, at, reason)
        else:
            await self.ctx.exit(reason)
        self._exit_pending = True
        self._sync_orders()
        await self._save_checkpoint()

    async def on_tick(self, tick: Tick) -> None:
        if before_market_open(tick.at):
            return
        self._sync_orders()
        if bar := self._tick_builder.add(tick):
            await self._process_bar(bar)
        await self._check_exit(tick.price, tick.at)
        await self._check_entry(tick.price, tick.at)

    async def on_backtest_candle(self, candle: Candle) -> None:
        """Emulate next-bar limit availability and exact underlying bracket levels."""
        end = candle.start + timedelta(seconds=self.bar_seconds)
        path = [candle.open]
        path += [candle.low, candle.high] if candle.close >= candle.open else [candle.high, candle.low]
        path += [candle.close]
        for index, price in enumerate(path):
            at = candle.start + timedelta(seconds=index * self.bar_seconds / 3)
            self.ctx.mark(price, at)
            self._sync_orders()
            await self._check_exit(price, at, exact=True, gap=index == 0)
            await self._check_entry(price, at, exact=True, gap=index == 0)
        bar = Bar(self.ctx.symbol, candle.start, self.bar_seconds, candle.open, candle.high,
                  candle.low, candle.close, 4)
        await self._process_bar(bar)
        self.ctx.mark(candle.close, end)

    def __init__(self, params: BaseModel, ctx, bar_seconds: int = 60) -> None:
        super().__init__(params, ctx, bar_seconds)
        # on_start uses _bars for the six completed candles needed by internal structure.
        self._tick_builder = self._bars

    def _checkpoint(self) -> dict:
        def pivot(value: _Pivot) -> dict:
            return asdict(value)

        def bracket(value: _Bracket | None):
            return asdict(value) if value else None

        return {
            "version": self.checkpoint_version,
            "strategy": self.name,
            "bar_seconds": self.bar_seconds,
            "params": self.params.model_dump(mode="json"),
            "bar_index": self._bar_index,
            "leg": self._leg,
            "trend": self._trend,
            "previous_close": self._previous_close,
            "high": pivot(self._high),
            "low": pivot(self._low),
            "bars": [{"symbol": b.symbol, "start": b.start.isoformat(), "seconds": b.seconds,
                      "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                      "ticks": b.ticks} for b in self._bars],
            "pending": [asdict(value) for value in self._pending],
            "submitted": bracket(self._submitted),
            "active": bracket(self._active),
            "exit_pending": self._exit_pending,
        }

    def _restore_checkpoint(self) -> None:
        state = getattr(self.ctx, "strategy_state", None)
        if not isinstance(state, dict):
            return
        expected = {"version": self.checkpoint_version, "strategy": self.name,
                    "bar_seconds": self.bar_seconds, "params": self.params.model_dump(mode="json")}
        if any(state.get(key) != value for key, value in expected.items()):
            return
        try:
            self._bar_index = int(state["bar_index"])
            self._leg = int(state["leg"])
            self._trend = int(state["trend"])
            self._previous_close = state.get("previous_close")
            self._high = _Pivot(**state["high"])
            self._low = _Pivot(**state["low"])
            self._bars = deque((Bar(
                row["symbol"], datetime.fromisoformat(row["start"]), int(row["seconds"]),
                float(row["open"]), float(row["high"]), float(row["low"]),
                float(row["close"]), int(row["ticks"]),
            ) for row in state.get("bars", [])), maxlen=6)
            self._pending = [_Bracket(**row) for row in state.get("pending", [])]
            self._submitted = (_Bracket(**state["submitted"]) if state.get("submitted") else None)
            self._active = _Bracket(**state["active"]) if state.get("active") else None
            self._exit_pending = bool(state.get("exit_pending"))
        except (KeyError, TypeError, ValueError):
            return

    async def _save_checkpoint(self) -> None:
        save = getattr(self.ctx, "save_strategy_state", None)
        if save:
            await save(self._checkpoint())


class InternalChoch3RCalls(InternalChoch3ROptions):
    name = "internal_choch_3r_calls_options"
    description = (
        "Confirmed internal bullish CHoCH: buy a 500-point ITM call on a 25% retracement, "
        "with the structure low as stop and a 3R target."
    )
    direction = 1
    option_kind = "CE"


class InternalChoch3RPuts(InternalChoch3ROptions):
    name = "internal_choch_3r_puts_options"
    description = (
        "Confirmed internal bearish CHoCH: buy a 500-point ITM put on a 25% retracement, "
        "with the structure high as stop and a 3R target."
    )
    direction = -1
    option_kind = "PE"

