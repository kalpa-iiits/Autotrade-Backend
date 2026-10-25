"""Turning broker calls into HTTP responses, shared by the REST routers."""

from contextlib import asynccontextmanager

from fastapi import HTTPException, status

from app.brokers import Broker, OrderError
from app.core.broker_service import BrokerNotConnected, BrokerService


def broker_or_503(service: BrokerService) -> Broker:
    try:
        return service.require()
    except BrokerNotConnected as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))


@asynccontextmanager
async def broker_errors():
    """OrderError (the broker refused: bad symbol, can't cancel...) -> 422;
    anything else from the broker (network, API down) -> 502."""
    try:
        yield
    except HTTPException:
        raise
    except BrokerNotConnected as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    except OrderError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Broker error: {e}")
