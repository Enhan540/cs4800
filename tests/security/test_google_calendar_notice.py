"""The Google Calendar testing notice appears only while access is limited."""

import pytest

from tests.security.conftest import login

pytestmark = pytest.mark.security

NOTICE = "Google Calendar is in limited testing."
UNVERIFIED = "Google hasn't verified Group Planner yet."


def owner_client(app, db, password):
    db.calendar_connections.delete_many({})
    client = app.test_client()
    login(client, "owner", password)
    return client


@pytest.mark.parametrize("path", ["/account", "/account?calendar_prompt=1"])
def test_limited_notice(secured, path):
    app, db, _ids, password = secured
    app.config["GOOGLE_CALENDAR_ACCESS"] = "limited"
    page = owner_client(app, db, password).get(path).get_data(as_text=True)
    assert NOTICE in page and "renewed every 7 days" in page and "iCal link" in page
    if "calendar_prompt" in path:
        assert page.count(NOTICE) == 2  # the calendars section and the post-signup prompt


@pytest.mark.parametrize("setting", ["SUPPORT_EMAIL", "FEEDBACK_EMAIL"])
def test_request_form_appears_without_revealing_the_developer_address(secured, setting):
    app, db, _ids, password = secured
    app.config["GOOGLE_CALENDAR_ACCESS"] = "limited"
    app.config[setting] = "developer-canary@example.invalid"
    page = owner_client(app, db, password).get("/account?calendar_prompt=1").get_data(as_text=True)
    assert "developer-canary@example.invalid" not in page and "mailto:" not in page
    assert page.count('action="/account/google-calendar-access"') == 1  # one form; the prompt links to it
    assert 'href="#calendar-access-request"' in page


def test_without_a_developer_address_the_notice_asks_people_to_contact_the_developer(secured):
    app, db, _ids, password = secured
    app.config["GOOGLE_CALENDAR_ACCESS"] = "limited"
    app.config.update(SUPPORT_EMAIL="", FEEDBACK_EMAIL="")
    page = owner_client(app, db, password).get("/account").get_data(as_text=True)
    assert "google-calendar-access" not in page and "Ask the developer to add the Google email" in page


def test_public_mode_hides_the_notice(secured):
    app, db, _ids, password = secured
    app.config["GOOGLE_CALENDAR_ACCESS"] = "public"
    page = owner_client(app, db, password).get("/account?calendar_prompt=1").get_data(as_text=True)
    assert NOTICE not in page


@pytest.mark.parametrize("value,expected", [("everyone", "unverified"), ("", "unverified"), (" Limited ", "limited"),
                                            ("unverified", "unverified")])
def test_setting_values(monkeypatch, value, expected):
    import mongomock
    from app import create_app
    monkeypatch.setenv("GOOGLE_CALENDAR_ACCESS", value)
    app = create_app(database_override=mongomock.MongoClient().notice)
    assert app.config["GOOGLE_CALENDAR_ACCESS"] == expected
    monkeypatch.setenv("GOOGLE_CALENDAR_ACCESS", " Public ")
    assert create_app(database_override=mongomock.MongoClient().notice).config["GOOGLE_CALENDAR_ACCESS"] == "public"


@pytest.mark.parametrize("mode,explains", [("limited", True), ("public", False), ("unverified", False)])
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


@pytest.mark.parametrize("path", ["/account", "/account?calendar_prompt=1"])
def test_unverified_notice_is_the_default_and_has_no_request_form(secured, path):
    app, db, _ids, password = secured
    app.config["SUPPORT_EMAIL"] = "developer-canary@example.invalid"
    page = owner_client(app, db, password).get(path).get_data(as_text=True)
    assert UNVERIFIED in page and "<strong>Advanced</strong>" in page and "iCal link" in page
    assert NOTICE not in page and "google-calendar-access" not in page and "renewed every 7 days" not in page


def test_unverified_callback_explains_the_warning(secured):
    app, db, ids, password = secured
    client = owner_client(app, db, password)
    with client.session_transaction() as state:
        state["google_calendar_oauth_state"] = "synthetic-state"
        state["google_calendar_oauth_account_id"] = str(ids["owner"])
    page = client.get("/auth/google-calendar/callback?state=synthetic-state&error=access_denied", follow_redirects=True).get_data(as_text=True)
    assert "choose Advanced, then Go to Group Planner" in page
