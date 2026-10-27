# AutoTrade

FastAPI trading platform for many users, each with their own broker accounts. Every user
signs in, adds one or more broker accounts (paper, Fyers, Zerodha) by pasting that broker app's
credentials, logs in to the broker each day, and trades or runs strategies on those accounts.
Credentials and access tokens are stored encrypted; users never see each other's accounts,
orders, trades or strategies.

| app | what it does | endpoints |
|---|---|---|
| **auth** | app sign-in (JSON + HTML page), optional sign-up, personal API tokens | `/api/auth/...` |
| **accounts** | your broker accounts: add (credentials), edit, delete, daily broker login | `/api/accounts/...` |
| **tickers** | live prices, quotes, candles, per account | `WS /api/accounts/{id}/ws/tickers`, `/api/accounts/{id}/market/...` |
| **orders** | place / cancel orders, order updates, history, positions, funds, per account | `WS /api/accounts/{id}/ws/orders`, `/api/accounts/{id}/orders/...` |
| **trades** | open / closed round-trip trades with P&L, across your accounts | `/api/trades/...` |
| **strategies** | run the strategies defined in code on your accounts, symbols and timeframes | `/api/strategies/...` |

Brokers (each account is one of these):

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
python -m app.accounts.vault generate-key                        # -> AUTOTRADE_CREDENTIALS_KEY
python -m app.auth.users create admin   # creates autotrade.db and your first user
```

Then sign in at http://127.0.0.1:8000 and add a broker account (start with a paper one).
Upgrading from the single-broker version: your `AUTOTRADE_BROKER` / `AUTOTRADE_FYERS_*` /
`AUTOTRADE_ZERODHA_*` / `AUTOTRADE_API_TOKEN` settings are ignored now (the server logs their
names). Existing orders and trades move to an "Imported (...)" account owned by the first user:
paste that broker app's credentials into it, then remove them from `.env`.

## Run

```bash
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000, sign in, and use the built-in test client.

Run **one** server process (no `--workers 2`): strategies, the broker connections and order
de-duplication live in it, so a second process would place every order twice. The server
holds a lock file while it runs (next to the SQLite file, or `AUTOTRADE_LOCK_FILE`) and a
second one refuses to start.

## Broker accounts and credentials

Each user adds their own accounts (`POST /api/accounts`, or **Add a broker account** on the test
page), up to `AUTOTRADE_MAX_ACCOUNTS_PER_USER` (10). Each has a label unique to that user.

