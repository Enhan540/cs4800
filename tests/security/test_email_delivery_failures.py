"""An unreachable email provider must fail closed without breaking account flows."""

import logging
import os

import pytest
import requests

from services import email_service
from tests.security.conftest import token

CANARY = "SYNTHETIC-PROVIDER-NETWORK-DETAIL"


def resend_mode(app):
    app.config.update(
        EMAIL_DELIVERY_MODE="resend",
        RESEND_API_KEY="synthetic-delivery-placeholder",
        FROM_EMAIL="planner@example.invalid",
        APP_BASE_URL="https://planner.example.invalid",
    )


def unreachable(error):
    def post(*args, **kwargs):
        raise error(CANARY)
    return post


@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError])
def test_network_failure_returns_false_without_logging_details(secured, monkeypatch, caplog, error):
    app, _db, _ids, _password = secured
    resend_mode(app)
    monkeypatch.setattr(email_service.requests, "post", unreachable(error))
    with caplog.at_level(logging.ERROR), app.app_context():
        sent = email_service.send_email(
            app, recipient="person@example.invalid", subject="Subject", text="PRIVATE-BODY-CANARY",
        )
    assert sent is False
    assert error.__name__ in caplog.text
    for secret in (CANARY, "PRIVATE-BODY-CANARY", "person@example.invalid", "synthetic-delivery-placeholder"):
        assert secret not in caplog.text


def test_signup_completes_when_verification_email_provider_is_unreachable(secured, monkeypatch):
    app, db, _ids, _password = secured
    resend_mode(app)
    monkeypatch.setattr(email_service.requests, "post", unreachable(requests.Timeout))
    client = app.test_client()
    password = os.urandom(24).hex()
    response = client.post("/register", data={
        "username": "network-signup", "email": "signup@example.invalid",
        "password": password, "confirm_password": password, "csrf_token": token(client),
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")
    account = db.accounts.find_one({"normalized_username": "network-signup"})
    assert account["email_verified"] is False
    # The new session is usable: the account was created and signed in.
    assert client.get("/dashboard").status_code == 200


def test_forgot_password_stays_generic_when_provider_is_unreachable(secured, monkeypatch):
    app, _db, _ids, _password = secured
    resend_mode(app)
    monkeypatch.setattr(email_service.requests, "post", unreachable(requests.ConnectionError))
    client = app.test_client()
    response = client.post("/forgot-password", data={
        "email": "owner@example.invalid", "csrf_token": token(client),
    }, follow_redirects=True)
    assert response.status_code == 200
    assert "If an account exists for that email" in response.get_data(as_text=True)
