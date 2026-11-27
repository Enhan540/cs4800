"""The Google Calendar testing notice appears only while access is limited."""

import pytest

from tests.security.conftest import login

pytestmark = pytest.mark.security

NOTICE = "Google Calendar is in limited testing."


def owner_client(app, db, password):
    db.calendar_connections.delete_many({})
    client = app.test_client()
    login(client, "owner", password)
    return client


@pytest.mark.parametrize("path", ["/account", "/account?calendar_prompt=1"])
def test_notice_shows_by_default(secured, path):
    app, db, _ids, password = secured
    page = owner_client(app, db, password).get(path).get_data(as_text=True)
    assert NOTICE in page and "renewed every 7 days" in page and "iCal link" in page
    if "calendar_prompt" in path:
        assert page.count(NOTICE) == 2  # the calendars section and the post-signup prompt


def test_request_access_links_to_support_email_when_configured(secured):
    app, db, _ids, password = secured
    app.config["SUPPORT_EMAIL"] = "support@example.invalid"
    page = owner_client(app, db, password).get("/account").get_data(as_text=True)
    assert 'href="mailto:support@example.invalid?subject=Google%20Calendar%20access%20request"' in page
    app.config["SUPPORT_EMAIL"] = ""
    page = owner_client(app, db, password).get("/account").get_data(as_text=True)
    assert "mailto:" not in page and "Ask the developer to add the Google email" in page


def test_public_mode_hides_the_notice(secured):
    app, db, _ids, password = secured
    app.config["GOOGLE_CALENDAR_ACCESS"] = "public"
    page = owner_client(app, db, password).get("/account?calendar_prompt=1").get_data(as_text=True)
    assert NOTICE not in page


def test_unknown_setting_falls_back_to_limited(monkeypatch):
    import mongomock
    from app import create_app
    monkeypatch.setenv("GOOGLE_CALENDAR_ACCESS", "everyone")
    app = create_app(database_override=mongomock.MongoClient().notice)
    assert app.config["GOOGLE_CALENDAR_ACCESS"] == "limited"
    monkeypatch.setenv("GOOGLE_CALENDAR_ACCESS", " Public ")
    assert create_app(database_override=mongomock.MongoClient().notice).config["GOOGLE_CALENDAR_ACCESS"] == "public"


@pytest.mark.parametrize("mode,explains", [("limited", True), ("public", False)])
def test_denied_callback_explains_limited_testing(secured, mode, explains):
    app, db, ids, password = secured
    app.config["GOOGLE_CALENDAR_ACCESS"] = mode
    client = owner_client(app, db, password)
    with client.session_transaction() as state:
        state["google_calendar_oauth_state"] = "synthetic-state"
        state["google_calendar_oauth_account_id"] = str(ids["owner"])
    response = client.get("/auth/google-calendar/callback?state=synthetic-state&error=access_denied", follow_redirects=True)
    page = response.get_data(as_text=True)
    assert "cancelled or denied" in page
    assert ("your Google account may not be approved yet" in page) == explains
