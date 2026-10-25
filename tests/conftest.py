import pytest
from fastapi.testclient import TestClient

from app.auth.users import create_user
from app.core.config import Settings, settings

# Tests must not depend on the developer's .env (broker choice, real credentials, ...):
# reset every setting to its default before the app is imported.
for _name, _field in Settings.model_fields.items():
    setattr(settings, _name, _field.get_default(call_default_factory=True))

ADMIN_PASSWORD = "correct horse battery"
settings.api_token = "test-api-token"

from app.main import app  # noqa: E402  (settings above must be in place first)

TOKEN = settings.api_token


@pytest.fixture(autouse=True)
def temp_database(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite file."""
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
def empty_client():
    """App with no users yet."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(empty_client):
    """App with one user, admin / ADMIN_PASSWORD."""
    empty_client.portal.call(create_user, "admin", ADMIN_PASSWORD)
    return empty_client


def run(client, fn, *args):
    """Call an async function on the app's event loop (where the DB engine lives)."""
    return client.portal.call(fn, *args)


def recv_until(ws, msg_type):
    """Skip pushed messages (ticks, order updates) until one of the wanted type arrives."""
    while True:
        msg = ws.receive_json()
        if msg["type"] == msg_type:
            return msg


def sign_in(client, password=ADMIN_PASSWORD, username="admin", next_="/"):
    return client.post("/login", data={"username": username, "password": password, "next": next_},
                       follow_redirects=False)
