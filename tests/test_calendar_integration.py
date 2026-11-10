"""Calendar connection and planning behavior with provider calls mocked."""

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from cryptography.fernet import Fernet
from services.calendar_service import CalendarFeedError

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


def test_google_busy_time_updates_group_and_member_views(calendar_app, monkeypatch):
    app, database = calendar_app
    account, member, group = planning_group(database)
    database.calendar_connections.insert_one({
        "account_id": account, "provider": "google", "sync_status": "pending",
        "secret_ciphertext": encrypted({"access_token": "test-token", "refresh_token": "refresh"}),
    })
    monkeypatch.setattr("app.requests.post", lambda *_args, **_kwargs: FreeBusyResponse([{
        "start": "2026-11-02T18:00:00Z", "end": "2026-11-02T19:00:00Z",
    }]))

    group_page = app.test_client().get(f"/groups/{group}?week_start=2026-11-02").get_data(as_text=True)
    member_page = app.test_client().get(
        f"/users/{member}/availability?week_start=2026-11-02"
    ).get_data(as_text=True)

    assert "member-overlay-conflict" in group_page
    assert 'data-calendar-mask' in member_page
    assert 'data-start-minute="600" data-end-minute="660"' in member_page
    assert 'aria-label="Actually free on Monday, November 2 from 9:00 AM to 10:00 AM"' in member_page
    assert 'aria-label="Actually free on Monday, November 2 from 11:00 AM to 12:00 PM"' in member_page


def test_member_chart_keeps_calendar_sources_separate_and_private(calendar_app):
    app, database = calendar_app
    account, member, _group = planning_group(database)
    cached = {
        "account_id": account, "sync_status": "connected",
        "last_synced_at": datetime(2026, 11, 1, 20, tzinfo=timezone.utc),
        "cache_start": datetime(2026, 11, 2, 8, tzinfo=timezone.utc),
        "cache_end": datetime(2026, 11, 9, 8, tzinfo=timezone.utc),
    }
    database.calendar_connections.insert_many([
        {**cached, "provider": "google", "busy_intervals": [{
            "start": datetime(2026, 11, 2, 18), "end": datetime(2026, 11, 2, 19),
            "summary": "Private doctor visit",
        }]},
        {**cached, "provider": "ical", "display_name": "School", "busy_intervals": [{
            "start": datetime(2026, 11, 2, 18, 30), "end": datetime(2026, 11, 2, 19, 30),
        }]},
    ])
    page = app.test_client().get(
        f"/users/{member}/availability?week_start=2026-11-02"
    ).get_data(as_text=True)

    assert "Google Calendar" in page and "School" in page
    assert 'data-start-minute="600" data-end-minute="660"' in page
    assert 'data-start-minute="630" data-end-minute="690"' in page
    assert 'aria-label="Actually free on Monday, November 2 from 11:30 AM to 12:00 PM"' in page
    assert "Private doctor visit" not in page


def test_busy_labels_and_checkboxes_identify_each_calendar(calendar_app):
    app, database = calendar_app
    account, member, _group = planning_group(database)
    interval = {"start": datetime(2026, 11, 2, 18), "end": datetime(2026, 11, 2, 18, 50)}
    cached = {
        "account_id": account, "sync_status": "connected", "busy_intervals": [interval],
        "last_synced_at": datetime(2026, 11, 1, 20, tzinfo=timezone.utc),
        "cache_start": datetime(2026, 11, 2, 8, tzinfo=timezone.utc),
        "cache_end": datetime(2026, 11, 9, 8, tzinfo=timezone.utc),
    }
    database.calendar_connections.insert_many([
        {**cached, "provider": "google", "secret_ciphertext": encrypted({
            "selected_calendars": [{"id": "primary", "name": "School"}, {"id": "secondary", "name": "Personal"}],
        }), "busy_sources": [{"busy_intervals": [interval]}, {"busy_intervals": []}]},
        {**cached, "provider": "ical", "source_key": "work-one", "display_name": "Work"},
        {**cached, "provider": "ical", "source_key": "work-two", "display_name": "Work"},
        {**cached, "provider": "ical", "source_key": "unnamed"},
    ])
    page = app.test_client().get(f"/users/{member}/availability?week_start=2026-11-02").get_data(as_text=True)
    assert 'BUSY (Google Calendar · School)' in page
    assert 'Google Calendar · Personal</span>' in page  # Empty calendars still have a checkbox.
    assert 'BUSY (Work)' in page and 'BUSY (Work (2))' in page
    assert 'BUSY (iCal calendar)' in page
    assert page.count('type="checkbox" data-calendar-filter=') == 5
    assert page.count('aria-describedby="calendar-display-help" checked') == 5
    assert '10:00 AM – 10:50 AM</span>' in page
    assert 'Actually free time still accounts for every connected calendar' in page
    assert 'Set availability</span>' in page


