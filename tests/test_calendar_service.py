from datetime import datetime

import pytest

from services.calendar_service import normalized_interval, parse_ical_busy_intervals, safe_calendar_url


def test_ical_events_are_normalized_and_merged():
    payload = b"""BEGIN:VCALENDAR
VERSION:2.0
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
