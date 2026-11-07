"""What each broker needs from the user: credentials (secret, stored encrypted) and options
(not secret, stored as JSON). Validation happens here, before anything is saved."""

import base64
import binascii
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import settings

BrokerName = Literal["paper", "fyers", "zerodha"]
# Optional credential fields (all or none) that let the server log an account in each morning
# by itself (app.accounts.autologin) instead of the daily browser login.
AUTO_LOGIN_FIELDS: dict[str, tuple[str, ...]] = {
    "fyers": ("fy_id", "pin", "totp_secret"),
    "zerodha": ("user_id", "password", "totp_secret"),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _totp_key(v: str | None) -> str | None:
    v = (v or "").replace(" ", "").upper() or None
    if v:
        try:
            base64.b32decode(v + "=" * (-len(v) % 8))
        except (binascii.Error, ValueError):
            raise ValueError("not a TOTP key (base32 letters A-Z and digits 2-7)") from None
    return v


def _all_or_none(model: BaseModel, broker: str) -> None:
    fields = AUTO_LOGIN_FIELDS[broker]
    given = [bool(getattr(model, f)) for f in fields]
    if any(given) and not all(given):
        raise ValueError(f"auto-login needs all of {', '.join(fields)}")


class FyersCredentials(_Strict):
    client_id: str = Field(min_length=1, max_length=64, description='Fyers app id, e.g. "XB12345-100"')
    secret_key: str = Field(min_length=1, max_length=256, description="Fyers app secret")
    # Optional, all three or none: for the morning auto-login.
    fy_id: str | None = Field(default=None, min_length=1, max_length=32, description='Fyers user id, e.g. "XA12345"')
    pin: str | None = Field(default=None, pattern=r"^\d{4}$", description="4-digit Fyers login PIN")
    totp_secret: str | None = Field(default=None, max_length=128, description="TOTP key from Fyers' external 2FA setup")

    @field_validator("totp_secret")
    @classmethod
    def _base32(cls, v: str | None) -> str | None:
        return _totp_key(v)

    @model_validator(mode="after")
    def _auto_login(self):
        _all_or_none(self, "fyers")
        return self


class FyersOptions(_Strict):
    product_type: Literal["INTRADAY", "CNC", "MARGIN", "MTF"] = "INTRADAY"


class ZerodhaCredentials(_Strict):
    api_key: str = Field(min_length=1, max_length=64, description="Kite Connect API key")
    api_secret: str = Field(min_length=1, max_length=256, description="Kite Connect API secret")
    # Optional, all three or none: for the morning auto-login.
    user_id: str | None = Field(default=None, min_length=1, max_length=32, description='Kite user id, e.g. "AB1234"')
    password: str | None = Field(default=None, min_length=1, max_length=128, description="Kite login password")
    totp_secret: str | None = Field(default=None, max_length=128, description="TOTP key from Kite's external 2FA setup")

    @field_validator("totp_secret")
    @classmethod
    def _base32(cls, v: str | None) -> str | None:
        return _totp_key(v)

    @model_validator(mode="after")
    def _auto_login(self):
        _all_or_none(self, "zerodha")
        return self


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
                        optional=AUTO_LOGIN_FIELDS["fyers"]),
    "zerodha": BrokerSpec(ZerodhaCredentials, ZerodhaOptions, identifiers=("api_key", "user_id"), daily_login=True,
                          optional=AUTO_LOGIN_FIELDS["zerodha"]),
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
