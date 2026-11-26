"""Private calendar busy-interval handling for Group Planner."""

import ipaddress
import io
import math
import socket
from time import monotonic
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import recurring_ical_events
import requests
import certifi
import urllib3
import dns.exception
import dns.resolver
from icalendar import Calendar


MAX_CALENDAR_BYTES = 2 * 1024 * 1024
SYNC_WINDOW_DAYS = 366
MAX_REDIRECTS = 3
MAX_CALENDAR_FETCH_SECONDS = 20
MAX_CALENDAR_DNS_SECONDS = 3.05


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


def public_calendar_addresses(hostname, *, deadline=None):
    deadline = min(deadline if deadline is not None else math.inf,
                   monotonic() + MAX_CALENDAR_DNS_SECONDS)
    if monotonic() >= deadline:
        raise CalendarFeedError("timeout", "Calendar provider timed out.")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    try:
        addresses = {str(literal)} if literal is not None else set()
        if literal is None:
            resolver = dns.resolver.get_default_resolver()
            for family in ("A", "AAAA"):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise CalendarFeedError("timeout", "Calendar provider timed out.")
                try:
                    # Absolute names avoid resolver search-domain expansion.
                    # A and AAAA share one lifetime; do not fall back to the
                    # system getaddrinfo call or leave background DNS workers.
                    answer = resolver.resolve(hostname.rstrip(".") + ".", family,
                                              lifetime=remaining, search=False)
                    addresses.update(str(record) for record in answer)
                except dns.resolver.NoAnswer:
                    continue
        if monotonic() >= deadline:
            raise CalendarFeedError("timeout", "Calendar provider timed out.")
    except dns.exception.Timeout as exc:
        raise CalendarFeedError("timeout", "Calendar provider timed out.") from exc
    except CalendarFeedError:
        raise
    except (dns.exception.DNSException, OSError, ValueError) as exc:
        raise CalendarFeedError("dns", "Calendar host could not be resolved.") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise CalendarFeedError("unsafe_url", "Calendar URL must not point to a private network.")
    return sorted(addresses)


