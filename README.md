# AutoTrade

FastAPI trading backend, split into three apps that share one broker connection:

| app | what it does | endpoints |
|---|---|---|
| **auth** | app sign-in (JSON + HTML page), API token, daily broker login, broker status | `/api/auth/...` |
| **tickers** | live prices, quotes, candles | `WS /api/ws/tickers`, `/api/market/...` |
| **orders** | place / cancel orders, order updates, history, positions, funds | `WS /api/ws/orders`, `/api/orders/...`, `/api/account/...` |
| **trades** | open / closed round-trip trades with P&L | `/api/trades/...` |
| **strategies** | run the strategies defined in code on symbols/timeframes chosen in the app | `/api/strategies/...` |

Brokers (pick one with `AUTOTRADE_BROKER`):

| broker | orders | live data | symbol format |
|---|---|---|---|
| `paper` (default) | simulated fills | simulated prices | `NSE:SBIN-EQ` |
| `fyers` | Fyers API v3 REST | Fyers data + order sockets | `NSE:SBIN-EQ` |
| `zerodha` | Kite Connect REST | Kite WebSocket (ticks + order updates) | `NSE:SBIN` |

## Setup

```bash
uv venv .venv --python 3.11        # or: python3 -m venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt  # or: pip install -r requirements.txt
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # -> AUTOTRADE_SESSION_SECRET
python -m app.auth.users create admin   # creates autotrade.db and your first user
```

## Run

```bash
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000, sign in, and use the built-in test client.

## Database

SQLite at `./autotrade.db` for now (`AUTOTRADE_DATABASE_URL`). Moving to Postgres later is a
URL change (`postgresql+asyncpg://...`, plus `pip install asyncpg`).

### Migrations (Alembic)

The schema lives in `migrations/versions/`. **The app applies pending migrations at startup**,
so normally there is nothing to run by hand. To change the schema:

1. Edit the model (`app/*/models.py`).
2. Generate a migration and review it (autogenerate misses some things, e.g. renames):
   ```bash
   .venv/bin/alembic revision --autogenerate -m "add stop_loss to positions"
   ```
3. Restart the app (with `--reload`, saving the file does it). It upgrades the database.

Order matters with `--reload`: if the app restarts after step 1 but before step 2, it logs a
warning that the models don't match the database ("Write a migration ..."). Write the
migration and it picks it up on the next reload.

**Hand-written migrations: stop the server first** (or write the file somewhere else and move
it in when done). `alembic revision -m ...` creates an *empty* migration; a running `--reload`
server applies it at once and records the database as upgraded, so your real migration would
then be skipped. If that happens: `sqlite3 autotrade.db "update alembic_version set
version_num='<previous revision>'"` and restart.

Other commands: `alembic current` (version of your database), `alembic history`,
`alembic check` (do the models match the migrations?), `alembic downgrade -1` (undo the last
one). They all use `AUTOTRADE_DATABASE_URL`. SQLite migrations use batch mode, so column
changes rebuild the table instead of failing. Back up `autotrade.db` before a risky migration:
`sqlite3 autotrade.db ".backup autotrade.backup.db"`.

A database created before migrations existed is adopted automatically on first start (marked
as being at the baseline revision).

| table | holds |
|---|---|
| `users` | app logins (scrypt password hashes), active flag, last login |
| `broker_tokens` | today's Fyers / Zerodha access token |
| `login_attempts` | every sign-in attempt: drives the lockout, kept 90 days as an audit log |
| `orders` | latest state of every order from any broker; read via `GET /api/orders/history` |
| `order_events` | every state change of every order (placed, part-filled, filled, cancelled ...) |
| `fills` | one row per execution: order, quantity, price, time |
| `positions` | round-trip trades: LONG/SHORT, OPEN/CLOSED, entry/exit averages, realized P&L |
| `position_fills` | links fills to the positions they opened, added to, or closed |

### How trades are tracked

