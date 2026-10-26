import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "AUTOTRADE_"


class Settings(BaseSettings):
    # Unknown AUTOTRADE_ names are ignored (see obsolete_settings), so an old .env still starts.
    model_config = SettingsConfigDict(env_file=".env", env_prefix=ENV_PREFIX, extra="ignore")

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
    # Anyone may create an account with POST /api/auth/signup. Off: users are created with
    # `python -m app.auth.users create`.
    allow_signup: bool = False
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

    # ---------- broker accounts ----------
    # Every user adds their own broker accounts (paper, Fyers, Zerodha) in the app; their
    # API keys/secrets and daily access tokens are stored encrypted (AES-256-GCM) with this key.
    # Generate one with: python -m app.accounts.vault generate-key
    # Keep it out of the database and its backups: whoever has both can decrypt.
    credentials_key: str = ""
    # Previous keys, still accepted for reading while `python -m app.accounts.vault rotate`
    # re-encrypts everything with credentials_key (JSON list in .env).
    credentials_old_keys: list[str] = []
    max_accounts_per_user: int = 10
    # Starting cash of a new paper account (each can set its own).
    paper_starting_cash: float = 1_000_000.0
    # Seconds between simulated price ticks from paper accounts.
    tick_interval: float = 1.0
    # Broker login redirects: register these on your Fyers app / Kite developer console. One
    # URL for every account: the login's `state` says which account it is for.
    fyers_redirect_uri: str = "http://127.0.0.1:8000/auth/fyers/callback"
    fyers_log_dir: str = "logs"

    # ---------- reliability ----------
    # Only one server process may run strategies and place orders: it holds this lock file
    # (flock) while running, and a second one refuses to start. Empty = next to the SQLite file,
    # or ./autotrade.lock for other databases.
    lock_file: str = ""
    # Order placements + cancels sent per account per second (Fyers and Kite allow 10).
    broker_orders_per_second: float = 8.0
    # Seconds between checks of each account's order book against our records (0 = only on
    # (re)connect). Catches order updates a socket never delivered.
    resync_interval: float = 60.0
    # Ticks queued per running strategy (and messages per WebSocket client) before the oldest
    # ticks are dropped (the client is disconnected) so one slow consumer can't stall the rest.
    strategy_queue_size: int = 1000
    ws_queue_size: int = 1000
    # Strategies order options with limit orders, never market ones: brokers cap or refuse
    # market orders on options (Kite needs market protection, Fyers caps them and may refuse
    # them on later expiries). Buys go at the option's last price + this %, sells at - this %,
    # on the 0.05 tick, so they fill straight away while the price can't run further than that.
    option_limit_buffer_pct: float = 3.0
    # Seconds a strategy's option order may stay unfilled before it is cancelled (0 = never).
    # An open order blocks the strategy's next one; it asks again on a later tick if it still wants to.
    option_order_timeout: float = 30.0


def obsolete_settings(env_file: str = ".env") -> list[str]:
    """AUTOTRADE_ names set in the environment or .env that are no longer settings (e.g. the
    old single-broker AUTOTRADE_BROKER / AUTOTRADE_FYERS_SECRET_KEY). Names only, never values."""
    names = {k for k in os.environ if k.startswith(ENV_PREFIX)}
    path = Path(env_file)
    if path.exists():
        for line in path.read_text().splitlines():
            name = line.split("=", 1)[0].strip().removeprefix("export ").strip()
            if name.startswith(ENV_PREFIX) and "=" in line:
                names.add(name)
    known = {ENV_PREFIX + f.upper() for f in Settings.model_fields}
    return sorted(names - known)


settings = Settings()