def test_busy_block_labels_use_clipped_local_times_at_midnight(calendar_app):
    app, database = calendar_app
    account, member, _group = planning_group(database)
    database.calendar_connections.insert_one({
        "account_id": account, "provider": "ical", "display_name": "Night shift",
        "sync_status": "connected", "last_synced_at": datetime(2026, 11, 1, 20, tzinfo=timezone.utc),
        "cache_start": datetime(2026, 11, 2, 8, tzinfo=timezone.utc),
        "cache_end": datetime(2026, 11, 9, 8, tzinfo=timezone.utc),
        "busy_intervals": [{"start": datetime(2026, 11, 3, 7, 45), "end": datetime(2026, 11, 3, 8, 15)}],
    })
    page = app.test_client().get(f"/users/{member}/availability?week_start=2026-11-02").get_data(as_text=True)
    assert 'data-start-minute="1425" data-end-minute="1440"' in page
    assert 'data-start-minute="0" data-end-minute="15"' in page
    assert '11:45 PM – 12:00 AM</span>' in page
    assert '12:00 AM – 12:15 AM</span>' in page


def test_member_chart_shows_busy_time_outside_recurring_window(calendar_app):
    app, database = calendar_app
    account, member, _group = planning_group(database)
    database.calendar_connections.insert_one({
        "account_id": account, "provider": "google", "sync_status": "connected",
        "last_synced_at": datetime(2026, 11, 1, 20, tzinfo=timezone.utc),
        "cache_start": datetime(2026, 11, 2, 8, tzinfo=timezone.utc),
        "cache_end": datetime(2026, 11, 9, 8, tzinfo=timezone.utc),
        "busy_intervals": [{"start": datetime(2026, 11, 2, 14),
                            "end": datetime(2026, 11, 2, 15)}],
    })
    page = app.test_client().get(
        f"/users/{member}/availability?week_start=2026-11-02"
    ).get_data(as_text=True)

    assert 'data-start-minute="360" data-end-minute="420"' in page
    assert 'aria-label="Actually free on Monday, November 2 from 9:00 AM to 12:00 PM"' in page


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
    client = app.test_client()
    empty = preview(client, group, earliest_time="10:00", latest_time="11:00").get_json()
    assert empty["diagnostic"]["reason"] == "no_availability"
    assert empty["calendar_warnings"]
    created = client.post(f"/groups/{group}/plans/create", data={
        "title": "Busy calendar", "start_date": "2026-11-02", "end_date": "2026-11-02",
        "duration_minutes": "60", "minimum_attendance": "1",
        "earliest_time": "10:00", "latest_time": "11:00",
    }, follow_redirects=True)
    assert b"No availability overlaps" in created.data
    assert b"using its last successful busy times" in created.data


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


