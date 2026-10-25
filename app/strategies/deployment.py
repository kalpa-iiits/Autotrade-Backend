import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from app.strategies.base import TIMEFRAMES, Strategy

KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Deployment:
    """A strategy with its parameters, size, limits and mode, declared in code
    (app/strategies/deployments.py). The app runs it as one or more *instances*, each on its own
    symbol and timeframe, chosen in the app.

    `key` identifies it in the database across restarts: keep it stable. `symbol` and
    `timeframe` are only the defaults for the first instance, created automatically; after
    that the app owns them. Changing any other field takes effect on the next server start, for
    every instance; instances that were running then come back stopped.
    """

    key: str
    strategy: type[Strategy]
    symbol: str  # default for the first instance
    quantity: int  # default size of buy()/sell()
    params: BaseModel | dict = field(default_factory=dict)
    # "signal": record what it would trade, place nothing. "live": place real orders
    # (also needs AUTOTRADE_STRATEGIES_LIVE=true).
    mode: Literal["signal", "live"] = "signal"
    max_order_qty: int | None = None  # default: quantity
    max_position: int | None = None  # absolute net quantity; default: quantity
    max_orders_per_day: int = 20
    label: str | None = None
    timeframe: str | None = None  # default for the first instance; None = strategy's default

    def __post_init__(self) -> None:
        if not KEY_PATTERN.match(self.key):
            raise ValueError(f"Deployment key {self.key!r}: use lowercase letters, digits, _ or -")
        if not (isinstance(self.strategy, type) and issubclass(self.strategy, Strategy)):
            raise TypeError(f"{self.key}: strategy must be a Strategy subclass")
        if self.mode not in ("signal", "live"):
            raise ValueError(f"{self.key}: mode must be 'signal' or 'live'")
        for name in ("quantity", "max_order_qty", "max_position", "max_orders_per_day"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{self.key}: {name} must be positive")
        # Validate params against the strategy's Params model (dicts are converted).
        params = self.params
        if isinstance(params, dict):
            params = self.strategy.Params(**params)
        elif not isinstance(params, self.strategy.Params):
            raise TypeError(f"{self.key}: params must be {self.strategy.__name__}.Params or a dict")
        object.__setattr__(self, "params", params)
        timeframe = self.timeframe or self.strategy.default_timeframe
        self.check_timeframe(timeframe)
        object.__setattr__(self, "timeframe", timeframe)

    def check_timeframe(self, timeframe: str) -> None:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unknown timeframe {timeframe!r}; use one of {', '.join(TIMEFRAMES)}")
        if timeframe not in self.strategy.timeframes:
            raise ValueError(
                f"{self.strategy.name} supports {', '.join(self.strategy.timeframes)}, not {timeframe}"
            )

    def settings(self) -> dict:
        """Fields copied from code onto every instance of this deployment at startup.
        (Symbol and timeframe are not among them: the app owns those.)"""
        return {
            "strategy": self.strategy.name,
            "label": self.label or self.key,
            "params": self.params.model_dump(mode="json"),
            "mode": self.mode,
            "quantity": self.quantity,
            "max_order_qty": self.max_order_qty or self.quantity,
            "max_position": self.max_position or self.quantity,
            "max_orders_per_day": self.max_orders_per_day,
        }
