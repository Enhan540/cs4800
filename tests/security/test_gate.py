import json

import pytest

from scripts.security_check import finding, is_blocking, merge_findings, parse_secret_report, private_reports_absent
from scripts.security_inventory import inventory

pytestmark = pytest.mark.security


@pytest.mark.parametrize("name", ["current-authentication-review.json", "release-tool-remediation-review.json", "usage-guard.json", "active-work-checkpoint.json", "main-source.zip", "group-permission-review.json", "scanner-triage-review.json"])
def test_new_private_reports_and_backups_are_rejected_at_any_depth(tmp_path, name):
    nested = tmp_path / "accidental-copy"
    nested.mkdir()
    (nested / name).write_text("Synthetic private artifact")
    assert not private_reports_absent(tmp_path)


def test_repository_security_policy_files_are_allowed(tmp_path):
    policy = tmp_path / ".security"
    policy.mkdir()
    (policy / "routes.json").write_text("[]")
    assert private_reports_absent(tmp_path)


@pytest.mark.parametrize("outcome", ["credential_exposure", "cross_user_access", "account_takeover"])
def test_severe_outcome_blocks_even_when_rating_is_low(outcome):
    assert is_blocking(finding("synthetic", "probe", "LOW", outcome=outcome))


@pytest.mark.parametrize("status", [None, "false_positive", "waived", "ignored"])
def test_unrecognized_private_status_cannot_waive_a_serious_risk(status):
    risk = finding("manual-review", "synthetic-unresolved", "HIGH")
    risk["status"] = status
    assert is_blocking(risk)


def test_low_risks_persist_and_failed_scans_do_not_resolve_findings():
    risk = finding("synthetic", "probe", "LOW")
    register = merge_findings({}, [risk], "main", {"synthetic"})
    assert not is_blocking(register["findings"][0])
    merge_findings(register, [], "main", set())
    assert register["findings"][0]["status"] == "candidate"
    merge_findings(register, [], "main", {"synthetic"})
    assert register["findings"][0]["status"] == "resolved"


@pytest.mark.parametrize("old_status", ["resolved", "false_positive", "waived", "ignored"])
def test_historical_triage_never_hides_a_new_blocking_scanner_result(old_status):
    prior = finding("synthetic", "same-rule-and-location", "LOW")
    prior.update(status=old_status, branch="main", owner="assigned maintainer", deadline="2030-01-01")
    current = finding("synthetic", "same-rule-and-location", "HIGH")
    register = merge_findings({"findings": [prior]}, [current], "main", {"synthetic"})
    result = register["findings"][0]
    assert is_blocking(result)
    assert result["status"] == "candidate"
    assert result["owner"] == "assigned maintainer" and result["deadline"] == "2030-01-01"


def test_route_inventory_matches_explicit_policy():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    declared = json.loads((root / ".security/routes.json").read_text())
    assert inventory(root) == declared
    assert all(row["login_guard"] for row in declared if row["access"] == "authenticated")


@pytest.mark.parametrize("code,data", [(1, b""), (42, b"[]"), (42, b""), (0, b"not JSON")])
def test_failed_or_incomplete_secret_scan_never_passes(code, data):
    with pytest.raises(ValueError):
        parse_secret_report(code, data)


def test_clean_secret_scan_and_unresolved_risks_have_distinct_results():
    assert parse_secret_report(0, b"") == []
    assert parse_secret_report(42, b'[{"RuleID":"synthetic"}]')


def test_staged_snapshot_cannot_be_hidden_by_unstaged_content(tmp_path, monkeypatch):
    import subprocess
    from scripts import security_check
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init")
    file = tmp_path / "example.txt"
    file.write_text("staged-content")
    git("add", "example.txt")
    file.write_text("different-unstaged-content")
    exported = tmp_path / "exported"
    exported.mkdir()
    monkeypatch.setattr(security_check, "ROOT", tmp_path)
    identity = security_check.make_snapshot(exported, "commit", "HEAD")
    assert (exported / "example.txt").read_text() == "staged-content"
    assert identity != security_check.hashlib.sha256(b"different-unstaged-content").hexdigest()


def test_push_hook_checks_all_refs_and_ignores_deletions():
    from scripts.security_push_hook import parse_updates
    one, two, zero = "1" * 40, "2" * 40, "0" * 40
    records = f"refs/heads/one {one} refs/heads/one {zero}\nrefs/heads/two {two} refs/heads/two {one}\n(delete) {zero} refs/heads/old {one}\n"
    assert parse_updates(records) == [("one", one), ("two", two)]


def test_synthetic_credential_is_detected_without_logging_its_value():
    import os
    import secrets
    import subprocess
    from pathlib import Path
    from scripts.security_check import IMAGES
    if os.environ.get("RUN_SECURITY_INTEGRATION") != "1":
        pytest.skip("Run the security gate for required scanner integration")
    policy = Path(__file__).resolve().parents[2] / ".security"
    value = "mongodb+srv://synthetic:" + secrets.token_hex(16) + "@audit.example.invalid/test\n"
    result = subprocess.run(["docker", "run", "--rm", "-i", "--network=none", "-v", f"{policy}:/policy:ro", IMAGES["gitleaks"], "stdin", "--config=/policy/gitleaks.toml", "--redact=100", "--exit-code=42", "--no-banner"], input=value.encode(), capture_output=True, timeout=60, check=False)
    assert result.returncode == 42
    assert value.encode() not in result.stdout + result.stderr
