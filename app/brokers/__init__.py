from pathlib import Path

from app.brokers.base import Broker, MissingCredentials, OrderError
from app.brokers.paper import PaperBroker
from app.core.config import Settings


def create_broker(
    settings: Settings,
    broker: str,
    credentials: dict | None,
    options: dict,
    access_token: str | None = None,
    account_id: int | None = None,
) -> Broker:
    """Build one account's broker from its (decrypted) credentials, options and today's
    access token. Raises MissingCredentials until it can connect (not logged in today ...)."""
    # Real brokers are imported lazily so paper accounts work without their SDKs.
    if broker == "paper":
        return PaperBroker(options.get("starting_cash", settings.paper_starting_cash), settings.tick_interval)
    if broker == "fyers":
        from app.brokers.fyers import FyersBroker

        creds = credentials or {}
        if not creds.get("client_id"):
            raise MissingCredentials("Add this account's Fyers app client id and secret")
        return FyersBroker(
            client_id=creds["client_id"],
            access_token=access_token or "",
            product_type=options.get("product_type", "INTRADAY"),
            log_dir=str(Path(settings.fyers_log_dir) / f"account-{account_id}"),
        )
    if broker == "zerodha":
        from app.brokers.zerodha import ZerodhaBroker

        creds = credentials or {}
        if not creds.get("api_key"):
            raise MissingCredentials("Add this account's Kite Connect API key and secret")
        return ZerodhaBroker(
            api_key=creds["api_key"],
            access_token=access_token or "",
            product=options.get("product", "MIS"),
        )
    raise ValueError(f"Unsupported broker: {broker!r}")


__all__ = ["Broker", "MissingCredentials", "OrderError", "PaperBroker", "create_broker"]