def safe_calendar_url(url, *, deadline=None):
    parsed = urlsplit((url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise CalendarFeedError("unsafe_url", "Calendar URLs must use HTTPS and a public hostname.")
    try:
        if parsed.port not in (None, 443):
            raise ValueError("Non-HTTPS port")
    except ValueError as exc:
        raise CalendarFeedError("unsafe_url", "Calendar URLs must use the HTTPS port.") from exc
    hostname = parsed.hostname.rstrip(".")
    if hostname.casefold() in {
        "localhost", "localhost.localdomain", "metadata.google.internal",
        "metadata", "instance-data.ec2.internal",
    } or hostname.casefold().endswith(".localhost"):
        raise CalendarFeedError("unsafe_url", "Calendar URL must not point to a private network.")
    public_calendar_addresses(hostname, deadline=deadline)
    return parsed.geturl()


class _CalendarDeadlineReader(io.RawIOBase):
    def __init__(self, raw, transport):
        self._raw = raw
        self._transport = transport

    def readable(self):
        return True

    def readinto(self, buffer):
        self._transport.apply_read_timeout()
        return self._raw.readinto(buffer)

    def close(self):
        try:
            self._raw.close()
        finally:
            super().close()


class _CalendarDeadlineSocket:
    def __init__(self, transport, deadline, read_timeout):
        self._transport = transport
        self._deadline = deadline
        self._read_timeout = read_timeout

    def __getattr__(self, name):
        return getattr(self._transport, name)

    def settimeout(self, value):
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            raise socket.timeout("Calendar transport deadline exceeded")
        self._transport.settimeout(min(value if value is not None else self._read_timeout, remaining))

    def apply_read_timeout(self):
        self.settimeout(self._read_timeout)

    def sendall(self, data, *args, **kwargs):
        self.apply_read_timeout()
        return self._transport.sendall(data, *args, **kwargs)

    def makefile(self, mode="rb", *args, **kwargs):
        if mode != "rb":
            raise OSError("Unsupported calendar transport stream")
        # Keep the socket's own file reference alive when a provider closes the
        # connection after its headers. Check the deadline before every recv,
        # including reads hidden inside buffered headers and decompression.
        raw = self._transport.makefile("rb", buffering=0)
        return io.BufferedReader(_CalendarDeadlineReader(raw, self))


def pinned_calendar_get(url, **options):
    """Connect to a checked IP while verifying TLS against the original hostname."""
    parsed = urlsplit(url)
    deadline = options.get("deadline", monotonic() + MAX_CALENDAR_FETCH_SECONDS)
    address = public_calendar_addresses(parsed.hostname, deadline=deadline)[0]
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise requests.Timeout("Calendar transport deadline exceeded")
    connect_timeout, read_timeout = options.get("timeout", (3.05, 10))

    class DeadlineHTTPSConnection(urllib3.connection.HTTPSConnection):
        def connect(self):
            super().connect()
            self.sock = _CalendarDeadlineSocket(self.sock, deadline, read_timeout)

    pool = urllib3.HTTPSConnectionPool(address, port=443, server_hostname=parsed.hostname,
                                     assert_hostname=parsed.hostname, cert_reqs="CERT_REQUIRED",
                                     ca_certs=certifi.where(), retries=False)
    pool.ConnectionCls = DeadlineHTTPSConnection
    class PinnedResponse(requests.Response):
        def close(self):
            try:
                super().close()
            finally:
                pool.close()
    response = PinnedResponse()
    try:
        headers = {**options.get("headers", {}), "Host": parsed.hostname}
        raw = pool.urlopen("GET", parsed.path + ("?" + parsed.query if parsed.query else "") or "/",
                           headers=headers, redirect=False, preload_content=False,
                           timeout=urllib3.Timeout(connect=min(connect_timeout, remaining / 2),
                                                   read=min(read_timeout, remaining)))
        response.raw, response.status_code, response.url = raw, raw.status, url
        response.headers.update(raw.headers)
        return response
    except (urllib3.exceptions.HTTPError, OSError) as exc:
        pool.close()
        if isinstance(exc, (socket.timeout, urllib3.exceptions.TimeoutError)) or monotonic() >= deadline:
            raise requests.Timeout("Calendar transport deadline exceeded") from exc
        raise requests.RequestException("Calendar connection failed") from exc


def fetch_calendar_url(url, request_get=None):
    request_get = request_get or pinned_calendar_get
    deadline = monotonic() + MAX_CALENDAR_FETCH_SECONDS
    current_url = safe_calendar_url(url, deadline=deadline)
    for _redirect in range(MAX_REDIRECTS + 1):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise CalendarFeedError("timeout", "Calendar provider timed out.")
        try:
            deadline_options = {"deadline": deadline} if request_get is pinned_calendar_get else {}
            response = request_get(
                current_url,
                timeout=(min(3.05, remaining), min(10, remaining)),
                allow_redirects=False,
                stream=True,
                headers={"Accept": "text/calendar, text/plain;q=0.8"},
                **deadline_options,
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
                current_url = safe_calendar_url(urljoin(current_url, location), deadline=deadline)
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
                    if monotonic() > deadline:
                        raise CalendarFeedError("timeout", "Calendar provider timed out.")
                    body += chunk
                    if len(body) > MAX_CALENDAR_BYTES:
                        raise CalendarFeedError("too_large", "Calendar feed is too large.")
            except requests.Timeout as exc:
                raise CalendarFeedError("timeout", "Calendar provider timed out.") from exc
            except requests.RequestException as exc:
                if monotonic() >= deadline:
                    raise CalendarFeedError("timeout", "Calendar provider timed out.") from exc
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
    if end <= start or end - start > timedelta(days=367):
        raise ValueError("Calendar expansion must use a bounded date window.")
    events = calendar.walk("VEVENT")
    if len(events) > 5000:
        raise ValueError("Calendar contains too many events.")
    estimated = len(events)
    for event in events:
        rule = event.get("RRULE")
        if rule:
            frequency = str(rule.get("FREQ", [""])[0]).upper()
            if frequency in {"SECONDLY", "MINUTELY"}:
                raise ValueError("Calendar recurrence is too frequent.")
            seconds = {"HOURLY": 3600, "DAILY": 86400, "WEEKLY": 604800, "MONTHLY": 2419200, "YEARLY": 31536000}.get(frequency, 1)
            interval = max(1, int(rule.get("INTERVAL", [1])[0]))
            multiplier = math.prod(max(1, len(rule.get(field, []))) for field in ("BYSECOND", "BYMINUTE", "BYHOUR", "BYDAY", "BYMONTHDAY", "BYMONTH"))
            expanded = math.ceil((end - start).total_seconds() / seconds / interval) * multiplier
            if rule.get("COUNT"):
                expanded = min(expanded, int(rule["COUNT"][0]))
            estimated += expanded
        rdates = event.get("RDATE")
        if rdates:
            for item in rdates if isinstance(rdates, list) else [rdates]:
                estimated += len(item.dts)
        if estimated > 20000:
            raise ValueError("Calendar recurrence exceeds the processing limit.")
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