Every order update goes through `app/trades/ledger.py` in one transaction. Brokers report an
order's cumulative filled quantity and average price; the difference from the previous update
is a new fill (so partial fills work, and a repeated update adds nothing). Each fill is applied
to the open position for that broker + symbol:

- no open position → opens one (BUY = LONG, SELL = SHORT)
- same side → adds to it (average entry price is re-weighted)
- opposite side → reduces it and books realized P&L; at zero it is CLOSED. Any extra quantity
  opens a new position the other way (e.g. SELL 15 while LONG 10 → close, then SHORT 5)

`GET /api/trades/open` includes `unrealized_pnl` for symbols the broker is streaming prices for
(paper: all; Fyers/Zerodha: symbols someone is subscribed to on `/api/ws/tickers`). Without a recent
price it is `null` and counted in `totals.unpriced`. `GET /api/trades/closed` takes
`broker`, `symbol`, `since`, `until` (ISO 8601 close time) and `limit`; its totals (count,
realized P&L, wins, losses, win rate) cover every match, not just the page. P&L is before
brokerage, charges and taxes. Only orders this app sees are tracked; trades placed elsewhere
before the app connects (or an existing broker position) are not imported.

The file is created with owner-only permissions (it holds password hashes and broker tokens).
Paper-broker cash and the simulated broker positions (`/api/account/positions`) are still in memory
and reset on restart; the `positions` table above keeps its trade history.

Manage users:

```bash
python -m app.auth.users create <username>
python -m app.auth.users set-password <username>   # also signs them out everywhere
python -m app.auth.users disable <username>        # or: enable
python -m app.auth.users list
```

## Authentication

- **App login.** Everything except `/login` and `/health` needs a signed-in session. Users
  live in the `users` table (see above). The session is a signed, HttpOnly, SameSite=Lax cookie
  that lasts `AUTOTRADE_SESSION_MAX_AGE` (12 h). Five failed logins from one IP lock that IP out
  for 5 minutes, even across restarts. Changing a password or disabling a user signs out their
  existing sessions. WebSockets use the same cookie and reject pages from other
  origins. Behind HTTPS, set `AUTOTRADE_SECURE_COOKIES=true`.
- **API token (optional)** for scripts and bots: set `AUTOTRADE_API_TOKEN`, then send
  `Authorization: Bearer ...` to REST endpoints or `?token=...` on WebSockets. Empty = disabled.
- Interactive API docs: `/api/docs` (and `/api/openapi.json`), for signed-in users only.
  Turn off with `AUTOTRADE_ENABLE_DOCS=false`.
- **Broker login** (real money). Fyers and Zerodha tokens expire daily, so log in once each
  trading day. Sign in to the app, then open `http://127.0.0.1:8000/api/auth/<broker>/login`
  (or use the login buttons on the test page). After you log in, the token is saved to
  `broker_tokens` table for the day and the broker connects. Until then, order and ticker
  requests reply with a "not logged in" error.

  | | Fyers | Zerodha |
  |---|---|---|
  | create app | https://myapi.fyers.in | https://developers.kite.trade |
  | redirect URL to register | `http://127.0.0.1:8000/auth/fyers/callback` | `http://127.0.0.1:8000/auth/zerodha/callback` |
  | `.env` | `AUTOTRADE_BROKER=fyers`, `AUTOTRADE_FYERS_CLIENT_ID`, `AUTOTRADE_FYERS_SECRET_KEY` | `AUTOTRADE_BROKER=zerodha`, `AUTOTRADE_ZERODHA_API_KEY`, `AUTOTRADE_ZERODHA_API_SECRET` |
  | product setting | `AUTOTRADE_FYERS_PRODUCT_TYPE` (INTRADAY, CNC, ...) | `AUTOTRADE_ZERODHA_PRODUCT` (MIS, CNC, NRML) |

## Strategies

**Code** (the admin) defines each strategy's logic, parameters, size, limits and mode.
**The app** chooses where it runs: which symbol and timeframe, on as many symbols at once as
you like.

