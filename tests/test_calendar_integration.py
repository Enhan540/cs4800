"""Calendar connection and planning behavior with provider calls mocked."""

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from cryptography.fernet import Fernet

os.environ["MONGO_URI"] = ""
from app import create_app


@pytest.fixture
def calendar_app(monkeypatch):
    database = mongomock.MongoClient().calendar_integration
    app = create_app(database_override=database)
    app.config.update(TESTING=True, SECRET_KEY="test-secret", LOGIN_DISABLED=True)
    monkeypatch.setattr("app.utc_now", lambda: datetime(2026, 11, 1, 20, tzinfo=timezone.utc))
    return app, database


def encrypted(secret):
    key = base64.urlsafe_b64encode(hashlib.sha256(b"test-secret").digest())
    return Fernet(key).encrypt(json.dumps(secret).encode()).decode()


def feed(day="20261102", start="100000", end="110000"):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
            "BEGIN:VEVENT\r\nUID:busy@test\r\n"
            f"DTSTART;TZID=America/Los_Angeles:{day}T{start}\r\n"
            f"DTEND;TZID=America/Los_Angeles:{day}T{end}\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n").encode()


class FreeBusyResponse:
    def __init__(self, busy=None, status=200, errors=None):
        self.status_code = status
        self.busy = busy or []
        self.errors = errors

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ValueError("Provider request failed")

    def json(self):
        if self.errors is not None:
            return {"calendars": {"primary": {"errors": self.errors}}}
        return {"calendars": {"primary": {"busy": self.busy}}}


def planning_group(database):
    account = database.accounts.insert_one({"username": "alex"}).inserted_id
    member = database.users.insert_one({
        "display_name": "Alex", "normalized_name": "alex", "linked_account_id": account,
    }).inserted_id
    group = database.groups.insert_one({"name": "Test", "member_ids": [member]}).inserted_id
    database.availability.insert_one({
        "user_id": member, "day": "Monday", "day_order": 0,
        "start_time": "09:00", "end_time": "12:00", "status": "available",
    })
    return account, member, group


def preview(client, group, **overrides):
    fields = {
        "start_date": "2026-11-02", "end_date": "2026-11-02",
        "duration_minutes": "60", "minimum_attendance": "1",
        "earliest_time": "09:00", "latest_time": "12:00",
    }
    fields.update(overrides)
    return client.post(f"/groups/{group}/schedule-preview", data=fields)


def test_naive_mongo_timestamp_does_not_crash_plan_preview(calendar_app, monkeypatch):
    app, database = calendar_app
    account, _member, group = planning_group(database)
    database.calendar_connections.insert_one({
        "account_id": account, "provider": "ical", "sync_status": "connected",
        "secret_ciphertext": encrypted({"url": "https://example.test/one.ics"}),
        "last_synced_at": datetime(2026, 11, 1, 20),
        "cache_start": datetime(2026, 11, 2, 8),
        "cache_end": datetime(2026, 11, 3, 8),
        "busy_intervals": [],
    })
    monkeypatch.setattr("app.fetch_calendar_url", lambda _url: (_ for _ in ()).throw(AssertionError("fresh cache fetched")))
    response = preview(app.test_client(), group)
    assert response.status_code == 200
    assert response.get_json()["results"]


def test_google_and_ical_busy_times_are_both_subtracted(calendar_app, monkeypatch):
    app, database = calendar_app
    account, _member, group = planning_group(database)
    database.calendar_connections.insert_many([
        {"account_id": account, "provider": "google", "sync_status": "pending",
         "secret_ciphertext": encrypted({"access_token": "test-token"})},
        {"account_id": account, "provider": "ical", "source_key": "feed-one",
         "sync_status": "pending",
         "secret_ciphertext": encrypted({"url": "https://example.test/one.ics"})},
    ])
    monkeypatch.setattr("app.fetch_calendar_url", lambda _url: feed(start="110000", end="120000"))
    monkeypatch.setattr("app.requests.post", lambda *_args, **_kwargs: FreeBusyResponse([{
        "start": "2026-11-02T17:00:00Z", "end": "2026-11-02T19:00:00Z",
    }]))
    response = preview(app.test_client(), group)
    assert response.status_code == 200
    assert response.get_json()["results"] == []
    assert database.calendar_connections.count_documents({"sync_status": "connected"}) == 2


