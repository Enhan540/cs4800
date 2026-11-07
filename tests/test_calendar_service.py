from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from services.calendar_service import (
    CalendarFeedError, fetch_calendar_url, normalized_interval,
    parse_ical_busy_intervals, safe_calendar_url,
)


def test_cancelled_and_transparent_events_do_not_block_time():
    payload = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:transparent@test
DTSTART:20261102T170000Z
DTEND:20261102T180000Z
TRANSP:TRANSPARENT
END:VEVENT
BEGIN:VEVENT
UID:cancelled@test
DTSTART:20261102T180000Z
DTEND:20261102T190000Z
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
UID:busy@test
DTSTART:20261102T190000Z
DTEND:20261102T200000Z
END:VEVENT
END:VCALENDAR
"""
    assert parse_ical_busy_intervals(
        payload, start=datetime(2026, 11, 2, tzinfo=timezone.utc),
        end=datetime(2026, 11, 3, tzinfo=timezone.utc),
    ) == [{"start": datetime(2026, 11, 2, 19), "end": datetime(2026, 11, 2, 20)}]


def test_cancelled_recurrence_instance_does_not_block_time():
    payload = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:weekly@test
DTSTART:20261102T190000Z
DTEND:20261102T200000Z
RRULE:FREQ=DAILY;COUNT=2
END:VEVENT
BEGIN:VEVENT
UID:weekly@test
RECURRENCE-ID:20261103T190000Z
DTSTART:20261103T190000Z
DTEND:20261103T200000Z
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""
    intervals = parse_ical_busy_intervals(
        payload, start=datetime(2026, 11, 2, tzinfo=timezone.utc),
        end=datetime(2026, 11, 5, tzinfo=timezone.utc),
    )
    assert intervals == [{"start": datetime(2026, 11, 2, 19),
                          "end": datetime(2026, 11, 2, 20)}]


def test_ical_events_are_normalized_and_merged():
    payload = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Group Planner Test//EN
BEGIN:VEVENT
DTSTART:20261023T180000Z
DTEND:20261023T190000Z
END:VEVENT
BEGIN:VEVENT
DTSTART:20261023T183000Z
DTEND:20261023T200000Z
END:VEVENT
END:VCALENDAR
"""
    intervals = parse_ical_busy_intervals(
        payload, start=datetime(2026, 10, 23), end=datetime(2026, 10, 24)
    )
    assert intervals == [{"start": datetime(2026, 10, 23, 18), "end": datetime(2026, 10, 23, 20)}]


def test_private_calendar_urls_are_rejected():
    with pytest.raises(ValueError, match="private network"):
        safe_calendar_url("https://127.0.0.1/private.ics")


def test_google_public_and_secret_ical_links_pass_url_validation(monkeypatch):
    monkeypatch.setattr("services.calendar_service.socket.getaddrinfo", lambda *_args, **_kwargs: [
        (None, None, None, None, ("8.8.8.8", 443)),
    ])
    for path in ("public/basic.ics", "private-secret/basic.ics"):
        url = f"https://calendar.google.com/calendar/ical/example%40gmail.com/{path}"
        assert safe_calendar_url(url) == url


@pytest.mark.parametrize("status, expected_code", [
    (429, "rate_limited"),
    (401, "authentication_required"),
    (403, "forbidden"),
    (404, "not_found"),
])
def test_ical_fetch_classifies_provider_failure_without_exposing_secret_url(monkeypatch, status, expected_code):
    monkeypatch.setattr("services.calendar_service.socket.getaddrinfo", lambda *_args, **_kwargs: [
        (None, None, None, None, ("8.8.8.8", 443)),
    ])
    secret_url = "https://calendar.google.com/calendar/ical/example%40gmail.com/private-secret/basic.ics"

    class FailedResponse:
        is_redirect = False
        headers = {"Content-Type": "text/html"}
        closed = False

        def __init__(self):
            self.status_code = status

        def close(self):
            self.closed = True

    response = FailedResponse()
    with pytest.raises(CalendarFeedError) as error:
        fetch_calendar_url(secret_url, request_get=lambda *_args, **_kwargs: response)
    assert (error.value.code, error.value.status_code) == (expected_code, status)
    assert secret_url not in str(error.value)
    assert response.closed


def test_ical_fetch_returns_valid_feed_and_closes_response(monkeypatch):
    monkeypatch.setattr("services.calendar_service.socket.getaddrinfo", lambda *_args, **_kwargs: [
        (None, None, None, None, ("8.8.8.8", 443)),
    ])
    payload = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\nEND:VCALENDAR\r\n"

    class FeedResponse:
        is_redirect = False
        status_code = 200
        headers = {"Content-Type": "text/calendar"}
        closed = False

        def iter_content(self, chunk_size):
            yield payload

        def close(self):
            self.closed = True

    response = FeedResponse()
    assert fetch_calendar_url("https://example.com/feed.ics", request_get=lambda *_args, **_kwargs: response) == payload
    assert response.closed


def test_normalized_interval_converts_utc_to_naive_storage_time():
    interval = normalized_interval("2026-10-23T18:00:00Z", "2026-10-23T19:00:00Z")
    assert interval == {"start": datetime(2026, 10, 23, 18), "end": datetime(2026, 10, 23, 19)}


