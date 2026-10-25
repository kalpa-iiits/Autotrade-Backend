import pytest
from fastapi.testclient import TestClient

from app.accounts.vault import generate_key
from app.auth.tokens import create_token
from app.auth.users import create_user
from app.core.config import Settings, settings

# Tests must not depend on the developer's .env (credentials key, real accounts, ...):
# reset every setting to its default before the app is imported.
for _name, _field in Settings.model_fields.items():
    setattr(settings, _name, _field.get_default(call_default_factory=True))

ADMIN_PASSWORD = "correct horse battery"
settings.credentials_key = generate_key()

from app.main import app  # noqa: E402  (settings above must be in place first)

TOKEN = "test-api-token"  # admin's personal API token
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ACCOUNT = 1  # admin's paper account, created by the `client` fixture
A = f"/api/accounts/{ACCOUNT}"


@pytest.fixture(autouse=True)
def temp_database(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite file (and lock file next to it)."""
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")


@pytest.fixture(autouse=True)
def no_real_brokers(monkeypatch):
    """Fyers / Zerodha accounts never reach the real brokers in tests: building one fails as
    "not logged in". Tests that need one patch app.accounts.registry.create_broker with a fake."""
    import app.accounts.registry as registry
    from app.brokers import MissingCredentials, create_broker

    def guarded(settings_, broker, *args, **kwargs):
        if broker != "paper":
            raise MissingCredentials(f"Real {broker} connections are disabled in tests")
        return create_broker(settings_, broker, *args, **kwargs)

    monkeypatch.setattr(registry, "create_broker", guarded)


@pytest.fixture
def empty_client():
    """App with no users yet."""
    with TestClient(app) as c:
        yield c


def add_user(client, username, password=ADMIN_PASSWORD, token=None, paper=True):
    """A user with an API token and (by default) a paper account. Returns (auth headers, account id)."""
    user = client.portal.call(create_user, username, password)
    _, token = client.portal.call(create_token, user.id, "test", token)
    headers = {"Authorization": f"Bearer {token}"}
    account_id = None
    if paper:
        r = client.post("/api/accounts", json={"broker": "paper", "label": "Paper"}, headers=headers)
        assert r.status_code == 201, r.text
        account_id = r.json()["id"]
    return headers, account_id


@pytest.fixture
def client(empty_client):
    """App with one user, admin / ADMIN_PASSWORD, token TOKEN, and paper account ACCOUNT."""
    _, account_id = add_user(empty_client, "admin", token=TOKEN)
    assert account_id == ACCOUNT
    return empty_client


def run(client, fn, *args):
    """Call an async function on the app's event loop (where the DB engine lives)."""
    return client.portal.call(fn, *args)


def runtime(client, account_id=ACCOUNT):
    """An account's live pieces: .service (broker), .ledger, .orders_ws, .tickers_ws."""
    return client.app.state.accounts.get(account_id)


def recv_until(ws, msg_type):
    """Skip pushed messages (ticks, order updates) until one of the wanted type arrives."""
    while True:
        msg = ws.receive_json()
        if msg["type"] == msg_type:
            return msg


def sign_in(client, password=ADMIN_PASSWORD, username="admin", next_="/"):
    return client.post("/login", data={"username": username, "password": password, "next": next_},
                       follow_redirects=False)
