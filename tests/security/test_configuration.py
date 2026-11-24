import os

import mongomock
import pytest

from app import create_app

pytestmark = pytest.mark.security


def production(monkeypatch):
    for key, value in {"APP_ENV": "production", "SECRET_KEY": os.urandom(32).hex(), "APP_BASE_URL": "https://planner.example.invalid", "SESSION_COOKIE_SECURE": "true", "EMAIL_DELIVERY_MODE": "resend", "MONGO_URI": "mongodb+srv://cluster.example.invalid/security", "GOOGLE_REDIRECT_URI": "https://planner.example.invalid/auth/google/callback", "GOOGLE_CALENDAR_REDIRECT_URI": "https://planner.example.invalid/auth/google-calendar/callback"}.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("key,value", [("SECRET_KEY", "tiny"), ("APP_BASE_URL", "http://planner.example.invalid"), ("SESSION_COOKIE_SECURE", "false"), ("EMAIL_DELIVERY_MODE", "console"), ("MONGO_URI", "mongodb://cluster.example.invalid/db"), ("MONGO_URI", "mongodb+srv://cluster.example.invalid/db?tlsInsecure=true"), ("GOOGLE_REDIRECT_URI", "https://other.example.invalid/callback")])
def test_unsafe_production_configuration_fails_closed(monkeypatch, key, value):
    production(monkeypatch)
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError):
        create_app(database_override=mongomock.MongoClient().configuration_test)


def test_production_rejects_untrusted_host_and_runtime_bypass(monkeypatch):
    production(monkeypatch)
    app = create_app(database_override=mongomock.MongoClient().configuration_test)
    c = app.test_client()
    assert c.get("/privacy", base_url="https://other.example.invalid").status_code == 400
    response = c.get("/privacy", base_url="https://planner.example.invalid")
    assert response.status_code == 200
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"
    cookie = c.get("/login", base_url="https://planner.example.invalid").headers["Set-Cookie"]
    assert cookie.startswith("__Host-group_planner=")
    assert "Secure" in cookie and "HttpOnly" in cookie and "Path=/" in cookie
    assert "Domain=" not in cookie
    app.config["LOGIN_DISABLED"] = True
    assert c.get("/privacy", base_url="https://planner.example.invalid").status_code == 503


def test_development_without_a_key_never_uses_a_shared_signing_secret(monkeypatch):
    for key in ("SECRET_KEY", "FLASK_SECRET_KEY", "VERCEL", "APP_ENV"):
        monkeypatch.setenv(key, "")
    first = create_app(database_override=mongomock.MongoClient().first)
    second = create_app(database_override=mongomock.MongoClient().second)
    assert len(first.config["SECRET_KEY"]) >= 32
    assert first.config["SECRET_KEY"] != second.config["SECRET_KEY"]


def test_unsafe_transport_is_rejected_before_any_database_connection(monkeypatch):
    production(monkeypatch)
    monkeypatch.setenv("MONGO_URI", "mongodb://cluster.example.invalid/db?tls=false")
    def unexpected_connection(*args, **kwargs):
        pytest.fail("Unsafe configuration attempted a database connection")
    monkeypatch.setattr("app.MongoClient", unexpected_connection)
    with pytest.raises(RuntimeError):
        create_app()


def test_production_requires_identity_indexes(monkeypatch):
    from pymongo.errors import OperationFailure

    production(monkeypatch)
    database = mongomock.MongoClient().index_configuration_test
    original = type(database.accounts).create_index
    def unavailable(collection, keys, *args, **kwargs):
        if collection.name == "accounts" and keys == "google_sub":
            raise OperationFailure("Synthetic index creation failure")
        return original(collection, keys, *args, **kwargs)
    monkeypatch.setattr(type(database.accounts), "create_index", unavailable)
    with pytest.raises(RuntimeError, match="indexes"):
        create_app(database_override=database)
