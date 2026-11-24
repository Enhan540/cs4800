"""Scanner reports must be usable by the non-root Linux runner."""
import os
import subprocess
import uuid
from types import SimpleNamespace

import pytest
from scripts import security_check

pytestmark = pytest.mark.security


def options_for(monkeypatch, platform):
    with monkeypatch.context() as patch:
        patch.setattr(security_check, "os", platform)
        return getattr(security_check, "docker_report_user_options", lambda: [])()


def test_windows_report_options_do_not_require_posix_user_apis(monkeypatch):
    assert options_for(monkeypatch, SimpleNamespace(name="nt")) == []


def test_posix_reports_use_the_actual_runner_identity(monkeypatch):
    options = options_for(monkeypatch, SimpleNamespace(name="posix", getuid=lambda: 1001, getgid=lambda: 127))
    assert options == ["--user", "1001:127"]


def test_linux_runner_can_read_and_sanitize_real_scanner_report(monkeypatch):
    if os.environ.get("RUN_SECURITY_INTEGRATION") != "1":
        pytest.skip("Run the security gate for required scanner integration")
    image = security_check.IMAGES["gitleaks"]
    volume = "group-planner-report-test-" + uuid.uuid4().hex
    flags = options_for(monkeypatch, SimpleNamespace(name="posix", getuid=lambda: 1001, getgid=lambda: 1001))

    def docker(*arguments, data=None):
        return subprocess.run(["docker", *arguments], input=data, capture_output=True, timeout=90, check=False)

    created = docker("volume", "create", volume)
    assert created.returncode == 0 and created.stdout.decode().strip() == volume
    try:
        setup = docker("run", "--rm", "--network=none", "--user", "0:0", "-v", volume + ":/reports",
                       "--entrypoint", "sh", image, "-c", "chown 1001:1001 /reports && chmod 700 /reports")
        assert setup.returncode == 0
        scan = docker("run", "--rm", "-i", "--network=none", *flags, "-v", volume + ":/reports", image,
                      "stdin", "--redact=100", "--report-format=json", "--report-path=/reports/report.json",
                      "--no-banner", data=b"Synthetic non-secret audit input\n")
        assert scan.returncode == 0, "The pinned scanner must finish before normalization"
        normalized = docker("run", "--rm", "--network=none", "--user", "1001:1001", "-v", volume + ":/reports",
                            "--entrypoint", "sh", image, "-c",
                            'cat /reports/report.json >/dev/null && printf "[]\\n" >/reports/report.json')
        assert normalized.returncode == 0, "The non-root runner must read and sanitize the scanner report"
    finally:
        cleaned = docker("volume", "rm", volume)
        assert cleaned.returncode == 0, "Disposable scanner storage must be removed"