def test_all_day_event_without_dtend_is_one_busy_day():
    payload = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Group Planner//EN
BEGIN:VEVENT
UID:all-day@example.test
DTSTART;VALUE=DATE:20261023
SUMMARY:Private event title is ignored
END:VEVENT
END:VCALENDAR
"""
    intervals = parse_ical_busy_intervals(
        payload, start=datetime(2026, 10, 23), end=datetime(2026, 10, 25)
    )
    assert intervals == [{"start": datetime(2026, 10, 23), "end": datetime(2026, 10, 24)}]


def test_ical_without_calendar_metadata_is_rejected():
    with pytest.raises(ValueError, match="valid iCalendar"):
        parse_ical_busy_intervals(b"BEGIN:VEVENT\nDTSTART:20261023T180000Z\nEND:VEVENT")


def test_pacific_tzid_event_is_stored_as_utc_once():
    payload = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Group Planner Test//EN
BEGIN:VEVENT
UID:pacific@example.test
DTSTART;TZID=America/Los_Angeles:20261026T090000
DTEND;TZID=America/Los_Angeles:20261026T095000
END:VEVENT
END:VCALENDAR
"""
    intervals = parse_ical_busy_intervals(
        payload,
        start=datetime(2026, 10, 26, tzinfo=timezone.utc),
        end=datetime(2026, 10, 27, tzinfo=timezone.utc),
        default_timezone=ZoneInfo("America/Los_Angeles"),
    )
    assert intervals == [{
        "start": datetime(2026, 10, 26, 16),
        "end": datetime(2026, 10, 26, 16, 50),
    }]


def test_utc_event_and_tzid_event_normalize_to_same_instant_without_double_conversion():
    local = normalized_interval(
        datetime(2026, 10, 26, 9, tzinfo=ZoneInfo("America/Los_Angeles")),
        datetime(2026, 10, 26, 9, 50, tzinfo=ZoneInfo("America/Los_Angeles")),
    )
    utc = normalized_interval("2026-10-26T16:00:00Z", "2026-10-26T16:50:00Z")
    assert local == utc


def test_recurring_pacific_events_preserve_local_time_across_dst():
    payload = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Group Planner Test//EN
BEGIN:VEVENT
UID:recurring@example.test
DTSTART;TZID=America/Los_Angeles:20261026T090000
DTEND;TZID=America/Los_Angeles:20261026T095000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4
END:VEVENT
END:VCALENDAR
"""
    intervals = parse_ical_busy_intervals(
        payload,
        start=datetime(2026, 10, 25, tzinfo=timezone.utc),
        end=datetime(2026, 11, 6, tzinfo=timezone.utc),
        default_timezone=ZoneInfo("America/Los_Angeles"),
    )
    assert [interval["start"] for interval in intervals] == [
        datetime(2026, 10, 26, 16),
        datetime(2026, 10, 28, 16),
        datetime(2026, 11, 2, 17),
        datetime(2026, 11, 4, 17),
    ]


def test_floating_ical_time_uses_app_timezone():
    payload = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Group Planner Test//EN
BEGIN:VEVENT
UID:floating@example.test
DTSTART:20261102T090000
DTEND:20261102T100000
END:VEVENT
END:VCALENDAR
"""
    intervals = parse_ical_busy_intervals(
        payload,
        start=datetime(2026, 11, 2, tzinfo=timezone.utc),
        end=datetime(2026, 11, 3, tzinfo=timezone.utc),
        default_timezone=ZoneInfo("America/Los_Angeles"),
    )
    assert intervals == [{
        "start": datetime(2026, 11, 2, 17),
        "end": datetime(2026, 11, 2, 18),
    }]


def test_all_day_ical_conflict_spans_fall_dst_transition():
    payload = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Group Planner Test//EN
BEGIN:VEVENT
UID:all-day-dst@example.test
DTSTART;VALUE=DATE:20261101
DTEND;VALUE=DATE:20261102
END:VEVENT
END:VCALENDAR
"""
    intervals = parse_ical_busy_intervals(
        payload,
        start=datetime(2026, 11, 1, tzinfo=timezone.utc),
        end=datetime(2026, 11, 3, tzinfo=timezone.utc),
        default_timezone=ZoneInfo("America/Los_Angeles"),
    )
    assert intervals == [{
        "start": datetime(2026, 11, 1, 7),
        "end": datetime(2026, 11, 2, 8),
    }]


def test_malformed_ical_interval_does_not_discard_valid_busy_time():
    payload = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Group Planner Test//EN
BEGIN:VEVENT
UID:invalid@example.test
DTSTART:20261102T100000Z
DTEND:20261102T090000Z
END:VEVENT
BEGIN:VEVENT
UID:valid@example.test
DTSTART:20261102T170000Z
DTEND:20261102T180000Z
END:VEVENT
END:VCALENDAR
"""
    intervals = parse_ical_busy_intervals(
        payload,
        start=datetime(2026, 11, 2, tzinfo=timezone.utc),
        end=datetime(2026, 11, 3, tzinfo=timezone.utc),
    )
    assert intervals == [{
        "start": datetime(2026, 11, 2, 17),
        "end": datetime(2026, 11, 2, 18),
    }]
