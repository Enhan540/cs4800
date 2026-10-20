"""Private calendar busy-interval handling for Group Planner."""

import ipaddress
import socket
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import recurring_ical_events
import requests
from icalendar import Calendar


MAX_CALENDAR_BYTES = 2 * 1024 * 1024
SYNC_WINDOW_DAYS = 366


def parse_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if not isinstance(value, str):
        raise ValueError("Calendar interval has no valid time.")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalized_interval(start, end):
    start = parse_datetime(start)
    end = parse_datetime(end)
    if start.tzinfo:
        start = start.astimezone(timezone.utc).replace(tzinfo=None)
    if end.tzinfo:
        end = end.astimezone(timezone.utc).replace(tzinfo=None)
    if end <= start:
        raise ValueError("Calendar interval must end after it starts.")
    return {"start": start, "end": end}


def merge_intervals(intervals):
    merged = []
    for interval in sorted(intervals, key=lambda item: item["start"]):
        if merged and interval["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], interval["end"])
        else:
            merged.append(dict(interval))
    return merged


def safe_calendar_url(url):
    parsed = urlsplit((url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Calendar URLs must use HTTPS and a public hostname.")
    hostname = parsed.hostname.rstrip(".")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Calendar host could not be resolved.") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError("Calendar URL must not point to a private network.")
    return parsed.geturl()


def fetch_calendar_url(url, request_get=requests.get):
    current_url = safe_calendar_url(url)
    for _redirect in range(4):
        response = request_get(
            current_url,
            timeout=(3.05, 10),
            allow_redirects=False,
            stream=True,
            headers={"Accept": "text/calendar, text/plain;q=0.8"},
        )
        if response.is_redirect:
            location = response.headers.get("Location")
            if not location:
                raise ValueError("Calendar redirect did not provide a destination.")
            current_url = safe_calendar_url(urljoin(current_url, location))
            continue
        response.raise_for_status()
        body = b""
        for chunk in response.iter_content(chunk_size=65536):
            body += chunk
            if len(body) > MAX_CALENDAR_BYTES:
                raise ValueError("Calendar feed is too large.")
        return body
    raise ValueError("Calendar URL redirected too many times.")


def parse_ical_busy_intervals(payload, *, start=None, end=None):
    start = start or datetime.now(timezone.utc).replace(tzinfo=None)
    end = end or start + timedelta(days=SYNC_WINDOW_DAYS)
    try:
        calendar = Calendar.from_ical(payload)
    except Exception as exc:
        raise ValueError("Calendar feed is not valid iCalendar data.") from exc
    intervals = []
    try:
        events = recurring_ical_events.of(calendar).between(start, end)
    except Exception:
        events = calendar.walk("VEVENT")
    for event in events:
        try:
            event_start = event.decoded("DTSTART")
            event_end = event.decoded("DTEND")
            intervals.append(normalized_interval(event_start, event_end))
        except (KeyError, ValueError, TypeError):
            continue
    return merge_intervals(intervals)


def effective_availability_records(records, busy_intervals):
    """Subtract date-specific busy intervals from recurring records for scheduling.

    The scheduling service receives the busy intervals separately; this helper is
    retained for callers that need the baseline records unchanged.
    """
    return list(records), merge_intervals(busy_intervals)