- A **strategy** is a Python class that reacts to price bars (or ticks) and calls `buy()`,
  `sell()` or `exit()` (`app/strategies/builtin/`). It can limit which timeframes it allows.
- A **deployment** is a strategy plus its parameters, size, limits and mode, listed in
  `app/strategies/deployments.py`:

```python
DEPLOYMENTS = [
    Deployment(
        key="nifty-1m-breakout",            # stable id; keep it the same across edits
        strategy=IntradayBreakout,
        symbol="NSE:NIFTY26NOVFUT",         # default for the first instance only
        timeframe="1m",                     # default for the first instance only
        params=IntradayBreakout.Params(...),
        quantity=65, max_position=65, max_orders_per_day=20,
        mode="signal",                      # "live" places real orders
    ),
]
```

- An **instance** is one run of a deployment on a symbol and timeframe
  (`1m, 3m, 5m, 10m, 15m, 30m, 60m`). The first one is created automatically from the
  deployment's defaults. In the app (Strategies section, or the API) you can **add** more
  instances, **change** an instance's symbol/timeframe while it's stopped, and **remove** it
  (archived: its signal history is kept, and adding the same symbol + timeframe again brings it
  back). A deployment can't run twice on the same symbol + timeframe. When the broker is
  connected, a new symbol is checked with it first. Each instance has its own position and its
  own copy of the limits, so two instances can each hold `max_position`.

On every server start, instances are synced from code: changed parameters/size/limits/mode
apply to every instance of that deployment, and **instances that were running come back
stopped** after such a change, so an edit (e.g. switching to live) never starts trading
unattended. Unchanged running ones resume. Removing a deployment from code marks its instances
`removed`. Every trade a strategy asks for is recorded in `strategy_signals` with its outcome.

**Safety, in order:**

1. **Mode.** New instances are `signal` mode: nothing is sent to the broker; the signal is
   recorded and the instance's position moves as if it filled, so a dry run behaves like the
   real thing. `live` mode places market orders.
2. **Global switch.** Live mode also needs `AUTOTRADE_STRATEGIES_LIVE=true` in `.env`
   (off by default); otherwise live signals are recorded as `blocked`.
3. **Risk limits per instance:** `max_order_qty`, `max_position` (absolute), `max_orders_per_day`
   (IST), and one open order at a time. Orders that only shrink a position are never blocked
   by these limits, so a strategy can always exit.
4. **Errors.** A strategy that raises is stopped with status `error` and the message in
   `last_error`.
5. **Kill switch.** `POST /api/strategies/stop-all` (or **Stop all** on the test page). It stops
   strategies; it does not cancel orders already at the broker.

After a restart, indicators start empty (no historical warm-up yet) and each strategy's
position is rebuilt from its own signals/orders.

**Built-in strategies** (`app/strategies/builtin/`):

| strategy | idea |
|---|---|
| `sma_crossover` | fast/slow moving average cross on bar closes; long-only or with shorts |
| `intraday_breakout` | port of the TradingView "NIFTY 1-minute breakout": from 10:30, trade the first break of the last 5 one-minute candles' high/low; stop 0.25x and trail 0.40x the 14-day average daily range; skip if the 5-candle range is wider than 0.4x it; max 8 entries/day; entries until 14:45; square-off 15:15 (IST). Deployed as `nifty-1m-breakout` on the NIFTY future, 1 lot (65), signal mode. Roll the symbol to the next contract before monthly expiry. |

Strategies can read the broker's candle history with `await self.ctx.history("D" or "1", days=...)`
(Fyers, Zerodha; the paper broker makes up candles for its own symbols).

**Writing a strategy:** add a module under `app/strategies/builtin/` with a `Strategy`
subclass, then add a `Deployment` for it in `deployments.py`. See `app/strategies/base.py`
for the API and `builtin/sma_crossover.py` for an example:

