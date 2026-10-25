import logging
import secrets
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware

from app.auth.api import router as auth_api_router
from app.auth.dependencies import require_user, session_user
from app.auth.pages import login_redirect
from app.auth.pages import router as pages_router
from app.auth.router import callback_router
from app.auth.router import router as auth_router
from app.auth.throttle import LoginThrottle
from app.auth.token_store import TokenStore
from app.core.broker_service import BrokerService
from app.core import db
from app.core.config import settings
from app.core.connections import ConnectionManager
from app.orders.router import publish_order
from app.orders.router import router as orders_router
from app.strategies import deployments
from app.strategies.router import router as strategies_router
from app.strategies.runner import StrategyRunner
from app.tickers.router import publish_tick
from app.tickers.router import router as tickers_router
from app.trades.ledger import TradeLedger
from app.trades.router import router as trades_router

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init(settings.database_url)
    token_stores = {name: TokenStore(name) for name in ("fyers", "zerodha")}
    order_connections = ConnectionManager()
    ticker_connections = ConnectionManager()

    async def active_token() -> str | None:
        store = token_stores.get(settings.broker)
        return await store.load() if store else None

    brokers = BrokerService(settings, token_loader=active_token)
    ledger = TradeLedger(settings.broker)
    brokers.on_order_update(ledger.on_order)  # orders, order_events, fills, positions tables
    brokers.on_order_update(partial(publish_order, order_connections))
    brokers.on_tick(ledger.on_tick)
    brokers.on_tick(partial(publish_tick, ticker_connections))
    brokers.add_watcher(ticker_connections.watched)

    # Strategy signals and status changes are pushed to /ws/orders clients.
    runner = StrategyRunner(brokers, settings, publish=order_connections.broadcast)
    brokers.add_watcher(runner.symbols)
    brokers.on_order_update(runner.on_order)
    brokers.on_tick(runner.on_tick)

    app.state.token_stores = token_stores
    app.state.login_states = {}
    app.state.login_throttle = LoginThrottle()
    await app.state.login_throttle.prune()
    app.state.brokers = brokers
    app.state.ledger = ledger
    app.state.strategy_runner = runner
    app.state.order_connections = order_connections
    app.state.ticker_connections = ticker_connections

    await brokers.start()  # stays disconnected (not an error) until the broker login
    try:
        await runner.sync(deployments.DEPLOYMENTS)  # strategies are defined in code only
        await runner.resume()  # ones that were running before the restart
    except BaseException:
        # Startup failed after the broker connected: close its sockets, or their threads
        # keep the process alive in a state that serves nothing.
        await brokers.stop()
        await db.close()
        raise
    yield
    await runner.shutdown()
    await brokers.stop()
    await db.close()


if not settings.session_secret:
    logging.getLogger(__name__).warning(
        "AUTOTRADE_SESSION_SECRET is not set; using a random one, so logins end on restart"
    )

app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    # Docs are served below at /api/docs, behind the login, instead of the public defaults.
    docs_url=None, redoc_url=None, openapi_url=None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret or secrets.token_urlsafe(32),
    session_cookie="autotrade_session",
    max_age=settings.session_max_age,
    same_site="lax",  # cookie not sent on cross-site POSTs/WebSockets
    https_only=settings.secure_cookies,
)
class SettingsCORSMiddleware(CORSMiddleware):
    """CORS for the frontend origins in AUTOTRADE_CORS_ORIGINS (read on each request, the
    same list the WebSocket origin check uses). Lets a frontend on another origin, e.g. a
    React dev server on :5173, call the API with the session cookie
    (fetch(url, {credentials: "include"}))."""

    def is_allowed_origin(self, origin: str) -> bool:
        return origin.rstrip("/") in {o.rstrip("/") for o in settings.cors_origins}


app.add_middleware(
    SettingsCORSMiddleware,
    allow_origins=[],  # decided by is_allowed_origin above
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

# Everything a frontend uses lives under /api (including the WebSockets).
api = APIRouter(prefix="/api")
api.include_router(auth_api_router)  # /api/auth/login, logout, me, password
api.include_router(auth_router)  # /api/auth/status, broker logins
api.include_router(tickers_router)  # /api/ws/tickers, /api/market/...
api.include_router(orders_router)  # /api/ws/orders, /api/orders/..., /api/account/...
api.include_router(trades_router)  # /api/trades/...
api.include_router(strategies_router)  # /api/strategies/...
app.include_router(api)

app.include_router(pages_router)  # HTML /login and /logout for the built-in test page
app.include_router(callback_router)  # broker redirect URLs as registered with Fyers/Zerodha

if settings.enable_docs:
    @app.get("/api/openapi.json", include_in_schema=False, dependencies=[Depends(require_user)])
    async def openapi_schema():
        return app.openapi()

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs(request: Request):
        if not await session_user(request):
            return login_redirect(request)
        return get_swagger_ui_html(openapi_url="/api/openapi.json", title=f"{settings.app_name} API")


@app.get("/health")
async def health():
    return {"status": "ok", "broker": settings.broker, "connected": app.state.brokers.connected}


@app.get("/", include_in_schema=False)
async def index(request: Request):
    if not await session_user(request):
        return login_redirect(request)
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})
