from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Union
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from app.core.schemas import Ping


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"  # stop-limit: at trigger_price it becomes a LIMIT order at limit_price
    SL_M = "SL-M"  # stop-market: at trigger_price it becomes a MARKET order


class Product(str, Enum):
    """Per order; missing means the account's default product (its options)."""
    INTRADAY = "INTRADAY"  # squared off by the broker the same day (Fyers INTRADAY, Kite MIS)
    DELIVERY = "DELIVERY"  # held overnight (Fyers CNC/MARGIN, Kite CNC/NRML)


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


TERMINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED})


def moves_backwards(prev_status: OrderStatus, prev_filled: int,
                    status: OrderStatus, filled: int) -> bool:
    """True if an update would undo what is already known about an order: less filled than
    before, or a finished order open again. Such updates are stale (e.g. place_order's own
    "OPEN" arriving after the socket already reported the fill) and must be ignored."""
    return filled < prev_filled or (prev_status in TERMINAL_STATUSES and status == OrderStatus.OPEN)


# Our id for an order, sent to the broker as its tag (Fyers orderTag, Kite tag): letters and
# digits only, at most 20 (Kite's limit). Placing again with the same id never places twice.
CLIENT_ORDER_ID_PATTERN = r"^[A-Za-z0-9]{1,20}$"


def new_client_order_id() -> str:
    return uuid4().hex[:20]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Order(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    client_order_id: str | None = None
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: float | None = None
    trigger_price: float | None = None  # SL / SL-M only
    product: Product | None = None
    status: OrderStatus = OrderStatus.OPEN
    filled_quantity: int = 0  # cumulative; fills are derived from changes in this
    filled_price: float | None = None  # average price of the filled quantity
    reason: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Position(BaseModel):
    symbol: str
    quantity: int
    avg_price: float


# ---------- Messages the client sends over /ws/orders ----------
# Every message carries an "action" field; pydantic picks the right model from it.

class PlaceOrder(BaseModel):
    action: Literal["place_order"]
    request_id: str | None = None
    symbol: str = Field(min_length=1)
    side: Side
    quantity: int = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = Field(default=None, gt=0)
    trigger_price: float | None = Field(default=None, gt=0)
    product: Product | None = None
    # Optional; send the same one when retrying so a lost reply can't place a second order.
    # Missing: the server makes one up (still sent to the broker as the order's tag).
    client_order_id: str | None = Field(default=None, pattern=CLIENT_ORDER_ID_PATTERN)

    @model_validator(mode="after")
    def _check_prices(self):
        if self.order_type in (OrderType.LIMIT, OrderType.SL) and self.limit_price is None:
            raise ValueError(f"limit_price is required for {self.order_type.value} orders")
        if self.order_type in (OrderType.SL, OrderType.SL_M) and self.trigger_price is None:
            raise ValueError(f"trigger_price is required for {self.order_type.value} orders")
        if self.order_type in (OrderType.MARKET, OrderType.LIMIT):
            self.trigger_price = None
        if self.order_type in (OrderType.MARKET, OrderType.SL_M):
            self.limit_price = None
        return self


class CancelOrder(BaseModel):
    action: Literal["cancel_order"]
    request_id: str | None = None
    order_id: str


class OrderQuery(BaseModel):
    action: Literal["get_orders", "get_positions", "get_funds"]
    request_id: str | None = None


OrderMessage = Annotated[
    Union[PlaceOrder, CancelOrder, OrderQuery, Ping],
    Field(discriminator="action"),
]