```python
class MyStrategy(Strategy):
    name = "my_strategy"
    description = "Shown in the UI"

    class Params(BaseModel):          # validated when an instance is created
        lookback: int = Field(20, ge=2)
        bar_seconds: int = 60

    async def on_bar(self, bar: Bar) -> None:   # or on_tick(self, tick)
        if <entry> and self.ctx.position == 0:
            await self.ctx.buy(reason="why")     # size = instance quantity
        elif <exit> and self.ctx.position > 0:
            await self.ctx.exit(reason="why")
```

`self.ctx` also has `symbol`, `position`, `last_price`, `quantity` and `log()`. Strategy
orders also flow into `orders`, `fills` and `positions` like manual ones (the `positions`
table is per broker + symbol, so it mixes manual and strategy trades on the same symbol).

Signals and status changes are pushed to `/api/ws/orders` clients as `strategy_signal` and
`strategy_status` messages.

## API (for a separate frontend)

Everything a frontend needs is under **`/api`**: REST endpoints and the two WebSockets.
Browse and try them at **`/api/docs`** (sign in first); the OpenAPI schema at
`/api/openapi.json` can generate a typed client, e.g.
`npx openapi-typescript http://localhost:8000/api/openapi.json -o src/api.d.ts` (needs a
session cookie or `Authorization: Bearer <API token>` header).

