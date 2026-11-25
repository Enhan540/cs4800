import socket

import pytest

from services.calendar_service import CalendarFeedError, parse_ical_busy_intervals, safe_calendar_url

pytestmark = pytest.mark.security


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "::ffff:127.0.0.1", "fc00::1"])
def test_private_dns_answers_are_rejected(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    with pytest.raises(CalendarFeedError):
        safe_calendar_url("https://synthetic.example.invalid/feed.ics")


def test_nonstandard_https_port_is_not_an_internal_port_probe():
    with pytest.raises(CalendarFeedError):
        safe_calendar_url("https://synthetic.example.invalid:8443/feed.ics")


@pytest.mark.parametrize("rule", ["FREQ=SECONDLY;COUNT=100000000", "FREQ=MINUTELY", "FREQ=HOURLY;BYMINUTE=" + ",".join(str(value) for value in range(60))])
def test_dense_recurrence_is_rejected_before_expansion(rule):
    payload = f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Security test//EN\r\nBEGIN:VEVENT\r\nUID:synthetic\r\nDTSTART:20261118T090000Z\r\nDTEND:20261118T100000Z\r\nRRULE:{rule}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n".encode()
    with pytest.raises(ValueError):
        parse_ical_busy_intervals(payload)