def test_failed_refresh_uses_covered_cache_and_warns(calendar_app, monkeypatch):
    app, database = calendar_app
    account, _member, group = planning_group(database)
    database.calendar_connections.insert_one({
        "account_id": account, "provider": "google", "sync_status": "connected",
        "secret_ciphertext": encrypted({"access_token": "expired"}),
        "last_synced_at": datetime(2026, 10, 30),
        "cache_start": datetime(2026, 11, 2, 8),
        "cache_end": datetime(2026, 11, 3, 8),
        "busy_intervals": [{"start": datetime(2026, 11, 2, 18),
                            "end": datetime(2026, 11, 2, 19)}],
    })
    monkeypatch.setattr("app.requests.post", lambda *_args, **_kwargs: FreeBusyResponse(status=503))
    response = preview(app.test_client(), group, earliest_time="10:00")
    assert response.status_code == 200
    assert response.get_json()["calendar_warnings"]
    assert response.get_json()["results"][0]["start"] == "11:00"


def test_failed_refresh_without_covered_cache_blocks_planning(calendar_app, monkeypatch):
    app, database = calendar_app
    account, _member, group = planning_group(database)
    database.calendar_connections.insert_one({
        "account_id": account, "provider": "google", "sync_status": "pending",
        "secret_ciphertext": encrypted({"access_token": "expired"}),
    })
    monkeypatch.setattr("app.requests.post", lambda *_args, **_kwargs: FreeBusyResponse(status=503))
    response = preview(app.test_client(), group)
    assert response.status_code == 400
    assert "could not sync" in response.get_json()["errors"][0]


def test_google_refresh_and_chunking(calendar_app, monkeypatch):
    app, database = calendar_app
    account, _member, group = planning_group(database)
    connection_id = database.calendar_connections.insert_one({
        "account_id": account, "provider": "google", "sync_status": "pending",
        "secret_ciphertext": encrypted({"access_token": "expired", "refresh_token": "refresh"}),
    }).inserted_id
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/token"):
            return type("Refresh", (), {"raise_for_status": lambda self: None,
                                          "json": lambda self: {"access_token": "new-token"}})()
        if kwargs["headers"]["Authorization"] == "Bearer expired":
            return FreeBusyResponse(status=401)
        return FreeBusyResponse()

    monkeypatch.setattr("app.requests.post", post)
    response = preview(app.test_client(), group, end_date="2027-01-05")
    assert response.status_code == 200
    assert len([call for call in calls if call[0].endswith("/freeBusy")]) >= 4
    saved = database.calendar_connections.find_one({"_id": connection_id})
    assert saved["sync_status"] == "connected"
    assert saved["cache_end"].date() >= datetime(2027, 1, 6).date()


def test_google_response_calendar_error_is_not_treated_as_empty(calendar_app, monkeypatch):
    app, database = calendar_app
    account, _member, group = planning_group(database)
    database.calendar_connections.insert_one({
        "account_id": account, "provider": "google", "sync_status": "pending",
        "secret_ciphertext": encrypted({"access_token": "test-token"}),
    })
    monkeypatch.setattr("app.requests.post", lambda *_args, **_kwargs: FreeBusyResponse(errors=[{"reason": "notFound"}]))
    response = preview(app.test_client(), group)
    assert response.status_code == 400
    assert database.calendar_connections.find_one({"account_id": account})["sync_status"] == "error"


def test_multiple_ical_feeds_and_duplicate_url_update(calendar_app, monkeypatch):
    app, database = calendar_app
    app.config["LOGIN_DISABLED"] = False
    client = app.test_client()
    client.post("/register", data={
        "username": "calendar-owner", "password": "correct-horse",
        "confirm_password": "correct-horse",
    })
    monkeypatch.setattr("app.fetch_calendar_url", lambda _url: feed())
    for name, url in (("Work", "https://example.test/work.ics"),
                      ("Family", "https://example.test/family.ics")):
        assert client.post("/account/calendars/ical", data={
            "calendar_name": name, "calendar_url": url,
        }).status_code == 302
    assert database.calendar_connections.count_documents({"provider": "ical"}) == 2
    assert {item["display_name"] for item in database.calendar_connections.find({"provider": "ical"})} == {
        "Work", "Family",
    }
    client.post("/account/calendars/ical", data={
        "calendar_name": "Work calendar", "calendar_url": "https://example.test/work.ics",
    })
    assert database.calendar_connections.count_documents({"provider": "ical"}) == 2
    assert b"Work calendar" in client.get("/account").data


