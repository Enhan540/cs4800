"""Google Calendar access requests are emailed to the developer without exposing the address."""

import copy
import logging

import pytest

import app as app_module
from tests.security.conftest import login
from tests.security.test_authorization import snapshot

pytestmark = pytest.mark.security

DEVELOPER = "developer-canary@example.invalid"


@pytest.fixture(autouse=True)
def limited_mode(secured):
    # Access requests exist only while Google requires approved test users.
    secured[0].config["GOOGLE_CALENDAR_ACCESS"] = "limited"


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    def fake_send(_app, **message):
        sent.append(message)
        return True

    monkeypatch.setattr(app_module, "send_email", fake_send)
    return sent


def member_client(app, password, name="member"):
    client = app.test_client()
    return client, {"X-CSRF-Token": login(client, name, password)}


def request_access(client, headers, google_email="person@gmail.example", consent="yes"):
    return client.post("/account/google-calendar-access", headers=headers,
                       data={"google_email": google_email, "consent": consent})


def test_request_emails_the_developer_and_is_saved(secured, outbox):
    app, db, ids, password = secured
    app.config["FEEDBACK_EMAIL"] = DEVELOPER
    client, headers = member_client(app, password)
    response = request_access(client, headers, "  Person@Gmail.Example ")
    assert response.status_code == 302
    assert len(outbox) == 1
    message = outbox[0]
    assert message["recipient"] == DEVELOPER
    assert message["subject"] == "[Group Planner] Google Calendar access request"
    assert "person@gmail.example" in message["text"] and "audit-member" in message["text"]
    record = db.calendar_access_requests.find_one({"account_id": ids["member"]})
    assert record["google_email"] == "person@gmail.example" and record["email_sent"] is True and record["consent_at"]
    page = client.get("/account").get_data(as_text=True)
    assert "Request sent." in page and DEVELOPER not in page


def test_support_email_is_used_when_no_feedback_email_is_set(secured, outbox):
    app, db, _ids, password = secured
    app.config.update(FEEDBACK_EMAIL="", SUPPORT_EMAIL=DEVELOPER)
    client, headers = member_client(app, password)
    request_access(client, headers)
    assert [message["recipient"] for message in outbox] == [DEVELOPER]


@pytest.mark.parametrize("google_email,consent", [("not-an-email", "yes"), ("", "yes"), ("person@gmail.example", "")])
def test_invalid_email_or_missing_consent_sends_nothing(secured, outbox, google_email, consent):
    app, db, _ids, password = secured
    app.config["FEEDBACK_EMAIL"] = DEVELOPER
    client, headers = member_client(app, password)
    before = copy.deepcopy(snapshot(db))
    assert request_access(client, headers, google_email, consent).status_code == 302
    assert outbox == [] and db.calendar_access_requests.count_documents({}) == 0
    assert snapshot(db) == before


def test_requests_need_login_and_csrf(secured, outbox):
    app, db, _ids, password = secured
    app.config["FEEDBACK_EMAIL"] = DEVELOPER
    anonymous = app.test_client()
    assert anonymous.post("/account/google-calendar-access", data={"google_email": "a@b.example", "consent": "yes"}).status_code in {302, 400}
    client, _headers = member_client(app, password)
    assert client.post("/account/google-calendar-access", data={"google_email": "a@b.example", "consent": "yes"}).status_code == 400
    assert outbox == [] and db.calendar_access_requests.count_documents({}) == 0


@pytest.mark.parametrize("config", [{"FEEDBACK_EMAIL": "", "SUPPORT_EMAIL": ""},
                                    {"FEEDBACK_EMAIL": DEVELOPER, "GOOGLE_CALENDAR_ACCESS": "public"},
                                    {"FEEDBACK_EMAIL": DEVELOPER, "GOOGLE_CALENDAR_ACCESS": "unverified"}])
def test_route_is_unavailable_without_a_recipient_or_when_public(secured, outbox, config):
    app, db, _ids, password = secured
    app.config.update(config)
    client, headers = member_client(app, password)
    assert request_access(client, headers).status_code == 404
    assert outbox == [] and db.calendar_access_requests.count_documents({}) == 0


def test_failed_email_is_reported_and_kept(secured, monkeypatch):
    app, db, ids, password = secured
    app.config["FEEDBACK_EMAIL"] = DEVELOPER
    monkeypatch.setattr(app_module, "send_email", lambda _app, **message: False)
    client, headers = member_client(app, password)
    request_access(client, headers)
    assert db.calendar_access_requests.find_one({"account_id": ids["member"]})["email_sent"] is False
    assert "go through. Please try again later." in client.get("/account").get_data(as_text=True)


def test_requests_are_rate_limited(secured, outbox):
    app, db, _ids, password = secured
    app.config.update(FEEDBACK_EMAIL=DEVELOPER, SECURITY_TEST_RATE_LIMITS=True)
    client, headers = member_client(app, password)
    statuses = [request_access(client, headers, f"person{index}@gmail.example").status_code for index in range(4)]
    assert statuses == [302, 302, 302, 429] and len(outbox) == 3


def test_logs_never_contain_the_requested_or_developer_email(secured, caplog):
    app, db, _ids, password = secured
    app.config.update(FEEDBACK_EMAIL=DEVELOPER, EMAIL_DELIVERY_MODE="resend", RESEND_API_KEY="", FROM_EMAIL="")
    client, headers = member_client(app, password)
    with caplog.at_level(logging.INFO):
        request_access(client, headers, "private-person@gmail.example")
    assert "private-person@gmail.example" not in caplog.text and DEVELOPER not in caplog.text
