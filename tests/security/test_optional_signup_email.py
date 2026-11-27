"""Signup works without an email, and the page explains why email is limited."""

import os

import pytest

from services import email_service
from tests.security.conftest import token

pytestmark = pytest.mark.security


def signup(client, username, email=""):
    password = os.urandom(24).hex()
    return client.post("/register", data={
        "username": username, "email": email, "password": password,
        "confirm_password": password, "csrf_token": token(client),
    })


def test_signup_page_marks_email_optional_and_explains_the_limit(secured):
    app, _db, _ids, _password = secured
    page = app.test_client().get("/register").get_data(as_text=True)
    email_input = page[page.index('id="email"'):].split(">", 1)[0]
    assert "required" not in email_input
    assert "Email (optional)" in page
    assert "doesn't have its own published domain" in page
    assert 'aria-describedby="email-delivery-notice"' in page


def test_several_accounts_can_sign_up_without_email(secured, monkeypatch):
    app, db, _ids, _password = secured
    sent = []
    monkeypatch.setattr(email_service, "send_email", lambda *args, **kwargs: sent.append(kwargs) or True)
    for name in ("no-email-one", "no-email-two"):
        response = signup(app.test_client(), name)
        assert response.status_code == 302 and response.headers["Location"].endswith("/dashboard")
        account = db.accounts.find_one({"normalized_username": name})
        # No email fields at all, so the unique sparse email indexes never collide.
        assert not {"email", "normalized_email", "email_normalized", "email_verified"} & set(account)
    assert sent == []


def test_an_email_given_at_signup_is_still_validated_and_unique(secured):
    app, db, _ids, _password = secured
    assert signup(app.test_client(), "bad-email-user", "not-an-email").status_code == 400
    assert db.accounts.find_one({"normalized_username": "bad-email-user"}) is None
    assert signup(app.test_client(), "taken-email-user", "owner@example.invalid").status_code == 400
    assert db.accounts.find_one({"normalized_username": "taken-email-user"}) is None
