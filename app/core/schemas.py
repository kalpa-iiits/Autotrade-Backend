from typing import Literal

from pydantic import BaseModel


class Ping(BaseModel):
    """Keep-alive any app's socket accepts; answered with {"type": "pong"}."""

    action: Literal["ping"]
    request_id: str | None = None