def test_account_shows_google_ical_guide_without_exposing_subscription_url(calendar_app):
    app, database = calendar_app
    app.config["LOGIN_DISABLED"] = False
    client = app.test_client()
    client.post("/register", data={
        "username": "guide-owner", "password": "correct-horse",
        "confirm_password": "correct-horse",
    })
    account = database.accounts.find_one({"normalized_username": "guide-owner"})
    secret_url = "https://calendar.google.com/calendar/ical/owner/private-secret/basic.ics"
    database.calendar_connections.insert_one({
        "account_id": account["_id"], "provider": "ical",
        "display_name": "Personal", "sync_status": "connected",
        "secret_ciphertext": encrypted({"url": secret_url}),
    })

    account_page = client.get("/account").get_data(as_text=True)
    privacy_page = client.get("/privacy").get_data(as_text=True)

    assert '<details class="ical-help">' in account_page
    assert "How to add a Google calendar with iCal" in account_page
    assert "Secret address" in account_page and "Public free/busy address" in account_page
    assert "name=\"calendar_url\"" in account_page
    assert secret_url not in account_page
    assert "We encrypt the subscription URL" in privacy_page


@pytest.mark.parametrize("category, status, message", [
    ("rate_limited", 429, b"limiting requests (HTTP 429)"),
    ("not_found", 404, b"not found (HTTP 404)"),
])
def test_ical_provider_failure_is_explained_without_saving_or_exposing_url(
    calendar_app, monkeypatch, caplog, category, status, message,
):
    app, database = calendar_app
    app.config["LOGIN_DISABLED"] = False
    client = app.test_client()
    client.post("/register", data={
        "username": "feed-owner", "password": "correct-horse",
        "confirm_password": "correct-horse",
    })
    secret_url = "https://calendar.google.com/calendar/ical/owner/private-secret/basic.ics"
    monkeypatch.setattr("app.fetch_calendar_url", lambda _url: (_ for _ in ()).throw(
        CalendarFeedError(category, "Calendar provider failed.", status)
    ))

    response = client.post("/account/calendars/ical", data={
        "calendar_name": "Personal", "calendar_url": secret_url,
    }, follow_redirects=True)

    assert response.status_code == 200
    assert message in response.data
    assert database.calendar_connections.count_documents({"provider": "ical"}) == 0
    assert secret_url.encode() not in response.data
    assert secret_url not in caplog.text
    assert f"category={category} status={status}" in caplog.text


def test_ical_rate_limit_keeps_covered_cache_without_repeated_fetches(calendar_app, monkeypatch):
    app, database = calendar_app
    account, _member, group = planning_group(database)
    connection_id = database.calendar_connections.insert_one({
        "account_id": account, "provider": "ical", "sync_status": "connected",
        "secret_ciphertext": encrypted({"url": "https://example.test/secret.ics"}),
        "last_synced_at": datetime(2026, 10, 30),
        "cache_start": datetime(2026, 11, 2, 8),
        "cache_end": datetime(2026, 11, 3, 8),
        "busy_intervals": [{"start": datetime(2026, 11, 2, 18),
                            "end": datetime(2026, 11, 2, 19)}],
    }).inserted_id
    calls = []

    def rate_limited(_url):
        calls.append(True)
        raise CalendarFeedError("rate_limited", "Calendar provider limited requests.", 429)

    monkeypatch.setattr("app.fetch_calendar_url", rate_limited)
    first = preview(app.test_client(), group, earliest_time="10:00")
    second = preview(app.test_client(), group, earliest_time="10:00")

    assert first.status_code == second.status_code == 200
    assert first.get_json()["results"][0]["start"] == "11:00"
    assert second.get_json()["calendar_warnings"]
    assert len(calls) == 1
    uncovered = preview(app.test_client(), group, start_date="2026-11-03", end_date="2026-11-03")
    assert uncovered.status_code == 400
    assert "could not sync" in uncovered.get_json()["errors"][0]
    assert len(calls) == 1
    connection = database.calendar_connections.find_one({"_id": connection_id})
    assert connection["sync_error"] == "The calendar provider is limiting requests (HTTP 429). Try again later."
    assert connection["next_sync_attempt_at"] > datetime(2026, 11, 1, 20)


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
    assert saved == {"access_token": "second", "refresh_token": "durable-refresh",
                     "selected_calendars": [{"id": "primary", "name": "Primary calendar"}]}


