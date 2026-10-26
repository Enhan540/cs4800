from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from services.calendar_service import normalized_interval, parse_ical_busy_intervals, safe_calendar_url


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
