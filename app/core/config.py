from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AUTOTRADE_")

    app_name: str = "AutoTrade"

    # SQLite for now; any SQLAlchemy async URL works later (e.g. postgresql+asyncpg://...).
    database_url: str = "sqlite+aiosqlite:///./autotrade.db"

    # App login (the /login page). Users live in the database:
    #   python -m app.auth.users create <username>
    # Signs session cookies. If empty, a random one is made at startup (logins end on restart).
    session_secret: str = ""
    session_max_age: int = 12 * 60 * 60  # seconds a login lasts
    # Set true when serving over HTTPS so the session cookie is never sent over plain HTTP.
    secure_cookies: bool = False
    # Optional token for scripts/bots (Authorization: Bearer ... or ?token=...). Empty = disabled.
    api_token: str = ""
    # Global switch for strategies in "live" mode to place real orders. Off: they are blocked.
    strategies_live_enabled: bool = False
    # Interactive API docs at /api/docs (and /api/openapi.json), for signed-in users only.
    enable_docs: bool = True
    # Browser origins of a separately hosted frontend, e.g. ["http://localhost:5173"]
    # (JSON list in .env). Allowed for CORS with credentials and for WebSocket connections.
    cors_origins: list[str] = []
    # Where to send the browser after a broker login, e.g. "http://localhost:5173/settings".
    # Empty: show a small "login successful" page instead.
    frontend_url: str = ""
    # "paper" (simulated), "fyers" or "zerodha" (REAL orders on your broker account).
    broker: str = "paper"
    paper_starting_cash: float = 1_000_000.0
    # Seconds between simulated price ticks from the paper broker.
    tick_interval: float = 1.0

    # Only one server process may run strategies and place orders: it holds this lock file
    # (flock) while running, and a second one refuses to start. Empty = next to the SQLite file,
    # or ./autotrade.lock for other databases.
    lock_file: str = ""
    # Order placements + cancels sent to the broker per second (Fyers and Kite allow 10).
    broker_orders_per_second: float = 8.0
    # Seconds between checks of the broker's order book against our records (0 = only on
    # (re)connect). Catches order updates a socket never delivered.
    resync_interval: float = 60.0
    # Ticks queued per running strategy (and messages per WebSocket client) before the oldest
    # ticks are dropped (the client is disconnected) so one slow consumer can't stall the rest.
    strategy_queue_size: int = 1000
    ws_queue_size: int = 1000

    # Fyers API v3. Log in daily at /auth/fyers/login; the token is saved in the database.
    fyers_client_id: str = ""  # app id, e.g. "XB12345-100"
    fyers_secret_key: str = ""  # app secret, needed for the login flow
    # Must match the redirect URL registered on your Fyers app.
    fyers_redirect_uri: str = "http://127.0.0.1:8000/auth/fyers/callback"
    # Optional: a token generated elsewhere; used when there is no saved login for today.
    fyers_access_token: str = ""  # raw token or "CLIENT_ID:token"
    fyers_product_type: str = "INTRADAY"  # CNC, INTRADAY, MARGIN, MTF
    fyers_log_dir: str = "logs"

    # Zerodha Kite Connect. Log in daily at /auth/zerodha/login. Register
    # http://127.0.0.1:8000/auth/zerodha/callback as the redirect URL on the Kite console.
    zerodha_api_key: str = ""
    zerodha_api_secret: str = ""
    zerodha_product: str = "MIS"  # MIS (intraday), CNC (delivery), NRML
    # Optional: a token generated elsewhere; used when there is no saved login for today.
    zerodha_access_token: str = ""


settings = Settings()