def test_select_secondary_google_calendar_and_preserve_cache_on_failure(calendar_app, monkeypatch):
    app, database = calendar_app
    app.config["LOGIN_DISABLED"] = False
    client = app.test_client()
    client.post("/register", data={
        "username": "calendar-owner", "password": "correct-horse",
        "confirm_password": "correct-horse",
    })
    account = database.accounts.find_one({"normalized_username": "calendar-owner"})
    member = database.users.insert_one({
        "display_name": "Calendar Owner", "account_id": account["_id"],
        "linked_account_id": account["_id"],
    }).inserted_id
    database.availability.insert_one({
        "user_id": member, "day": "Monday", "start_time": "09:00", "end_time": "12:00",
    })
    connection_id = database.calendar_connections.insert_one({
        "account_id": account["_id"], "provider": "google", "sync_status": "pending",
        "secret_ciphertext": encrypted({"access_token": "access", "refresh_token": "refresh"}),
    }).inserted_id

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    monkeypatch.setattr("app.requests.get", lambda *_args, **_kwargs: Response({"items": [
        {"id": "primary@example.com", "summary": "Primary", "primary": True},
        {"id": "school@example.com", "summary": "School", "primary": False},
    ]}))
    failed = False

    def freebusy(_url, **kwargs):
        items = kwargs["json"]["items"]
        return Response({"calendars": {
            item["id"]: ({"errors": [{"reason": "notFound"}]} if failed else {
                "busy": [{"start": "2026-11-02T18:00:00Z", "end": "2026-11-02T19:00:00Z"}]
                if item["id"] == "school@example.com" else []
            }) for item in items
        }})

    monkeypatch.setattr("app.requests.post", freebusy)
    chooser = client.get("/account/calendars/google/selection")
    assert b"School" in chooser.data and b"Primary" in chooser.data
    invalid = client.post("/account/calendars/google/selection", data={"calendar_id": "unlisted@example.com"})
    assert invalid.status_code == 400
    assert database.calendar_connections.find_one({"_id": connection_id})["sync_status"] == "pending"
    response = client.post("/account/calendars/google/selection", data={"calendar_id": "school@example.com"})
    assert response.status_code == 302
    connection = database.calendar_connections.find_one({"_id": connection_id})
    saved = json.loads(Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"test-secret").digest())).decrypt(
        connection["secret_ciphertext"].encode()))
    assert saved["selected_calendars"] == [{"id": "school@example.com", "name": "School"}]
    assert len(connection["busy_intervals"]) == 1
    assert len(connection["busy_sources"]) == 1
    page = client.get(f"/users/{member}/availability?week_start=2026-11-02").get_data(as_text=True)
    assert "School busy" in page

    failed = True
    client.post("/account/calendars/google/selection", data={
        "calendar_id": ["primary@example.com", "school@example.com"],
    })
    unchanged = database.calendar_connections.find_one({"_id": connection_id})
    assert unchanged["busy_intervals"] == connection["busy_intervals"]
    assert unchanged["busy_sources"] == connection["busy_sources"]
    saved_after_failure = json.loads(Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"test-secret").digest())).decrypt(
        unchanged["secret_ciphertext"].encode()))
    assert saved_after_failure["selected_calendars"] == saved["selected_calendars"]


