from collections import deque

from pydantic import BaseModel, Field, model_validator

from app.strategies.base import Bar, Strategy


class SmaCrossover(Strategy):
    name = "sma_crossover"
    description = (
        "Buys when the fast moving average of bar closes crosses above the slow one and exits "
        "when it crosses back below. With allow_short, also sells short on the downward cross."
    )
    tags = ("Trend following", "Moving averages", "Any timeframe")

    @classmethod
    def rules(cls, p: "SmaCrossover.Params") -> list[dict]:
        fast, slow = f"SMA({p.fast})", f"SMA({p.slow})"
        rules = [
            {"kind": "filter", "title": "Warm-up", "conditions": [
                f"Waits for {p.slow} completed bars before the first signal (indicators start empty)",
                "Signals are checked once per bar, at the bar's close",
            ]},
            {"kind": "entry_long", "title": "Buy", "conditions": [
                f"{fast} of closes crosses above {slow}: was at or below on the previous bar, above now",
                "Position is flat (a short is covered first)",
            ]},
            {"kind": "exit", "title": "Exit long", "conditions": [
                f"{fast} crosses back below {slow}",
            ]},
        ]
        if p.allow_short:
            rules.insert(2, {"kind": "entry_short", "title": "Sell short", "conditions": [
                f"{fast} crosses below {slow}, after any long is closed",
            ]})
            rules.append({"kind": "exit", "title": "Cover short", "conditions": [f"{fast} crosses back above {slow}"]})
        rules.append({"kind": "risk", "title": "Risk", "conditions": [
            "No stop-loss or target: the opposite cross is the only exit",
            "Holds overnight if no cross happens (not intraday-only)",
        ]})
        return rules

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