def test_readding_legacy_ical_url_upgrades_connection(calendar_app, monkeypatch):
    app, database = calendar_app
    app.config["LOGIN_DISABLED"] = False
    client = app.test_client()
    client.post("/register", data={
        "username": "legacy-owner", "password": "correct-horse",
        "confirm_password": "correct-horse",
    })
    account = database.accounts.find_one({"normalized_username": "legacy-owner"})
    original_id = database.calendar_connections.insert_one({
        "account_id": account["_id"], "provider": "ical",
        "secret_ciphertext": encrypted({"url": "https://example.test/legacy.ics"}),
    }).inserted_id
    monkeypatch.setattr("app.fetch_calendar_url", lambda _url: feed())
    client.post("/account/calendars/ical", data={
        "calendar_name": "Legacy", "calendar_url": "https://example.test/legacy.ics",
    })
    assert database.calendar_connections.count_documents({"account_id": account["_id"]}) == 1
    upgraded = database.calendar_connections.find_one({"_id": original_id})
    assert upgraded["display_name"] == "Legacy"
    assert upgraded["source_key"]


def test_five_member_november_plan_survives_naive_cached_calendar(calendar_app, monkeypatch):
    app, database = calendar_app
    account, member, group = planning_group(database)
    others = [database.users.insert_one({
        "display_name": f"Member {index}", "normalized_name": f"member-{index}",
    }).inserted_id for index in range(4)]
    database.groups.update_one({"_id": group}, {"$push": {"member_ids": {"$each": others}}})
    database.availability.insert_many({
        "user_id": other, "day": "Monday", "day_order": 0,
        "start_time": "09:00", "end_time": "12:00", "status": "available",
    } for other in others)
    database.calendar_connections.insert_one({
        "account_id": account, "provider": "ical", "sync_status": "connected",
        "secret_ciphertext": encrypted({"url": "https://example.test/work.ics"}),
        "last_synced_at": datetime(2026, 11, 1, 20),
        "cache_start": datetime(2026, 11, 1, 7),
        "cache_end": datetime(2026, 11, 9, 8),
        "busy_intervals": [{"start": datetime(2026, 11, 2, 18),
                            "end": datetime(2026, 11, 2, 19)}],
    })
    monkeypatch.setattr("app.fetch_calendar_url", lambda _url: (_ for _ in ()).throw(AssertionError("fresh cache fetched")))
    fields = {
        "title": "Test Plan", "start_date": "2026-11-01", "end_date": "2026-11-08",
        "duration_minutes": "60", "minimum_attendance": "5",
    }
    client = app.test_client()
    preview_response = client.post(f"/groups/{group}/schedule-preview", data=fields)
    create_response = client.post(f"/groups/{group}/plans/create", data=fields)
    assert preview_response.status_code == 200
    assert create_response.status_code == 302
    plan = database.plans.find_one({"title": "Test Plan"})
    assert plan["matches"]
    assert plan["matches"][0]["attendance_count"] == 5
    assert plan["matches"][0]["start_time"] == "09:00"


def test_scheduling_rechecks_calendar_after_plan_was_previewed(calendar_app, monkeypatch):
    app, database = calendar_app
    account, _member, group = planning_group(database)
    connection_id = database.calendar_connections.insert_one({
        "account_id": account, "provider": "ical", "sync_status": "connected",
        "secret_ciphertext": encrypted({"url": "https://example.test/work.ics"}),
        "last_synced_at": datetime(2026, 11, 1, 20),
        "cache_start": datetime(2026, 11, 2, 8),
        "cache_end": datetime(2026, 11, 3, 8),
        "busy_intervals": [],
    }).inserted_id
    client = app.test_client()
    client.post(f"/groups/{group}/plans/create", data={
        "title": "Dinner", "start_date": "2026-11-02", "end_date": "2026-11-02",
        "duration_minutes": "60", "minimum_attendance": "1",
    })
    plan = database.plans.find_one({"title": "Dinner"})
    assert plan["matches"]
    database.calendar_connections.update_one({"_id": connection_id}, {
        "$set": {"last_synced_at": datetime(2026, 10, 30)},
    })
    monkeypatch.setattr("app.fetch_calendar_url", lambda _url: feed(start="090000", end="120000"))
    response = client.post(f"/plans/{plan['_id']}/schedule", data={"match_index": "0"})
    assert response.status_code == 302
    updated = database.plans.find_one({"_id": plan["_id"]})
    assert updated["status"] == "planning"
    assert updated["matches"] == []