Errors are JSON `{"detail": ...}` with: **401** not signed in, **403** wrong current password,
**404** not found, **409** conflict (e.g. strategy running), **422** invalid request or refused by
the broker (bad symbol, can't cancel), **429** locked out (`Retry-After`), **502** broker
API error, **503** broker not connected (log in to the broker).

| | endpoint | |
|---|---|---|
| **Auth** | `POST /api/auth/login` `{username, password}` | sets the session cookie; 401 / 429 |
| | `POST /api/auth/logout` | |
| | `GET /api/auth/me` | current user or 401 (call on app load) |
| | `POST /api/auth/password` `{current_password, new_password}` | other sessions are signed out |
| **Broker** | `GET /api/auth/status` | active broker, connected, logged in today |
| | `GET /api/auth/{fyers,zerodha}/login?redirect=false` | `{url}`: send the browser there |
| | `POST /api/auth/{broker}/logout` | |
| **Market** | `GET /api/market/quotes?symbols=A,B` | `{symbol: last price}` |
| | `GET /api/market/candles?symbol=&resolution=1\|D&start=&end=` | OHLCV for charts |
| | `WS /api/ws/tickers` | `subscribe` / `unsubscribe`, pushes `tick` |
| **Orders** | `GET /api/orders` | today's order book (broker) |
| | `POST /api/orders` `{symbol, side, quantity, order_type, limit_price}` | 201 with the order |
| | `DELETE /api/orders/{id}` | cancel |
| | `GET /api/orders/history?broker=&symbol=&limit=` | from the database |
| | `GET /api/orders/{id}/events` | status changes of one order |
| | `WS /api/ws/orders` | same actions, pushes `order_update`, `strategy_signal`, `strategy_status` |
| **Account** | `GET /api/account/positions`, `GET /api/account/funds` | from the broker |
| **Trades** | `GET /api/trades/open`, `/api/trades/closed?broker=&symbol=&since=&until=&limit=`, `/api/trades/{id}` | round trips + P&L |
| **Strategies** | `GET /api/strategies` | instances, plus the deployments code defines |
| | `POST /api/strategies` `{deployment_key, symbol, timeframe}` | add an instance |
| | `PATCH /api/strategies/{id}` `{symbol?, timeframe?}` | while stopped |
| | `DELETE /api/strategies/{id}` | archive (history kept) |
| | `POST /api/strategies/{id}/start`, `/stop`, `POST /api/strategies/stop-all` | |
| | `GET /api/strategies/{id}/signals?limit=` | |

Outside `/api`: `GET /health` (public), the HTML `/login` page and `/` test page, and the
broker callbacks `/auth/{fyers,zerodha}/callback` (the URLs registered with the brokers).

### Connecting a React app

**Development (recommended): proxy through the dev server**, so the browser sees one origin
and nothing else needs configuring. Vite `vite.config.ts`:

```ts
export default defineConfig({
  server: {
    proxy: {
      "/api": { target: "http://localhost:8000", ws: true },   // REST + WebSockets
      "/auth": "http://localhost:8000",                         // broker callbacks
    },
  },
});
```

Then call `fetch("/api/...")` and `new WebSocket(`ws://${location.host}/api/ws/orders`)`.

**Separate origins** (frontend on its own host or port without a proxy): list it in `.env`

```
AUTOTRADE_CORS_ORIGINS=["http://localhost:5173"]
AUTOTRADE_FRONTEND_URL=http://localhost:5173/settings   # where broker logins return to
```

and call the API with cookies: `fetch("http://localhost:8000/api/...", {credentials: "include"})`.
The session cookie is `SameSite=Lax`, so frontend and API must be on the same site (e.g.
`app.example.com` + `api.example.com`, or `localhost` on two ports). In production use HTTPS
and `AUTOTRADE_SECURE_COOKIES=true`.

**Broker login from the frontend:** `GET /api/auth/fyers/login?redirect=false` → set
`window.location = url`. After the user logs in at Fyers, the browser comes back to
`AUTOTRADE_FRONTEND_URL` with `?broker_login=fyers` (or `?broker_login_error=...`).

## WebSocket protocol

Every message is JSON with an `action` and an optional `request_id` (echoed back).
Both sockets accept `ping`. Server replies: `welcome`, `ack`, `error`, `pong`.

**`/api/ws/tickers`**

| action | fields |
|---|---|
| `subscribe` / `unsubscribe` | `symbols: [...]` in the active broker's format (see above) |

Pushes `{"type": "tick", "symbol": ..., "ltp": ...}`.

**`/api/ws/orders`**

| action | fields |
|---|---|
| `place_order` | `symbol`, `side` (BUY/SELL), `quantity`, `order_type` (MARKET/LIMIT), `limit_price` |
| `cancel_order` | `order_id` |
| `get_orders`, `get_positions`, `get_funds` | — |

Pushes `{"type": "order_update", "order": {...}}` to every connected client.

## Layout

```
app/
  main.py                 wires the apps, broker and connection managers together
  core/
    config.py             settings from env / .env
    db.py                 SQLAlchemy async engine/session; runs migrations at startup
    broker_service.py     owns the live broker; restarted after a Fyers login
    connections.py        tracks sockets per app and what each one follows
    ws.py                 shared WebSocket loop (auth, parsing, ack/error replies)
    schemas.py            messages shared by all apps (ping)
  brokers/
    base.py               Broker interface — implement this for another broker
    paper.py              simulated broker
    fyers.py              Fyers API v3 broker
    zerodha.py            Zerodha Kite Connect broker
  auth/
    pages.py              /login page, /logout
    models.py             users, broker_tokens, login_attempts tables
    users.py              user accounts + `python -m app.auth.users` CLI
    passwords.py          scrypt hashing
    throttle.py           failed-login lockout (backed by login_attempts)
    dependencies.py       who's calling: session cookie or API token
    router.py             broker logins and status
    token_store.py        today's broker access tokens
  tickers/                router.py, schemas.py
  orders/                 router.py, schemas.py, models.py (orders, order_events), history.py
  trades/                 ledger.py (fills -> positions), models.py, router.py
  strategies/             deployments.py (what runs: edit this), deployment.py, base.py
                          (Strategy API), runner.py (sync, risk checks, orders), models.py,
                          router.py, builtin/ (sma_crossover.py)
  static/                 test client page
migrations/               Alembic env.py and versions/ (the schema history)
alembic.ini
tests/                    one file per app, plus test_fyers.py and test_zerodha.py
```

## Tests

```bash
pytest
```
