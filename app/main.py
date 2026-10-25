import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, Response
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.accounts.registry import AccountRegistry
from app.accounts.router import router as accounts_router
from app.accounts.vault import vault
from app.auth.api import router as auth_api_router
from app.auth.dependencies import require_user, session_user
from app.auth.pages import login_redirect
from app.auth.pages import router as pages_router
from app.auth.router import callback_router
from app.auth.router import router as broker_login_router
from app.auth.throttle import LoginThrottle
from app.core import db
from app.core.config import obsolete_settings, settings
from app.core.instance_lock import InstanceLock, lock_path
from app.orders.router import router as orders_router
from app.strategies import deployments
from app.strategies.router import router as strategies_router
from app.strategies.runner import StrategyRunner
from app.tickers.router import router as tickers_router
from app.trades.router import router as trades_router

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    lock = InstanceLock(lock_path(settings.lock_file, settings.database_url))
    lock.acquire()  # one server process only: see app/core/instance_lock.py
    try:
        async with _services(app):
            yield
    finally:
        lock.release()


@asynccontextmanager
async def _services(app: FastAPI):
    if obsolete := obsolete_settings():
        log.warning("Ignoring settings that no longer exist: %s. Broker credentials now live in "
                    "each user's broker accounts (add them in the app), not in .env.", ", ".join(obsolete))
    if not vault().configured:
        log.warning("AUTOTRADE_CREDENTIALS_KEY is not set: only paper accounts can be added. "
                    "Generate one with: python -m app.accounts.vault generate-key")
    await db.init(settings.database_url)

    # Every broker account gets its own connection, ledger and sockets (app/accounts/registry.py).
    accounts = AccountRegistry(settings)
    runner = StrategyRunner(accounts, settings)
    accounts.runner = runner

    app.state.login_states = {}
    app.state.login_throttle = LoginThrottle()
    await app.state.login_throttle.prune()
    app.state.accounts = accounts
    app.state.strategy_runner = runner

    # Accounts without credentials or today's login stay disconnected (not an error). Order
    # books are checked against our records below, once running strategies own their open
    # orders again.
    await accounts.start_all(resync=False)
    try:
        await runner.sync(deployments.DEPLOYMENTS)  # strategies are defined in code only
        await runner.resume()  # ones that were running before the restart
        await accounts.resync_all()  # fills / cancels that happened while we were down
    except BaseException:
        # Startup failed after brokers connected: close their sockets, or their threads
        # keep the process alive in a state that serves nothing.
        await accounts.stop_all()
        await db.close()
        raise
    yield
    await runner.shutdown()
    await accounts.stop_all()
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
api.include_router(auth_api_router)  # /api/auth/login, logout, me, password, signup, tokens
api.include_router(accounts_router)  # /api/accounts: your broker accounts
api.include_router(broker_login_router)  # /api/accounts/{id}/broker-login, /api/auth/*/callback
api.include_router(tickers_router)  # /api/accounts/{id}/ws/tickers, /api/accounts/{id}/market/...
api.include_router(orders_router)  # /api/accounts/{id}/ws/orders, .../orders, positions, funds
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


async def _database_ok() -> bool:
    try:
        async with db.session() as s:
            await s.execute(text("select 1"))
        return True
    except Exception:
        return False


@app.get("/health")
async def health(response: Response):
    """For monitoring. Public, so counts only: nothing about any user or account.
    status "ok"; "degraded" when a connected account's stream or resync reports a problem;
    "failing" with HTTP 503 when the database is unreachable or an account's trade ledger has
    updates it couldn't save. Per-account detail: GET /api/accounts/{id} (owner only)."""
    accounts = app.state.accounts.summary()
    database = await _database_ok()
    resync_errors = sum(bool(r.service.last_resync_error) for r in app.state.accounts if r.service.connected)
    if not database or accounts["ledger_unhealthy"]:
        status_ = "failing"
        response.status_code = 503
    elif accounts["stream_errors"] or resync_errors:
        status_ = "degraded"
    else:
        status_ = "ok"
    return {
        "status": status_,
        "database": database,
        "accounts": accounts | {"resync_errors": resync_errors},
        "strategies": app.state.strategy_runner.health(),
    }


@app.get("/", include_in_schema=False)
async def index(request: Request):
    if not await session_user(request):
        return login_redirect(request)
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})
