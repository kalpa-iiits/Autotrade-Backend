"""What a strategy is: a class that reacts to ticks or bars and asks for trades.

Write one by subclassing Strategy (see app/strategies/builtin/sma_crossover.py), then run it
by adding a Deployment to app/strategies/deployments.py:

    class MyStrategy(Strategy):
        name = "my_strategy"
        description = "One line shown in the UI"

        class Params(BaseModel):
            lookback: int = Field(20, ge=2)

        async def on_bar(self, bar: Bar) -> None:
            if <entry condition> and self.ctx.position == 0:
                await self.ctx.buy(reason="why")
            elif <exit condition> and self.ctx.position > 0:
                await self.ctx.exit(reason="why")

A strategy never talks to the broker directly. Its buy/sell/exit requests go through the
runner, which applies the instance's mode (signal or live) and risk limits first.
"""

from abc import ABC
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from app.strategies.runner import StrategyContext


# Bar timeframes an instance can run on (chosen in the app, per instance).
TIMEFRAMES: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900, "30m": 1800, "60m": 3600,
}


@dataclass
class Tick:
    symbol: str
    price: float
    at: datetime


@dataclass
class Bar:
    symbol: str
    start: datetime
    seconds: int
    open: float
    high: float
    low: float
    close: float
    ticks: int


class BarBuilder:
    """Groups ticks into fixed time bars (by tick arrival time); returns a bar when one closes."""

    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        self._bucket: int | None = None
        self._bar: Bar | None = None

    def add(self, tick: Tick) -> Bar | None:
        bucket = int(tick.at.timestamp()) // self.seconds
        if bucket == self._bucket and self._bar is not None:
            b = self._bar
            b.high, b.low, b.close = max(b.high, tick.price), min(b.low, tick.price), tick.price
            b.ticks += 1
            return None
        finished = self._bar
        self._bucket = bucket
        self._bar = Bar(
            symbol=tick.symbol, seconds=self.seconds,
            start=datetime.fromtimestamp(bucket * self.seconds, tz=timezone.utc),
            open=tick.price, high=tick.price, low=tick.price, close=tick.price, ticks=1,
        )
        return finished


class Strategy(ABC):
    name: ClassVar[str]
    description: ClassVar[str] = ""

    class Params(BaseModel):
        """Override with the strategy's settings; validated when the deployment is loaded."""

    # Timeframes this strategy may run on, and the one new deployments get by default.
    timeframes: ClassVar[tuple[str, ...]] = tuple(TIMEFRAMES)
    default_timeframe: ClassVar[str] = "1m"

    def __init__(self, params: BaseModel, ctx: "StrategyContext", bar_seconds: int = 60) -> None:
        self.params = params
        self.ctx = ctx
        self.bar_seconds = bar_seconds  # the instance's timeframe
        self._bars = BarBuilder(bar_seconds)

    async def on_start(self) -> None:
        """Called once when the instance starts (indicators start empty)."""

    async def on_tick(self, tick: Tick) -> None:
        """Every price update. The default builds bars and calls on_bar."""
        if bar := self._bars.add(tick):
            await self.on_bar(bar)

    async def on_bar(self, bar: Bar) -> None:
        """A completed bar of the instance's timeframe (`self.bar_seconds`)."""

    async def on_stop(self) -> None:
        """Called when the instance stops."""
