"""Bug reports and suggestions reach the developer without exposing addresses or secrets."""

import copy
import logging

import pytest

import app as app_module
from services import feedback_service
from tests.security.conftest import login, token
from tests.security.test_authorization import snapshot

pytestmark = pytest.mark.security

DEVELOPER = "developer-canary@example.invalid"


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "send_email", lambda _app, **message: sent.append(message) or True)
    return sent


def send(client, proof=None, **fields):
    data = {"type": "bug", "summary": "Plan page crashes", "details": "Clicked schedule.", **fields}
    data["csrf_token"] = proof or token(client)
    return client.post("/feedback", data=data, headers={"User-Agent": "SyntheticBrowser/1.0 " + "x" * 400})


def test_footer_links_appear_on_every_page_and_name_the_page(secured):
    app, _db, _ids, password = secured
    anonymous = app.test_client()
    for path in ("/", "/login", "/privacy", "/does-not-exist"):
        page = anonymous.get(path).get_data(as_text=True)
        assert "Report a bug" in page and "Suggest an idea" in page, path
    client = app.test_client()
    login(client, "owner", password)
    page = client.get("/dashboard").get_data(as_text=True)
    assert 'href="/feedback?type=bug&amp;from=dashboard"' in page


def test_signed_out_report_is_saved_and_emailed(secured, outbox):
    app, db, _ids, _password = secured
    app.config["FEEDBACK_EMAIL"] = DEVELOPER
    client = app.test_client()
    response = send(client, page="login", contact_email="Reply@Example.invalid")
    assert response.status_code == 302 and response.headers["Location"].endswith("/feedback/thanks")
    record = db.feedback.find_one({})
    assert record["type"] == "bug" and record["page"] == "login" and record["account_id"] is None
    assert record["contact_email"] == "reply@example.invalid" and record["email_sent"] is True
    assert len(record["user_agent"]) == feedback_service.USER_AGENT_MAX
    assert not {"ip", "remote_addr", "ip_address"} & set(record)
    [message] = outbox
    assert message["recipient"] == DEVELOPER and message["subject"] == "[Group Planner] Bug report"
    assert "Plan page crashes" in message["text"] and "someone who was signed out" in message["text"]


def test_signed_in_suggestion_can_reply_to_the_verified_account_email(secured, outbox):
    app, db, ids, password = secured
    app.config["FEEDBACK_EMAIL"] = DEVELOPER
    client = app.test_client()
    proof = login(client, "owner", password)
    assert "Reply to my account email" in client.get("/feedback?type=suggestion").get_data(as_text=True)
    send(client, proof, type="suggestion", summary="Dark mode", details="", use_account_email="yes")
    record = db.feedback.find_one({})
    assert record["account_id"] == ids["owner"] and record["contact_email"] == "owner@example.invalid"
    assert record["user_agent"] is None
    assert outbox[0]["subject"] == "[Group Planner] Suggestion" and "audit-owner" in outbox[0]["text"]


@pytest.mark.parametrize("page", ["reset_password", "verify_email"])
def test_token_pages_are_recorded_by_name_only(secured, outbox, page):
    app, db, _ids, _password = secured
    client = app.test_client()
    send(client, page=page)
    assert db.feedback.find_one({})["page"] == page


@pytest.mark.parametrize("page", ["/reset-password/SECRET-TOKEN", "https://evil.example/x", "static", "not_a_page"])
def test_addresses_and_unknown_pages_are_never_stored(secured, outbox, page):
    app, db, _ids, _password = secured
    send(app.test_client(), page=page)
    assert db.feedback.find_one({})["page"] is None


@pytest.mark.parametrize("fields", [
    {"summary": ""}, {"summary": "x" * 121}, {"details": "x" * 4001},
    {"type": "complaint"}, {"contact_email": "not-an-email"},
])
def test_invalid_reports_save_and_send_nothing(secured, outbox, fields):
    app, db, _ids, _password = secured
    app.config["FEEDBACK_EMAIL"] = DEVELOPER
    before = copy.deepcopy(snapshot(db))
    assert send(app.test_client(), **fields).status_code == 400
    assert db.feedback.count_documents({}) == 0 and outbox == [] and snapshot(db) == before


def test_trap_field_silently_discards_bots(secured, outbox):
    app, db, _ids, _password = secured
    app.config["FEEDBACK_EMAIL"] = DEVELOPER
    response = send(app.test_client(), website="http://spam.example")
    assert response.status_code == 302 and response.headers["Location"].endswith("/feedback/thanks")
    assert db.feedback.count_documents({}) == 0 and outbox == []


def test_csrf_is_required(secured, outbox):
    app, db, _ids, _password = secured
    client = app.test_client()
    response = client.post("/feedback", data={"type": "bug", "summary": "No token"})
    assert response.status_code == 400 and db.feedback.count_documents({}) == 0


def test_reports_are_saved_when_no_developer_address_or_email_fails(secured, monkeypatch):
    app, db, _ids, _password = secured
    app.config.update(FEEDBACK_EMAIL="", SUPPORT_EMAIL="")
    send(app.test_client())
    app.config["FEEDBACK_EMAIL"] = DEVELOPER
    monkeypatch.setattr(app_module, "send_email", lambda _app, **message: False)
    send(app.test_client())
    assert [item["email_sent"] for item in db.feedback.find({})] == [False, False]


def test_network_address_rate_limit(secured, outbox):
    app, db, _ids, _password = secured
    app.config.update(FEEDBACK_EMAIL=DEVELOPER, SECURITY_TEST_RATE_LIMITS=True)
    client = app.test_client()
    statuses = [send(client, summary=f"Report {index}").status_code for index in range(6)]
    assert statuses == [302] * 5 + [429] and db.feedback.count_documents({}) == 5


def test_developer_email_cap_keeps_saving_reports(secured, outbox):
    app, db, _ids, _password = secured
    app.config["FEEDBACK_EMAIL"] = DEVELOPER
    db.security_rate_limits.delete_many({})
    for index in range(35):
        send(app.test_client(), summary=f"Flood {index}")
    assert db.feedback.count_documents({}) == 35 and len(outbox) == 30


def test_feedback_text_never_appears_on_pages_or_in_logs(secured, outbox, caplog):
    app, db, _ids, password = secured
    app.config["FEEDBACK_EMAIL"] = DEVELOPER
    client = app.test_client()
    with caplog.at_level(logging.INFO):
        response = send(client, summary="PRIVATE-SUMMARY-CANARY", details="<script>PRIVATE-DETAILS</script>",
                        contact_email="private-reply@example.invalid")
        thanks = client.get(response.headers["Location"]).get_data(as_text=True)
    for value in ("PRIVATE-SUMMARY-CANARY", "PRIVATE-DETAILS", "private-reply@example.invalid", DEVELOPER):
        assert value not in thanks and value not in caplog.text
    owner = app.test_client()
    login(owner, "owner", password)
    for path in ("/dashboard", "/account", "/feedback"):
        assert "PRIVATE-SUMMARY-CANARY" not in owner.get(path).get_data(as_text=True)


def test_privacy_policy_describes_feedback(secured):
    app, _db, _ids, _password = secured
    page = app.test_client().get("/privacy").get_data(as_text=True)
    assert "Feedback you send" in page and "180 days" in page and "No IP address is stored" in page
