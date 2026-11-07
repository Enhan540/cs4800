"""Private calendar busy-interval handling for Group Planner."""

import ipaddress
import socket
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import recurring_ical_events
import requests
from icalendar import Calendar


MAX_CALENDAR_BYTES = 2 * 1024 * 1024
SYNC_WINDOW_DAYS = 366
MAX_REDIRECTS = 3


class CalendarFeedError(ValueError):
    """A safe, URL-free reason an iCal subscription could not be read."""

    def __init__(self, code, message, status_code=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def timezone_info(value):
    """Return an IANA timezone, rejecting ambiguous fixed-offset labels."""
    if hasattr(value, "utcoffset"):
        return value
    try:
        return ZoneInfo(value or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Calendar timezone is not a valid IANA timezone.") from exc


def parse_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if not isinstance(value, str):
        raise ValueError("Calendar interval has no valid time.")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalized_interval(start, end, *, default_timezone=timezone.utc):
    start = parse_datetime(start)
    end = parse_datetime(end)
    # Floating iCalendar values have no timezone marker. The application uses
    # UTC as its scheduling baseline, so make that interpretation explicit
    # rather than silently comparing local wall-clock values to UTC values.
    default_timezone = timezone_info(default_timezone)
    if start.tzinfo is None:
        start = start.replace(tzinfo=default_timezone)
    if end.tzinfo is None:
        end = end.replace(tzinfo=default_timezone)
    start = start.astimezone(timezone.utc).replace(tzinfo=None)
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
        raise CalendarFeedError("unsafe_url", "Calendar URLs must use HTTPS and a public hostname.")
    hostname = parsed.hostname.rstrip(".")
    if hostname.casefold() in {
        "localhost", "localhost.localdomain", "metadata.google.internal",
        "metadata", "instance-data.ec2.internal",
    } or hostname.casefold().endswith(".localhost"):
        raise CalendarFeedError("unsafe_url", "Calendar URL must not point to a private network.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)}
    except (OSError, socket.gaierror) as exc:
        raise CalendarFeedError("dns", "Calendar host could not be resolved.") from exc
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
            raise CalendarFeedError("unsafe_url", "Calendar URL must not point to a private network.")
    return parsed.geturl()


def fetch_calendar_url(url, request_get=requests.get):
    current_url = safe_calendar_url(url)
    for _redirect in range(MAX_REDIRECTS + 1):
        try:
            response = request_get(
                current_url,
                timeout=(3.05, 10),
                allow_redirects=False,
                stream=True,
                headers={"Accept": "text/calendar, text/plain;q=0.8"},
            )
        except requests.Timeout as exc:
            raise CalendarFeedError("timeout", "Calendar provider timed out.") from exc
        except requests.RequestException as exc:
            raise CalendarFeedError("network", "Calendar provider could not be reached.") from exc
        try:
            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise CalendarFeedError("redirect", "Calendar redirect had no destination.")
                current_url = safe_calendar_url(urljoin(current_url, location))
                continue
            status = response.status_code
            if status == 429:
                raise CalendarFeedError("rate_limited", "Calendar provider limited requests.", status)
            if status == 401:
                raise CalendarFeedError("authentication_required", "Calendar feed requires authorization.", status)
            if status == 403:
                raise CalendarFeedError("forbidden", "Calendar provider denied access.", status)
            if status == 404:
                raise CalendarFeedError("not_found", "Calendar feed was not found.", status)
            if status >= 500:
                raise CalendarFeedError("provider_error", "Calendar provider is unavailable.", status)
            if status >= 400:
                raise CalendarFeedError("http_error", "Calendar provider rejected the request.", status)
            body = b""
            try:
                for chunk in response.iter_content(chunk_size=65536):
                    body += chunk
                    if len(body) > MAX_CALENDAR_BYTES:
                        raise CalendarFeedError("too_large", "Calendar feed is too large.")
            except requests.Timeout as exc:
                raise CalendarFeedError("timeout", "Calendar provider timed out.") from exc
            except requests.RequestException as exc:
                raise CalendarFeedError("network", "Calendar provider could not be reached.") from exc
            return body
        finally:
            response.close()
    raise CalendarFeedError("redirect", "Calendar URL redirected too many times.")


def parse_ical_busy_intervals(payload, *, start=None, end=None, default_timezone=timezone.utc):
    default_timezone = timezone_info(default_timezone)
    start = start or datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = end or start + timedelta(days=SYNC_WINDOW_DAYS)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    try:
        calendar = Calendar.from_ical(payload)
    except Exception as exc:
        raise ValueError("Calendar feed is not valid iCalendar data.") from exc
    if calendar.get("VERSION") is None or calendar.get("PRODID") is None:
        raise ValueError("Calendar feed is not valid iCalendar data.")
    def event_interval(event):
        if str(event.get("TRANSP", "")).upper() == "TRANSPARENT":
            return None
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            return None
        try:
            event_start = event.decoded("DTSTART")
            event_end = event.decoded("DTEND") if event.get("DTEND") else None
            if event_end is None and event.get("DURATION"):
                event_end = event_start + event.decoded("DURATION")
            if event_end is None:
                event_end = event_start + timedelta(days=1)
            return normalized_interval(event_start, event_end, default_timezone=default_timezone)
        except (KeyError, ValueError, TypeError):
            return None

    calendar.subcomponents = [
        component for component in calendar.subcomponents
        if component.name != "VEVENT"
        or str(component.get("STATUS", "")).upper() == "CANCELLED"
        or str(component.get("TRANSP", "")).upper() == "TRANSPARENT"
        or event_interval(component) is not None
    ]
    intervals = []
    try:
        events = recurring_ical_events.of(calendar).between(start, end)
    except Exception:
        events = calendar.walk("VEVENT")
    for event in events:
        interval = event_interval(event)
        if interval is not None:
            intervals.append(interval)
    return merge_intervals(intervals)


def effective_availability_records(records, busy_intervals):
    """Subtract date-specific busy intervals from recurring records for scheduling.

    The scheduling service receives the busy intervals separately; this helper is
    retained for callers that need the baseline records unchanged.
    """
    return list(records), merge_intervals(busy_intervals)
