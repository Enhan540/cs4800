"""The disposable DAST target must never listen on every network interface."""
import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture
def dynamic(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("security_dynamic")


def use_linux_gateway(monkeypatch, dynamic, gateway):
    monkeypatch.setattr(dynamic.os, "name", "posix")
    monkeypatch.setattr(dynamic.sys, "platform", "linux")
    calls = []

    def fake_docker(*arguments):
        calls.append(arguments)
        return gateway

    monkeypatch.setattr(dynamic, "docker", fake_docker)
    return calls


@pytest.mark.parametrize("os_name,platform", [("nt", "win32"), ("posix", "darwin")])
def test_docker_desktop_binds_loopback_only(monkeypatch, dynamic, os_name, platform):
    monkeypatch.setattr(dynamic.os, "name", os_name)
    monkeypatch.setattr(dynamic.sys, "platform", platform)
    monkeypatch.setattr(dynamic, "docker", lambda *_: pytest.fail("Docker Desktop needs no network lookup"))
    assert dynamic.scan_bind_host() == "127.0.0.1"


def test_linux_binds_private_bridge_gateway(monkeypatch, dynamic):
    calls = use_linux_gateway(monkeypatch, dynamic, b"172.17.0.1\n")
    assert dynamic.scan_bind_host() == "172.17.0.1"
    assert calls and calls[0][:3] == ("network", "inspect", "bridge")


@pytest.mark.parametrize("gateway", [b"0.0.0.0", b"8.8.8.8", b"127.0.0.1", b"", b"not-an-address", b"fd00::1"])
def test_linux_rejects_unsafe_or_unknown_gateway(monkeypatch, dynamic, gateway):
    use_linux_gateway(monkeypatch, dynamic, gateway)
    with pytest.raises(RuntimeError):
        dynamic.scan_bind_host()


def test_scan_never_binds_all_interfaces():
    source = (SCRIPTS / "security_dynamic.py").read_text(encoding="utf-8")
    assert "0.0.0.0" not in source
    assert "api.addrs.addr.name=.*" not in source