| broker | credentials (secret, write-only) | options | daily login |
|---|---|---|---|
| `paper` | none | `starting_cash` | no |
| `fyers` | `client_id`, `secret_key` of *your* Fyers app (https://myapi.fyers.in) | `product_type` (INTRADAY, CNC, MARGIN, MTF) | yes |
| `zerodha` | `api_key`, `api_secret` of *your* Kite Connect app (https://developers.kite.trade) | `product` (MIS, CNC, NRML) | yes |

- **Encrypted at rest.** Credentials and each day's access token are encrypted with AES-256-GCM
  using `AUTOTRADE_CREDENTIALS_KEY`, which lives in the environment, never in the database. Each
  value is bound to its account and field, so a ciphertext copied onto another row won't
  decrypt. Without the key, only paper accounts can be added. Keep the key out of database
  backups: whoever has both can decrypt.
- **Never shown back.** Responses say which credential fields are set; the client id / API key
  is masked (`XB12…00`) so you can tell accounts apart; secrets aren't shown at all. To change
  them, `PATCH` the fields you're replacing (that also drops today's broker login).
- **Key rotation:** move the current key into `AUTOTRADE_CREDENTIALS_OLD_KEYS`, set a new
  `AUTOTRADE_CREDENTIALS_KEY`, run `python -m app.accounts.vault rotate`, then drop the old key.
- **Daily broker login** (Fyers and Zerodha tokens expire daily): open
  `/api/accounts/{id}/broker-login` (or **Broker login** on the test page). It uses that
  account's own app; afterwards the token is stored encrypted for the day and the account
  connects. Or paste a token you generated elsewhere: `POST /api/accounts/{id}/access-token`.
  Register **one redirect URL per broker for all accounts** on your broker app:
  `http://<server>/auth/fyers/callback` (also `AUTOTRADE_FYERS_REDIRECT_URI`) or
  `http://<server>/auth/zerodha/callback`. The login's `state` says which account it was for.
- **Deleting** an account wipes its credentials and token and disconnects it; its order and
  trade history stays yours. Stop its strategies first (they're archived).
- **Isolation.** Another user's account id gets the same 404 as one that doesn't exist, on
  every route and socket. Each account has its own broker connection, rate limit, ledger,
  resync and WebSocket channels: one account's broker or ledger failing never affects another.

## Reliability

- **Reconciliation.** After every broker (re)connect, at startup, and every
  `AUTOTRADE_RESYNC_INTERVAL` seconds (60), the broker's order book is compared with the
  `orders` table and every order whose state we missed (socket down, dropped update) is
  replayed to the ledger, running strategies and clients. A strategy never stays stuck waiting
  on a fill it was never told about.
- **Slow consumers are isolated.** Each running strategy has its own tick queue and task, and
  each WebSocket client its own send queue, so a strategy placing an order or a slow browser tab
  never delays the broker feed or anyone else. A strategy that falls
  `AUTOTRADE_STRATEGY_QUEUE_SIZE` ticks behind drops its oldest ticks; a client that falls
  `AUTOTRADE_WS_QUEUE_SIZE` messages behind is disconnected (code 1013) and should reconnect.
  Fyers' SDK callbacks (on its own threads) are delivered in order, one at a time, like the
  other brokers'.
- **Ledger failures are never dropped.** An order update the database can't save is kept and
  retried (latest state per order) until it is. Meanwhile `/health` reports `failing`, order
  placement double-checks the broker before sending, and live strategies are paused.
- **Broker rate limit.** Placements and cancels are spaced to
  `AUTOTRADE_BROKER_ORDERS_PER_SECOND` (8; Fyers and Kite allow 10), per account.
- **Exact money.** Prices and P&L are stored as exact decimals (whole 1/10000ths), so totals
  don't drift the way float sums do.
- **`GET /health`** (public, so counts only: nothing about any user or account): `status` is
  `ok`, `degraded` (a connected account's stream or resync reports a problem) or `failing` with
  HTTP 503 (database unreachable or an account's ledger has unsaved updates), with account and
  strategy counts. Each account's detail (connection, tick counts, last resync, ledger, sockets)
  is in `GET /api/accounts/{id}` for its owner.

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
| `api_tokens` | personal API tokens (SHA-256 hashes only) |
| `broker_accounts` | each user's broker accounts: encrypted credentials and today's encrypted access token, options |
| `login_attempts` | every sign-in attempt: drives the lockout, kept 90 days as an audit log |
| `orders` | latest state of every order, per account; read via `GET /api/accounts/{id}/orders/history` |
| `order_events` | every state change of every order (placed, part-filled, filled, cancelled ...) |
| `fills` | one row per execution: order, quantity, price, time |
| `positions` | round-trip trades: LONG/SHORT, OPEN/CLOSED, entry/exit averages, realized P&L |
| `position_fills` | links fills to the positions they opened, added to, or closed |

### How trades are tracked

Every order update goes through `app/trades/ledger.py` in one transaction. Brokers report an
order's cumulative filled quantity and average price; the difference from the previous update
is a new fill (so partial fills work, and a repeated update adds nothing). Each fill is applied
to the open position for that account + symbol:

- no open position → opens one (BUY = LONG, SELL = SHORT)
- same side → adds to it (average entry price is re-weighted)
- opposite side → reduces it and books realized P&L; at zero it is CLOSED. Any extra quantity
  opens a new position the other way (e.g. SELL 15 while LONG 10 → close, then SHORT 5)

`GET /api/trades/open` (all your accounts, or `?account_id=`) includes `unrealized_pnl` for
symbols the account's broker is streaming prices for (paper: all; Fyers/Zerodha: symbols someone
is subscribed to on that account's tickers socket). Without a recent price it is `null` and
counted in `totals.unpriced`. `GET /api/trades/closed` takes
`account_id`, `symbol`, `since`, `until` (ISO 8601 close time) and `limit`; its totals (count,
realized P&L, wins, losses, win rate) cover every match, not just the page. P&L is before
brokerage, charges and taxes. Only orders this app sees are tracked; trades placed elsewhere
before the app connects (or an existing broker position) are not imported.

The file is created with owner-only permissions (it holds password hashes and encrypted broker
credentials). Paper-broker cash and the simulated broker positions (`/api/accounts/{id}/positions`) are still in memory
and reset on restart; the `positions` table above keeps its trade history.

Manage users:

```bash
python -m app.auth.users create <username>
python -m app.auth.users set-password <username>   # also signs them out everywhere
python -m app.auth.users disable <username>        # or: enable
python -m app.auth.users list
python -m app.auth.users token <username> [name]   # a personal API token (shown once)
```

## Authentication

- **App login.** Everything except `/login` and `/health` needs a signed-in session. Users
  live in the `users` table (see above). The session is a signed, HttpOnly, SameSite=Lax cookie
  that lasts `AUTOTRADE_SESSION_MAX_AGE` (12 h). Five failed logins from one IP lock that IP out
  for 5 minutes, even across restarts. Changing a password or disabling a user signs out their
  existing sessions. WebSockets use the same cookie and reject pages from other
  origins. Behind HTTPS, set `AUTOTRADE_SECURE_COOKIES=true`.
- **Sign-up.** With `AUTOTRADE_ALLOW_SIGNUP=true`, anyone can create a user with
  `POST /api/auth/signup` (same lockout as login). Off by default: users are created with the CLI.
- **Personal API tokens** for scripts and bots: `POST /api/auth/tokens {name}` (shown once,
  stored hashed), `GET /api/auth/tokens`, `DELETE /api/auth/tokens/{id}`, or the CLI above. A
  token acts as its user. Send `Authorization: Bearer ...` to REST endpoints or `?token=...` on
  WebSockets (browsers can't set WebSocket headers). REST does not accept `?token=`: URLs end
  up in logs. A disabled user's tokens stop working.
- Interactive API docs: `/api/docs` (and `/api/openapi.json`), for signed-in users only.
  Turn off with `AUTOTRADE_ENABLE_DOCS=false`.
- **Broker login** per account: see [Broker accounts](#broker-accounts-and-credentials).

## Strategies

**Code** (the platform admin) defines each strategy's logic, parameters, size and limits:
the catalog. **Each user** chooses where it runs: which of their broker accounts, symbol and
timeframe, on as many at once as they like. Users only see and control their own instances.

- A **strategy** is a Python class that reacts to price bars (or ticks) and calls `buy()`,
  `sell()` or `exit()` (`app/strategies/builtin/`). It can limit which timeframes it allows.
- A **deployment** is a strategy plus its parameters, size and limits, listed in
  `app/strategies/deployments.py`:

```python
DEPLOYMENTS = [
    Deployment(
        key="nifty-1m-breakout",            # stable id; keep it the same across edits
        strategy=IntradayBreakout,
        symbol="NSE:NIFTY26NOVFUT",         # suggested symbol in the "add" form
        timeframe="1m",                     # suggested timeframe
        params=IntradayBreakout.Params(...),
        quantity=65, max_position=65, max_orders_per_day=20,
    ),
]
```

- An **instance** is one run of a deployment on one broker account, symbol and timeframe
  (`1m, 3m, 5m, 10m, 15m, 30m, 60m`). Code creates none: users **add** instances on their
  accounts (Strategies section, or the API), **change** an instance's symbol/timeframe while it's
  stopped, and **remove** it (archived: its signal history is kept, and adding the same
  account + symbol + timeframe again brings it back). A deployment can't run twice on the same
  account + symbol + timeframe. When the account's broker is connected, a new symbol is checked
  with it first. Its orders go to that account's broker only. Each instance has its own position and its
  own copy of the limits, so two instances can each hold `max_position`.

On every server start, instances are synced from code: changed parameters/size/limits
apply to every instance of that deployment, and **instances that were running come back
stopped** after such a change, so an edit never resumes trading unattended. Unchanged running ones resume. Removing a deployment from code marks its instances
`removed`. Every trade a strategy asks for is recorded in `strategy_signals` with its outcome.

**Safety, in order:**

1. **Real orders only.** A running instance places real orders on its account's broker; there
   is no dry-run mode. To try a strategy without real money, run it on a **paper** account.
   Option contracts are ordered at a limit price from market depth, never at market.
2. **Risk limits per instance:** `max_order_qty`, `max_position` (absolute), `max_orders_per_day`
   (IST), and one open order at a time. Orders that only shrink a position are never blocked
   by these limits, so a strategy can always exit.
3. **Errors.** A strategy that raises is stopped with status `error` and the message in
   `last_error`.
4. **Kill switch.** `POST /api/strategies/stop-all` (or **Stop all** on the test page) stops
   all of *your* strategies; it does not cancel orders already at the broker.

After a restart, indicators start empty (no historical warm-up yet) and each strategy's
position is rebuilt from the orders it placed.

**Built-in strategies** (`app/strategies/builtin/`):

| strategy | idea |
|---|---|
| `sma_crossover` | fast/slow moving average cross on bar closes; long-only or with shorts |
| `intraday_breakout` | port of the TradingView "NIFTY 1-minute breakout": from 10:30, trade the first break of the last 5 one-minute candles' high/low; stop 0.25x and trail 0.40x the 14-day average daily range; skip if the 5-candle range is wider than 0.4x it; max 8 entries/day; entries until 14:45; square-off 15:15 (IST). Deployed as `nifty-1m-breakout` on the NIFTY future, 1 lot (65). Roll the symbol to the next contract before monthly expiry. |

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
table is per account + symbol, so it mixes manual and strategy trades on the same symbol).

Signals and status changes are pushed to the account's orders socket as `strategy_signal` and
`strategy_status` messages.

## API (for a separate frontend)

Everything a frontend needs is under **`/api`**: REST endpoints and each account's two WebSockets.
Browse and try them at **`/api/docs`** (sign in first); the OpenAPI schema at
`/api/openapi.json` can generate a typed client, e.g.
`npx openapi-typescript http://localhost:8000/api/openapi.json -o src/api.d.ts` (needs a
session cookie or `Authorization: Bearer <API token>` header).

Errors are JSON `{"detail": ...}` with: **401** not signed in, **403** wrong current password,
**404** not found (also: not yours), **409** conflict (e.g. strategy running), **422** invalid
request or refused by the broker (bad symbol, can't cancel), **429** locked out (`Retry-After`),
**502** broker API error, **503** account's broker not connected (log in to the broker) or no
credentials key on the server.

| | endpoint | |
|---|---|---|
| **Auth** | `POST /api/auth/login` `{username, password}` | sets the session cookie; 401 / 429 |
| | `POST /api/auth/logout` | |
| | `GET /api/auth/me` | current user or 401 (call on app load) |
| | `POST /api/auth/password` `{current_password, new_password}` | other sessions are signed out |
| | `POST /api/auth/signup` `{username, password}` | when enabled |
| | `GET/POST /api/auth/tokens`, `DELETE /api/auth/tokens/{id}` | personal API tokens |
| **Accounts** | `GET /api/accounts`, `GET /api/accounts/brokers` | yours; what each broker needs |
| | `POST /api/accounts` `{broker, label, credentials?, options?}` | add one (credentials encrypted) |
| | `GET/PATCH/DELETE /api/accounts/{id}` | `connection` holds its live status |
| | `GET /api/accounts/{id}/broker-login?redirect=false` | `{url}`: send the browser there |
| | `POST /api/accounts/{id}/access-token` `{access_token}`, `/broker-logout`, `/reconnect` | |
| **Market** | `GET /api/accounts/{id}/market/quotes?symbols=A,B` | `{symbol: last price}` |
| | `GET /api/accounts/{id}/market/candles?symbol=&resolution=1\|D&start=&end=` | OHLCV for charts |
| | `WS /api/accounts/{id}/ws/tickers` | `subscribe` / `unsubscribe`, pushes `tick` |
| **Orders** | `GET /api/accounts/{id}/orders` | today's order book (broker) |
| | `POST /api/accounts/{id}/orders` `{symbol, side, quantity, order_type, limit_price}` | 201 with the order |
| | `DELETE /api/accounts/{id}/orders/{order_id}` | cancel |
| | `GET /api/accounts/{id}/orders/history?symbol=&limit=` | from the database |
| | `GET /api/accounts/{id}/orders/{order_id}/events` | status changes of one order |
| | `WS /api/accounts/{id}/ws/orders` | same actions, pushes `order_update`, `strategy_signal`, `strategy_status` |
| **Account** | `GET /api/accounts/{id}/positions`, `GET /api/accounts/{id}/funds` | from the broker |
| **Trades** | `GET /api/trades/open`, `/api/trades/closed?account_id=&symbol=&since=&until=&limit=`, `/api/trades/{id}` | round trips + P&L |
| **Strategies** | `GET /api/strategies` | instances, plus the deployments code defines |
| | `GET /api/strategies?account_id=` | one account's only |
| | `POST /api/strategies` `{deployment_key, account_id, symbol, timeframe}` | add an instance |
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

Then call `fetch("/api/...")` and `new WebSocket(`ws://${location.host}/api/accounts/${id}/ws/orders`)`.

**Separate origins** (frontend on its own host or port without a proxy): list it in `.env`

```
AUTOTRADE_CORS_ORIGINS=["http://localhost:5173"]
AUTOTRADE_FRONTEND_URL=http://localhost:5173/settings   # where broker logins return to
```

and call the API with cookies: `fetch("http://localhost:8000/api/...", {credentials: "include"})`.
The session cookie is `SameSite=Lax`, so frontend and API must be on the same site (e.g.
`app.example.com` + `api.example.com`, or `localhost` on two ports). In production use HTTPS
and `AUTOTRADE_SECURE_COOKIES=true`.

**Broker login from the frontend:** `GET /api/accounts/{id}/broker-login?redirect=false` → set
`window.location = url`. After the user logs in at the broker, the browser comes back to
`AUTOTRADE_FRONTEND_URL` with `?broker_login=fyers&account_id={id}` (or `?broker_login_error=...`).

## WebSocket protocol

Each broker account has two sockets; only its owner can open them (session cookie, or
`?token=` with a personal API token). Every message is JSON with an `action` and an optional
`request_id` (echoed back). Both sockets accept `ping`. Server replies: `welcome` (with
`account_id` and `broker`), `ack`, `error`, `pong`.

**`/api/accounts/{id}/ws/tickers`**

| action | fields |
|---|---|
| `subscribe` / `unsubscribe` | `symbols: [...]` in the account's broker's format (see above) |

Pushes `{"type": "tick", "account_id": ..., "symbol": ..., "ltp": ...}`.

**`/api/accounts/{id}/ws/orders`**

| action | fields |
|---|---|
| `place_order` | `symbol`, `side` (BUY/SELL), `quantity`, `order_type` (MARKET/LIMIT), `limit_price` |
| `cancel_order` | `order_id` |
| `get_orders`, `get_positions`, `get_funds` | — |

Pushes `{"type": "order_update", "account_id": ..., "order": {...}}` to that account's clients only.

## Layout

```
app/
  main.py                 wires the apps, the account registry and the strategy runner together
  core/
    config.py             settings from env / .env
    db.py                 SQLAlchemy async engine/session; runs migrations at startup
    broker_service.py     one account's live broker; restarted after a broker login; order
                          de-duplication, rate limit, reconciliation with the order book
    connections.py        tracks sockets per app, what each one follows, per-client send queues
    instance_lock.py      one server process at a time
    ratelimit.py          token bucket for broker order calls
    ws.py                 shared WebSocket loop (auth, parsing, ack/error replies)
    schemas.py            messages shared by all apps (ping)
  accounts/
    models.py             broker_accounts table
    vault.py              AES-256-GCM encryption of credentials/tokens + key CLI
    specs.py              what each broker needs (credentials, options); masking
    store.py              account CRUD; the only place secrets are encrypted/decrypted
    registry.py           each active account's broker, ledger and sockets
    router.py             /api/accounts
    dependencies.py       "your account or 404"
  brokers/
    base.py               Broker interface — implement this for another broker
    paper.py              simulated broker
    fyers.py              Fyers API v3 broker
    zerodha.py            Zerodha Kite Connect broker
  auth/
    pages.py              /login page, /logout
    models.py             users, api_tokens, login_attempts tables
    tokens.py             personal API tokens (hashed)
    users.py              user accounts + `python -m app.auth.users` CLI
    passwords.py          scrypt hashing
    throttle.py           failed-login lockout (backed by login_attempts)
    dependencies.py       who's calling: session cookie or API token -> a User
    router.py             daily broker login per account, broker callbacks
  tickers/                router.py, schemas.py
  orders/                 router.py, schemas.py, models.py (orders, order_events), history.py
  trades/                 ledger.py (fills -> positions), models.py, router.py
  strategies/             deployments.py (what runs: edit this), deployment.py, base.py
                          (Strategy API), runner.py (sync, risk checks, orders), models.py,
                          router.py, builtin/ (sma_crossover.py)
  static/                 test client page
migrations/               Alembic env.py and versions/ (the schema history)
alembic.ini
tests/                    one file per app, test_accounts.py (encryption, isolation), test_fyers.py,
                          test_zerodha.py, test_reliability.py
```

## Tests

```bash
pytest
```
