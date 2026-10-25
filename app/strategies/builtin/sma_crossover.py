from collections import deque

from pydantic import BaseModel, Field, model_validator

from app.strategies.base import Bar, Strategy


class SmaCrossover(Strategy):
    name = "sma_crossover"
    description = (
        "Buys when the fast moving average of bar closes crosses above the slow one and exits "
        "when it crosses back below. With allow_short, also sells short on the downward cross."
    )

    class Params(BaseModel):
        fast: int = Field(9, ge=1, description="Fast moving average length, in bars")
        slow: int = Field(21, ge=2, description="Slow moving average length, in bars")
        allow_short: bool = Field(False, description="Also go short on a downward cross")

        @model_validator(mode="after")
        def _fast_below_slow(self):
            if self.fast >= self.slow:
                raise ValueError("fast must be smaller than slow")
            return self

    async def on_start(self) -> None:
        self.closes: deque[float] = deque(maxlen=self.params.slow)
        self.prev_diff: float | None = None

    async def on_bar(self, bar: Bar) -> None:
        self.closes.append(bar.close)
        if len(self.closes) < self.params.slow:
            return  # still warming up
        closes = list(self.closes)
        fast = sum(closes[-self.params.fast:]) / self.params.fast
        slow = sum(closes) / self.params.slow
        diff, prev, self.prev_diff = fast - slow, self.prev_diff, fast - slow
        if prev is None:
            return

        crossed_up = prev <= 0 < diff
        crossed_down = prev >= 0 > diff
        pos = self.ctx.position
        if crossed_up:
            if pos < 0:
                await self.ctx.exit(reason=f"fast {fast:.2f} crossed above slow {slow:.2f}: cover")
            if self.ctx.position == 0:
                await self.ctx.buy(reason=f"fast {fast:.2f} crossed above slow {slow:.2f}")
        elif crossed_down:
            if pos > 0:
                await self.ctx.exit(reason=f"fast {fast:.2f} crossed below slow {slow:.2f}")
            if self.params.allow_short and self.ctx.position == 0:
                await self.ctx.sell(reason=f"fast {fast:.2f} crossed below slow {slow:.2f}: short")
