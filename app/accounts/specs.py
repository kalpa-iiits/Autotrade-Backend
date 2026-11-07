"""What each broker needs from the user: credentials (secret, stored encrypted) and options
(not secret, stored as JSON). Validation happens here, before anything is saved."""

import base64
import binascii
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import settings

BrokerName = Literal["paper", "fyers", "zerodha"]
AUTO_LOGIN_FIELDS = ("fy_id", "pin", "totp_secret")  # Fyers: optional, for the morning auto-login


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FyersCredentials(_Strict):
    client_id: str = Field(min_length=1, max_length=64, description='Fyers app id, e.g. "XB12345-100"')
    secret_key: str = Field(min_length=1, max_length=256, description="Fyers app secret")
    # Optional, all three or none: lets the server log in each morning by itself
    # (app.accounts.autologin) instead of the daily browser login.
    fy_id: str | None = Field(default=None, min_length=1, max_length=32, description='Fyers user id, e.g. "XA12345"')
    pin: str | None = Field(default=None, pattern=r"^\d{4}$", description="4-digit Fyers login PIN")
    totp_secret: str | None = Field(default=None, max_length=128, description="TOTP key from Fyers' external 2FA setup")

    @field_validator("totp_secret")
    @classmethod
    def _base32(cls, v: str | None) -> str | None:
        v = (v or "").replace(" ", "").upper() or None
        if v:
            try:
                base64.b32decode(v + "=" * (-len(v) % 8))
            except (binascii.Error, ValueError):
                raise ValueError("not a TOTP key (base32 letters A-Z and digits 2-7)") from None
        return v

    @model_validator(mode="after")
    def _all_or_none(self):
        given = [bool(getattr(self, f)) for f in AUTO_LOGIN_FIELDS]
        if any(given) and not all(given):
            raise ValueError("auto-login needs all of fy_id, pin and totp_secret")
        return self


class FyersOptions(_Strict):
    product_type: Literal["INTRADAY", "CNC", "MARGIN", "MTF"] = "INTRADAY"


class ZerodhaCredentials(_Strict):
    api_key: str = Field(min_length=1, max_length=64, description="Kite Connect API key")
    api_secret: str = Field(min_length=1, max_length=256, description="Kite Connect API secret")


class ZerodhaOptions(_Strict):
    product: Literal["MIS", "CNC", "NRML"] = "MIS"


class PaperOptions(_Strict):
    starting_cash: float = Field(default_factory=lambda: settings.paper_starting_cash, gt=0, le=1e12)


@dataclass(frozen=True)
class BrokerSpec:
    credentials: type[BaseModel] | None  # None: needs none (paper)
    options: type[BaseModel]
    # Credential fields shown back (masked) so the user can tell accounts apart; the others
    # are never shown, only whether they are set.
    identifiers: tuple[str, ...] = ()
    daily_login: bool = False
    optional: tuple[str, ...] = ()  # credential fields that may be left empty


SPECS: dict[str, BrokerSpec] = {
    "paper": BrokerSpec(None, PaperOptions),
    "fyers": BrokerSpec(FyersCredentials, FyersOptions, identifiers=("client_id", "fy_id"), daily_login=True,
                        optional=AUTO_LOGIN_FIELDS),
    "zerodha": BrokerSpec(ZerodhaCredentials, ZerodhaOptions, identifiers=("api_key",), daily_login=True),
}


def mask(value: str) -> str:
    return "••••" if len(value) <= 6 else f"{value[:4]}…{value[-2:]}"


def masked_credentials(broker: str, credentials: dict | None) -> dict | None:
    spec = SPECS[broker]
    if spec.credentials is None:
        return None
    credentials = credentials or {}
    return {
        name: (mask(credentials[name]) if name in spec.identifiers else "set") if credentials.get(name) else None
        for name in spec.credentials.model_fields
    }
