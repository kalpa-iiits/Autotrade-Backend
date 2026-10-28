import pytest
from starlette.websockets import WebSocketDisconnect

from app.auth.passwords import hash_password, verify_password
from app.auth.users import UserError, create_user, list_users, set_active, set_password
from app.core.config import settings
from tests.conftest import ADMIN_PASSWORD, TOKEN, run, sign_in


def test_pages_redirect_to_login_when_signed_out(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/"
    assert client.get("/api/accounts").status_code == 401
    assert client.get("/api/accounts/1/orders").status_code == 401
    assert client.get("/health").status_code == 200  # stays public for monitoring
    page = client.get("/login")
    assert page.status_code == 200 and 'name="password"' in page.text


def test_sign_in_and_out(client):
    r = sign_in(client, next_="/api/accounts/1/funds")
    assert r.status_code == 303 and r.headers["location"] == "/api/accounts/1/funds"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie

    assert client.get("/").status_code == 200
    assert [a["broker"] for a in client.get("/api/accounts").json()] == ["paper"]
    assert client.get("/login", follow_redirects=False).status_code == 303  # already signed in

    assert client.post("/logout", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_wrong_credentials(client):
    r = sign_in(client, password="nope")
    assert r.status_code == 401 and "Invalid email or password" in r.text
    assert sign_in(client, username="root").status_code == 401
    assert client.get("/api/accounts").status_code == 401


def test_next_cannot_leave_the_site(client):
    for target in ["//evil.example/x", "https://evil.example", "/\\evil.example"]:
        assert sign_in(client, next_=target).headers["location"] == "/"
        client.post("/logout")
    page = client.get('/login?next="><script>alert(1)</script>')
    assert "<script>alert(1)" not in page.text


def test_lockout_after_repeated_failures(client):
    for _ in range(5):
        assert sign_in(client, password="nope").status_code == 401
    r = sign_in(client)  # right password, still locked out
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


def test_changing_password_ends_existing_sessions(client):
    sign_in(client)
    assert client.get("/api/accounts").status_code == 200
    run(client, set_password, "admin", "a brand new password")
    assert client.get("/api/accounts").status_code == 401
    assert sign_in(client).status_code == 401
    assert sign_in(client, password="a brand new password").status_code == 303


def test_disabled_user_is_signed_out_and_cannot_sign_in(client):
    sign_in(client)
    run(client, set_active, "admin", False)
    assert client.get("/api/accounts").status_code == 401
    assert sign_in(client).status_code == 401
    run(client, set_active, "admin", True)
    assert sign_in(client).status_code == 303


def test_multiple_users(client):
    run(client, create_user, "trader2", "another long password")
    assert sign_in(client, username="trader2", password="another long password").status_code == 303
    assert [u.username for u in run(client, list_users)] == ["admin", "trader2"]
    assert run(client, list_users)[1].last_login_at is not None


def test_user_validation(client):
    with pytest.raises(UserError, match="already exists"):
        run(client, create_user, "admin", "whatever-long-enough")
    with pytest.raises(UserError, match="at least"):
        run(client, create_user, "shorty", "short")
    with pytest.raises(UserError, match="No user"):
        run(client, set_password, "ghost", "whatever-long-enough")


def test_lockout_survives_restart(client):
    for _ in range(5):
        sign_in(client, password="nope")
    # A fresh app on the same database still remembers the failures.
    from fastapi.testclient import TestClient
    from app.main import app
    client.__exit__(None, None, None)
    with TestClient(app) as fresh:
        assert sign_in(fresh).status_code == 429


def test_not_configured(empty_client):
    page = empty_client.get("/login")
    assert "python -m app.auth.users create" in page.text and "disabled" in page.text
    assert sign_in(empty_client).status_code == 503


def test_websockets_use_the_session(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/accounts/1/ws/orders") as ws:
            ws.receive_json()

    sign_in(client)
    with client.websocket_connect("/api/accounts/1/ws/orders") as ws:
        assert ws.receive_json()["type"] == "welcome"

    # Another site's page can't ride on the cookie.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/accounts/1/ws/tickers", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()


def test_api_token_access(client):
    assert client.get("/api/accounts/1/funds", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert client.get("/api/accounts/1/funds", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get("/api/accounts/1/funds", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/accounts/1/funds?token=").status_code == 401


def test_api_docs_need_login(client):
    assert client.get("/docs").status_code == 404  # no public docs
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/api/docs", follow_redirects=False).headers["location"] == "/login?next=/api/docs"
    assert client.get("/api/openapi.json").status_code == 401
    sign_in(client)
    assert "swagger" in client.get("/api/docs").text.lower()
    paths = client.get("/api/openapi.json").json()["paths"]
    assert "/api/accounts/{account_id}/orders" in paths and "/login" not in paths


def test_password_hashing():
    encoded = hash_password("s3cret-passphrase")
    assert encoded.startswith("scrypt:") and "s3cret" not in encoded
    assert verify_password("s3cret-passphrase", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("s3cret-passphrase", "garbage")
    assert hash_password("same") != hash_password("same")  # salted