def test_google_reconnect_keeps_refresh_token(calendar_app, monkeypatch):
    app, database = calendar_app
    app.config["LOGIN_DISABLED"] = False
    client = app.test_client()
    client.post("/register", data={
        "username": "google-owner", "password": "correct-horse",
        "confirm_password": "correct-horse",
    })
    account = database.accounts.find_one({"normalized_username": "google-owner"})
    monkeypatch.setattr("app.requests.post", lambda *_args, **_kwargs: FreeBusyResponse())

    class OAuthClient:
        token = {"access_token": "first", "refresh_token": "durable-refresh"}

        def authorize_access_token(self):
            return self.token

    oauth = OAuthClient()
    app.extensions["google_calendar_oauth_client"] = oauth
    for access in ("first", "second"):
        oauth.token = {"access_token": access}
        if access == "first":
            oauth.token["refresh_token"] = "durable-refresh"
        with client.session_transaction() as oauth_session:
            oauth_session["google_calendar_oauth_state"] = "state"
            oauth_session["google_calendar_oauth_account_id"] = str(account["_id"])
        response = client.get("/auth/google-calendar/callback?state=state")
        assert response.status_code == 302
    connection = database.calendar_connections.find_one({"account_id": account["_id"]})
    key = base64.urlsafe_b64encode(hashlib.sha256(b"test-secret").digest())
    saved = json.loads(Fernet(key).decrypt(connection["secret_ciphertext"].encode()))
    assert saved == {"access_token": "second", "refresh_token": "durable-refresh"}


def test_google_callback_rejects_session_from_another_account(calendar_app):
    app, database = calendar_app
    app.config["LOGIN_DISABLED"] = False
    client = app.test_client()
    client.post("/register", data={
        "username": "calendar-owner", "password": "correct-horse",
        "confirm_password": "correct-horse",
    })
    with client.session_transaction() as oauth_session:
        oauth_session["google_calendar_oauth_state"] = "state"
        oauth_session["google_calendar_oauth_account_id"] = "different-account"
    response = client.get("/auth/google-calendar/callback?state=state")
    assert response.status_code == 302
    assert database.calendar_connections.count_documents({}) == 0


def test_failed_calendar_refresh_preserves_confirmed_plan(calendar_app, monkeypatch):
    app, database = calendar_app
    account, member, group = planning_group(database)
    database.calendar_connections.insert_one({
        "account_id": account, "provider": "google", "sync_status": "error",
        "secret_ciphertext": encrypted({"access_token": "expired"}),
    })
    plan_id = database.plans.insert_one({
        "group_id": group, "title": "Dinner", "status": "scheduled",
        "invited_member_ids": [member],
        "start_date": "2026-11-02", "end_date": "2026-11-02",
        "duration_minutes": 60, "minimum_attendance": 1,
        "scheduled_start": datetime(2026, 11, 2, 9),
        "scheduled_end": datetime(2026, 11, 2, 10),
    }).inserted_id
    monkeypatch.setattr("app.requests.post", lambda *_args, **_kwargs: FreeBusyResponse(status=503))
    response = app.test_client().post(f"/plans/{plan_id}/find-times")
    assert response.status_code == 302
    saved = database.plans.find_one({"_id": plan_id})
    assert saved["status"] == "scheduled"
    assert saved["scheduled_start"] == datetime(2026, 11, 2, 9)


def test_uncovered_calendar_blocks_scheduling_saved_option(calendar_app, monkeypatch):
    app, database = calendar_app
    account, member, group = planning_group(database)
    database.calendar_connections.insert_one({
        "account_id": account, "provider": "google", "sync_status": "pending",
        "secret_ciphertext": encrypted({"access_token": "expired"}),
    })
    plan_id = database.plans.insert_one({
        "group_id": group, "title": "Dinner", "status": "planning",
        "invited_member_ids": [member],
        "start_date": "2026-11-02", "end_date": "2026-11-02",
        "duration_minutes": 60, "minimum_attendance": 1,
        "matches": [{"date": "2026-11-02", "start_time": "09:00",
                     "end_time": "10:00", "is_exact": True}],
    }).inserted_id
    monkeypatch.setattr("app.requests.post", lambda *_args, **_kwargs: FreeBusyResponse(status=503))
    response = app.test_client().post(f"/plans/{plan_id}/schedule", data={"match_index": "0"})
    assert response.status_code == 302
    assert database.plans.find_one({"_id": plan_id})["status"] == "planning"
