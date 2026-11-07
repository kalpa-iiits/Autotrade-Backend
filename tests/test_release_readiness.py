import base64
import json
import sqlite3

import pytest

from app.core.config import Settings, production_config_errors
from scripts import backup_database, check_health, restore_database


def production_settings(**overrides):
    values = {
        "environment": "production",
        "database_url": "postgresql+asyncpg://user:password@db.example.com/autotrade",
        "session_secret": "s" * 32,
        "secure_cookies": True,
        "credentials_key": base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="),
        "trusted_hosts": ["trade.example.com"],
        "enable_docs": False,
        "fyers_redirect_uri": "https://trade.example.com/auth/fyers/callback",
        "frontend_url": "https://app.example.com/accounts",
        "cors_origins": ["https://app.example.com"],
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_safe_production_configuration_passes():
    assert production_config_errors(production_settings()) == []


@pytest.mark.parametrize(
    ("setting", "value", "expected"),
    [
        ("session_secret", "short", "SESSION_SECRET"),
        ("secure_cookies", False, "SECURE_COOKIES"),
        ("credentials_key", "", "CREDENTIALS_KEY"),
        ("database_url", "sqlite+aiosqlite:///prod.db", "DATABASE_URL"),
        ("trusted_hosts", [], "TRUSTED_HOSTS"),
        ("enable_docs", True, "ENABLE_DOCS"),
        ("fyers_redirect_uri", "http://trade.example.com/callback", "FYERS_REDIRECT_URI"),
        ("frontend_url", "http://app.example.com", "FRONTEND_URL"),
        ("cors_origins", ["http://app.example.com"], "CORS_ORIGINS"),
    ],
)
def test_unsafe_production_configuration_is_rejected(setting, value, expected):
    assert any(expected in error for error in production_config_errors(production_settings(**{setting: value})))


def test_security_headers_are_present(client):
    response = client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"


def test_sqlite_backup_and_restore(tmp_path, monkeypatch):
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as connection:
        connection.execute("create table sample (value text)")
        connection.execute("insert into sample values ('before')")
    monkeypatch.setattr(backup_database.settings, "database_url", f"sqlite:///{database}")
    backup = backup_database.backup(tmp_path / "backups")
    with sqlite3.connect(database) as connection:
        connection.execute("update sample set value='after'")
    restore_database.restore(backup, str(database))
    with sqlite3.connect(database) as connection:
        assert connection.execute("select value from sample").fetchone()[0] == "before"


def test_external_health_check(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            return json.dumps({"status": "ok", "database": True}).encode()

    monkeypatch.setattr(check_health.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert check_health.check("https://trade.example.com/health")["status"] == "ok"


def test_restore_skips_schema_level_entries():
    from scripts.restore_database import _is_schema_entry

    skipped = [
        "3; 2615 2200 SCHEMA - public pg_database_owner",
        "4; 0 0 COMMENT - SCHEMA public pg_database_owner",
        "5; 0 0 ACL - SCHEMA public pg_database_owner",
    ]
    kept = [
        "; Archive created at 2026-11-07 07:43:44 UTC",
        "220; 1259 17000 TABLE public orders postgres",
        "221; 0 0 ACL public TABLE orders postgres",
        "3400; 0 17000 TABLE DATA public orders postgres",
        "230; 1259 17010 SEQUENCE public positions_id_seq postgres",
    ]
    assert all(_is_schema_entry(line) for line in skipped)
    assert not any(_is_schema_entry(line) for line in kept)
