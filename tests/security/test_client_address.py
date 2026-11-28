"""Rate limits count each visitor separately on Vercel without trusting headers elsewhere."""

import pytest

from services.security_service import client_address
from tests.security.conftest import token

pytestmark = pytest.mark.security


def feedback(client, headers=None):
    return client.post("/feedback", headers=headers or {}, data={
        "type": "bug", "summary": "Rate limit check", "csrf_token": token(client),
    })


@pytest.mark.parametrize("headers,trusted,expected", [
    ({"X-Real-IP": "203.0.113.7"}, True, "203.0.113.7"),
    ({"X-Forwarded-For": "198.51.100.9, 10.0.0.1"}, True, "198.51.100.9"),
    ({"X-Real-IP": "not-an-ip", "X-Forwarded-For": "198.51.100.9"}, True, "198.51.100.9"),
    ({"X-Real-IP": "garbage"}, True, "127.0.0.1"),
    ({"X-Real-IP": "203.0.113.7"}, False, "127.0.0.1"),
    ({}, True, "127.0.0.1"),
])
def test_client_address(secured, headers, trusted, expected):
    app, _db, _ids, _password = secured
    app.config["TRUST_PLATFORM_CLIENT_IP"] = trusted
    with app.test_request_context("/", headers=headers, environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert client_address(app) == expected


def test_visitors_get_separate_limits_on_vercel(secured):
    app, db, _ids, _password = secured
    app.config.update(SECURITY_TEST_RATE_LIMITS=True, TRUST_PLATFORM_CLIENT_IP=True)
    first, second = app.test_client(), app.test_client()
    one = {"X-Real-IP": "203.0.113.1"}
    assert [feedback(first, one).status_code for _ in range(6)] == [302] * 5 + [429]
    # A different visitor behind the same proxy address still has their own allowance.
    assert feedback(second, {"X-Real-IP": "203.0.113.2"}).status_code == 302


def test_forged_headers_do_not_bypass_limits_off_vercel(secured):
    app, db, _ids, _password = secured
    app.config.update(SECURITY_TEST_RATE_LIMITS=True, TRUST_PLATFORM_CLIENT_IP=False)
    client = app.test_client()
    statuses = [feedback(client, {"X-Real-IP": f"203.0.113.{index}"}).status_code for index in range(6)]
    assert statuses == [302] * 5 + [429]


def test_vercel_environment_turns_on_trust(monkeypatch):
    import mongomock
    from app import create_app
    monkeypatch.delenv("VERCEL", raising=False)
    assert create_app(database_override=mongomock.MongoClient().ip).config["TRUST_PLATFORM_CLIENT_IP"] is False
