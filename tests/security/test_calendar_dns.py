"""Calendar DNS must stay inside the request budget and reject unsafe answers."""

import socket
import time

import dns.resolver
import pytest

from services import calendar_service as calendar

pytestmark = pytest.mark.security


def test_silent_dns_server_cannot_outlive_fetch_budget(monkeypatch):
    # A bound UDP socket that never answers provides a real resolver timeout,
    # without contacting an external nameserver or leaving a worker behind.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 0))
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = ["127.0.0.1"]
        resolver.port = server.getsockname()[1]
        monkeypatch.setattr(dns.resolver, "get_default_resolver", lambda: resolver)
        monkeypatch.setattr(calendar, "MAX_CALENDAR_FETCH_SECONDS", 0.12)

        def slow_system_lookup(*args, **kwargs):
            time.sleep(0.5)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

        monkeypatch.setattr(socket, "getaddrinfo", slow_system_lookup)
        start = time.monotonic()
        with pytest.raises(calendar.CalendarFeedError) as caught:
            calendar.fetch_calendar_url("https://calendar.example.invalid/private-feed")
        elapsed = time.monotonic() - start
        assert caught.value.code == "timeout"
        assert elapsed < 0.4, "DNS escaped the shared download deadline"
        assert "private-feed" not in str(caught.value)


def test_address_families_share_one_dns_budget(monkeypatch):
    clock = [100.0]
    observed = []

    def resolve(_self, hostname, family, **options):
        observed.append((hostname, family, options))
        clock[0] += 0.2
        return ["8.8.8.8"] if family == "A" else ["2606:4700:4700::1111"]

    monkeypatch.setattr(calendar, "monotonic", lambda: clock[0])
    monkeypatch.setattr(dns.resolver.Resolver, "resolve", resolve)
    assert calendar.public_calendar_addresses("calendar.example.invalid", deadline=100.5)
    assert [entry[1] for entry in observed] == ["A", "AAAA"]
    assert observed[0][2]["lifetime"] == pytest.approx(0.5)
    assert observed[1][2]["lifetime"] == pytest.approx(0.3)
    assert all(entry[2]["search"] is False for entry in observed)
    assert all(entry[0] == "calendar.example.invalid." for entry in observed)


@pytest.mark.parametrize("answers", [
    {"A": ["8.8.8.8"], "AAAA": ["::1"]},
    {"A": ["8.8.8.8", "169.254.169.254"], "AAAA": []},
    {"A": [], "AAAA": ["::ffff:127.0.0.1"]},
])
def test_any_private_answer_rejects_the_entire_resolution(monkeypatch, answers):
    monkeypatch.setattr(dns.resolver.Resolver, "resolve", lambda _self, _host, family, **kw: answers[family])
    with pytest.raises(calendar.CalendarFeedError) as caught:
        calendar.safe_calendar_url("https://calendar.example.invalid/feed")
    assert caught.value.code == "unsafe_url"


def test_literal_addresses_never_need_dns(monkeypatch):
    def unexpected_lookup(*args, **kwargs):
        pytest.fail("An IP literal must not invoke DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_lookup)
    monkeypatch.setattr(dns.resolver, "get_default_resolver", unexpected_lookup)
    assert calendar.public_calendar_addresses("8.8.8.8") == ["8.8.8.8"]
    with pytest.raises(calendar.CalendarFeedError):
        calendar.safe_calendar_url("https://[::1]/feed")


def test_budget_exhausted_between_answers_stays_a_timeout(monkeypatch):
    clock = [100.0]
    calls = []

    def resolve(_self, _host, family, **kw):
        calls.append(family)
        clock[0] += 0.6
        return ["8.8.8.8"]

    monkeypatch.setattr(calendar, "monotonic", lambda: clock[0])
    monkeypatch.setattr(dns.resolver.Resolver, "resolve", resolve)
    with pytest.raises(calendar.CalendarFeedError) as caught:
        calendar.public_calendar_addresses("calendar.example.invalid", deadline=100.5)
    assert caught.value.code == "timeout"
    assert calls == ["A"]


def test_missing_one_family_can_use_the_other_but_dns_failures_are_closed(monkeypatch):
    def ipv6_only(_self, _host, family, **kw):
        if family == "A":
            raise dns.resolver.NoAnswer()
        return ["2606:4700:4700::1111"]

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", ipv6_only)
    assert calendar.public_calendar_addresses("calendar.example.invalid") == ["2606:4700:4700::1111"]
    monkeypatch.setattr(dns.resolver.Resolver, "resolve", lambda *a, **kw: (_ for _ in ()).throw(dns.resolver.NoNameservers()))
    with pytest.raises(calendar.CalendarFeedError) as caught:
        calendar.public_calendar_addresses("calendar.example.invalid")
    assert caught.value.code == "dns"
