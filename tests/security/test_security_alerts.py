import copy
from datetime import timedelta

import pytest
import requests
from pymongo.errors import OperationFailure

from services import security_alert_service as alerts

pytestmark = pytest.mark.security


def test_security_alert_uses_previous_verified_contact_and_contains_no_secrets(secured, monkeypatch):
    app, db, ids, password = secured
    calls = []
    monkeypatch.setattr(alerts, "send_security_change_email", lambda *args, **kwargs: calls.append(kwargs) or True)
    before = db.accounts.find_one({"_id": ids["owner"]})
    updated = alerts.commit_change(app, db, before, {}, {"$set": {"email": "unverified@example.invalid", "email_verified": False}, "$inc": {"auth_version": 1}}, "email_changed")
    assert updated["email"] == "unverified@example.invalid"
    assert calls[0]["recipient"] == "owner@example.invalid"
    assert password not in str(calls) and "unverified@example.invalid" not in str(calls)
    assert db.notifications.count_documents({"recipient_user_id": ids["owner"], "type": "SECURITY_CHANGE"}) == 1
    assert db.security_alerts.find_one({})["state"] == "sent"


def test_unverified_contacts_get_only_an_owned_in_app_alert(secured, monkeypatch):
    app, db, ids, password = secured
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"email_verified": False}})
    monkeypatch.setattr(alerts, "send_security_change_email", lambda *a, **k: pytest.fail("Do not send security details to an unverified contact"))
    account = db.accounts.find_one({"_id": ids["owner"]})
    alerts.commit_change(app, db, account, {}, {"$inc": {"auth_version": 1}}, "password_changed")
    assert db.notifications.find_one({"type": "SECURITY_CHANGE"})["recipient_user_id"] == ids["owner"]


def test_failed_delivery_retries_with_stable_idempotency_and_no_duplicate_notice(secured, monkeypatch):
    app, db, ids, password = secured
    calls = []
    def send(*args, **kwargs):
        calls.append(kwargs)
        return len(calls) > 1
    monkeypatch.setattr(alerts, "send_security_change_email", send)
    account = db.accounts.find_one({"_id": ids["owner"]})
    alerts.commit_change(app, db, account, {}, {"$inc": {"auth_version": 1}}, "password_changed")
    job = db.security_alerts.find_one({})
    assert job["state"] == "pending"
    db.security_alerts.update_one({"_id": job["_id"]}, {"$set": {"next_attempt_at": alerts.now() - timedelta(seconds=1)}})
    assert alerts.deliver_due(app, db)["sent"] == 1
    assert calls[0] == calls[1]
    assert db.notifications.count_documents({"type": "SECURITY_CHANGE"}) == 1


def test_prepared_job_recovers_after_account_commit_but_before_activation(secured, monkeypatch):
    app, db, ids, password = secured
    original = type(db.security_alerts).update_one
    failed = []
    def interrupt(collection, query, update, *args, **kwargs):
        if collection.name == "security_alerts" and update.get("$set", {}).get("state") == "pending" and not failed:
            failed.append(True)
            raise OperationFailure("Synthetic interruption")
        return original(collection, query, update, *args, **kwargs)
    monkeypatch.setattr(type(db.security_alerts), "update_one", interrupt)
    monkeypatch.setattr(alerts, "send_security_change_email", lambda *a, **k: True)
    account = db.accounts.find_one({"_id": ids["owner"]})
    updated = alerts.commit_change(app, db, account, {}, {"$inc": {"auth_version": 1}}, "password_changed")
    assert updated["auth_version"] == 1 and db.security_alerts.find_one({})["state"] == "prepared"
    assert alerts.deliver_due(app, db)["sent"] == 1
    assert db.accounts.find_one({"_id": ids["owner"]})["security_alert_markers"] == []


def test_failed_or_cross_account_mutation_cannot_send_an_alert(secured, monkeypatch):
    app, db, ids, password = secured
    before = copy.deepcopy(list(db.accounts.find({})))
    monkeypatch.setattr(alerts, "send_security_change_email", lambda *a, **k: pytest.fail("An uncommitted change must not send an alert"))
    account = db.accounts.find_one({"_id": ids["owner"]})
    assert alerts.commit_change(app, db, account, {"_id": ids["outsider"]}, {"$set": {"username": "changed"}}, "username_changed") is None
    assert list(db.accounts.find({})) == before
    assert db.security_alerts.count_documents({}) == 0 and db.notifications.count_documents({"type": "SECURITY_CHANGE"}) == 0


def test_provider_error_is_deferred_without_logging_payload(secured, monkeypatch, caplog):
    app, db, ids, password = secured
    canary = "SYNTHETIC-PRIVATE-PROVIDER-DETAIL"
    def fail(*args, **kwargs):
        raise requests.Timeout(canary)
    monkeypatch.setattr(alerts, "send_security_change_email", fail)
    account = db.accounts.find_one({"_id": ids["owner"]})
    assert alerts.commit_change(app, db, account, {}, {"$inc": {"auth_version": 1}}, "password_changed")
    assert db.security_alerts.find_one({})["state"] == "pending"
    assert canary not in caplog.text


def test_retry_winning_activation_race_does_not_requeue_sent_alert(secured, monkeypatch):
    app, db, ids, password = secured
    calls = []
    monkeypatch.setattr(alerts, "send_security_change_email", lambda *args, **kwargs: calls.append(kwargs) or True)
    collection_type = type(db.accounts)
    original = collection_type.find_one_and_update
    raced = []
    def retry_after_commit(collection, query, update, *args, **kwargs):
        result = original(collection, query, update, *args, **kwargs)
        if collection.name == "accounts" and "security_alert_markers" in update.get("$addToSet", {}) and not raced:
            raced.append(True)
            assert alerts.deliver_due(app, db)["sent"] == 1
        return result
    monkeypatch.setattr(collection_type, "find_one_and_update", retry_after_commit)
    account = db.accounts.find_one({"_id": ids["owner"]})
    alerts.commit_change(app, db, account, {}, {"$inc": {"auth_version": 1}}, "password_changed")
    assert raced and len(calls) == 1
    assert db.security_alerts.find_one({})["state"] == "sent"


def test_account_security_alert_cannot_be_read_by_another_account(secured):
    from tests.security.conftest import login
    app, db, ids, password = secured
    account = db.accounts.find_one({"_id": ids["owner"]})
    alerts.commit_change(app, db, account, {}, {"$inc": {"auth_version": 1}}, "mfa_enabled")
    own, other = app.test_client(), app.test_client()
    login(own, "owner", password)
    login(other, "outsider", password)
    assert "Authenticator verification was enabled" in own.get("/api/notifications").text
    assert "Authenticator verification was enabled" not in other.get("/api/notifications").text
