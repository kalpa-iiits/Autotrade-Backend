from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from app.core.schemas import Ping


class Subscribe(BaseModel):
    action: Literal["subscribe", "unsubscribe"]
    request_id: str | None = None
    symbols: list[str] = Field(min_length=1)


TickerMessage = Annotated[Union[Subscribe, Ping], Field(discriminator="action")]
