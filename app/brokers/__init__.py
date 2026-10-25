from app.brokers.base import Broker, MissingCredentials, OrderError
from app.brokers.paper import PaperBroker
from app.core.config import Settings


def create_broker(settings: Settings, access_token: str | None = None) -> Broker:
    """Build the configured broker. `access_token` is today's saved login, if any;
    it takes precedence over a token set in the environment."""
    # Real brokers are imported lazily so the paper broker works without their SDKs.
    if settings.broker == "paper":
        return PaperBroker(settings.paper_starting_cash, settings.tick_interval)
    if settings.broker == "fyers":
        from app.brokers.fyers import FyersBroker

        return FyersBroker(
            client_id=settings.fyers_client_id,
            access_token=access_token or settings.fyers_access_token,
            product_type=settings.fyers_product_type,
            log_dir=settings.fyers_log_dir,
        )
    if settings.broker == "zerodha":
        from app.brokers.zerodha import ZerodhaBroker

        return ZerodhaBroker(
            api_key=settings.zerodha_api_key,
            access_token=access_token or settings.zerodha_access_token,
            product=settings.zerodha_product,
        )
    raise ValueError(f"Unsupported broker: {settings.broker!r}")


__all__ = ["Broker", "MissingCredentials", "OrderError", "PaperBroker", "create_broker"]