def test_multiple_selected_google_calendars_merge_for_scheduling(calendar_app, monkeypatch):
    app, database = calendar_app
    account, member, group = planning_group(database)
    database.calendar_connections.insert_one({
        "account_id": account, "provider": "google", "sync_status": "pending",
        "secret_ciphertext": encrypted({
            "access_token": "access", "refresh_token": "refresh",
            "selected_calendars": [
                {"id": "primary", "name": "Primary"},
                {"id": "school@example.com", "name": "School"},
            ],
        }),
    })

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"calendars": {
                "primary": {"busy": [{"start": "2026-11-02T17:30:00Z", "end": "2026-11-02T18:30:00Z"}]},
                "school@example.com": {"busy": [{"start": "2026-11-02T18:00:00Z", "end": "2026-11-02T19:30:00Z"}]},
            }}

    monkeypatch.setattr("app.requests.post", lambda *_args, **_kwargs: Response())
    response = preview(app.test_client(), group)
    connection = database.calendar_connections.find_one({"account_id": account})

    assert response.status_code == 200
    assert len(connection["busy_sources"]) == 2
    assert len(connection["busy_intervals"]) == 1
    assert connection["busy_intervals"][0]["start"] == datetime(2026, 11, 2, 17, 30)
    assert connection["busy_intervals"][0]["end"] == datetime(2026, 11, 2, 19, 30)
    assert {item["start"] for item in response.get_json()["results"]} == {"09:00", "11:30"}


def test_other_group_owner_sees_busy_mask_without_calendar_name(calendar_app):
    app, database = calendar_app
    app.config["LOGIN_DISABLED"] = False
    client = app.test_client()
    client.post("/register", data={
        "username": "group-owner", "password": "correct-horse",
        "confirm_password": "correct-horse",
    })
    owner = database.accounts.find_one({"normalized_username": "group-owner"})
    member_account = database.accounts.insert_one({"username": "member"}).inserted_id
    member = database.users.insert_one({
        "display_name": "Member", "linked_account_id": member_account,
    }).inserted_id
    database.groups.insert_one({
        "name": "Group", "owner_account_id": owner["_id"],
        "account_ids": [owner["_id"]], "member_ids": [member],
    })
    database.availability.insert_one({
        "user_id": member, "day": "Monday", "start_time": "09:00", "end_time": "12:00",
    })
    interval = {"start": datetime(2026, 11, 2, 18), "end": datetime(2026, 11, 2, 19)}
    database.calendar_connections.insert_one({
        "account_id": member_account, "provider": "google", "sync_status": "connected",
        "secret_ciphertext": encrypted({
            "access_token": "access", "refresh_token": "refresh",
            "selected_calendars": [{"id": "school@example.com", "name": "Private School Calendar"}],
        }),
        "busy_sources": [{"busy_intervals": [interval]}], "busy_intervals": [interval],
        "last_synced_at": datetime(2026, 11, 1, 20, tzinfo=timezone.utc),
        "cache_start": datetime(2026, 11, 2, 8, tzinfo=timezone.utc),
        "cache_end": datetime(2026, 11, 9, 8, tzinfo=timezone.utc),
    })

    page = client.get(f"/users/{member}/availability?week_start=2026-11-02").get_data(as_text=True)
    assert "Connected calendar busy" in page
    assert "Private School Calendar" not in page
    assert "school@example.com" not in page


def test_created_member_does_not_inherit_creators_calendar(calendar_app):
    app, database = calendar_app
    account = database.accounts.insert_one({"username": "owner"}).inserted_id
    member = database.users.insert_one({
        "display_name": "Another person", "account_id": account,
    }).inserted_id
    group = database.groups.insert_one({"name": "Group", "member_ids": [member]}).inserted_id
    database.availability.insert_one({
        "user_id": member, "day": "Monday", "start_time": "09:00", "end_time": "12:00",
    })
    database.calendar_connections.insert_one({
        "account_id": account, "provider": "google", "sync_status": "connected",
        "busy_intervals": [{"start": datetime(2026, 11, 2, 17),
                            "end": datetime(2026, 11, 2, 20)}],
    })
    response = preview(app.test_client(), group)
    assert response.status_code == 200
    assert response.get_json()["results"][0]["start"] == "09:00"


def test_google_calendar_requires_refresh_token_for_new_connection(calendar_app):
    app, database = calendar_app
    app.config["LOGIN_DISABLED"] = False
    client = app.test_client()
    client.post("/register", data={
        "username": "calendar-owner", "password": "correct-horse",
        "confirm_password": "correct-horse",
    })
    account = database.accounts.find_one({"normalized_username": "calendar-owner"})

    class OAuthClient:
        def authorize_access_token(self):
            return {"access_token": "temporary-only"}

    app.extensions["google_calendar_oauth_client"] = OAuthClient()
    with client.session_transaction() as oauth_session:
        oauth_session["google_calendar_oauth_state"] = "state"
        oauth_session["google_calendar_oauth_account_id"] = str(account["_id"])
    response = client.get("/auth/google-calendar/callback?state=state", follow_redirects=True)

    assert b"did not grant ongoing access" in response.data
    assert database.calendar_connections.count_documents({"account_id": account["_id"]}) == 0


def test_calendar_reauthorization_denial_keeps_existing_connection(calendar_app):
    app, database = calendar_app
    app.config["LOGIN_DISABLED"] = False
    client = app.test_client()
    client.post("/register", data={
        "username": "calendar-owner", "password": "correct-horse",
        "confirm_password": "correct-horse",
    })
    account = database.accounts.find_one({"normalized_username": "calendar-owner"})
    connection_id = database.calendar_connections.insert_one({
        "account_id": account["_id"], "provider": "google", "sync_status": "connected",
        "secret_ciphertext": encrypted({"access_token": "old", "refresh_token": "old-refresh"}),
        "busy_intervals": [{"start": datetime(2026, 11, 2, 18), "end": datetime(2026, 11, 2, 19)}],
    }).inserted_id
    before = database.calendar_connections.find_one({"_id": connection_id})
    with client.session_transaction() as oauth_session:
        oauth_session["google_calendar_oauth_state"] = "state"
        oauth_session["google_calendar_oauth_account_id"] = str(account["_id"])
    denied = client.get("/auth/google-calendar/callback?state=state&error=access_denied")
    assert denied.status_code == 302
    assert database.calendar_connections.find_one({"_id": connection_id}) == before

    class OAuthClient:
        def authorize_access_token(self):
            return {"access_token": "new", "refresh_token": "new-refresh",
                    "scope": "https://www.googleapis.com/auth/calendar.events.freebusy"}

    app.extensions["google_calendar_oauth_client"] = OAuthClient()
    with client.session_transaction() as oauth_session:
        oauth_session["google_calendar_oauth_state"] = "state"
        oauth_session["google_calendar_oauth_account_id"] = str(account["_id"])
    missing_scope = client.get("/auth/google-calendar/callback?state=state")
    assert missing_scope.status_code == 302
    assert database.calendar_connections.find_one({"_id": connection_id}) == before


def test_empty_successful_google_sync_is_explained_on_account_page(calendar_app):
    app, database = calendar_app
    app.config["LOGIN_DISABLED"] = False
    client = app.test_client()
    client.post("/register", data={
        "username": "calendar-owner", "password": "correct-horse",
        "confirm_password": "correct-horse",
    })
    account = database.accounts.find_one({"normalized_username": "calendar-owner"})
    database.calendar_connections.insert_one({
        "account_id": account["_id"], "provider": "google", "sync_status": "connected",
        "secret_ciphertext": encrypted({"access_token": "access", "refresh_token": "refresh",
                                        "selected_calendars": [{"id": "school@example.com", "name": "School"}]}),
        "busy_intervals": [],
        "last_synced_at": datetime(2026, 11, 1, 20, tzinfo=timezone.utc),
        "cache_start": datetime(2026, 11, 2, 8, tzinfo=timezone.utc),
        "cache_end": datetime(2026, 11, 9, 8, tzinfo=timezone.utc),
    })
    page = client.get("/account").get_data(as_text=True)
    assert "0 busy periods found" in page
    assert "Selected: School" in page
    assert "Choose calendars" in page


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
