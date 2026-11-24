"""Tooling must not leak credentials, accept spoofed checks, or rewrite trusted evidence."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
import requests

from scripts import security_admin as admin
from scripts import security_report as report

pytestmark = pytest.mark.security


class Response:
    def __init__(self, value=None, *, body=None, status=200):
        self.status_code = status
        self.body = json.dumps(value).encode() if body is None else body
        self.closed = False
    def iter_content(self, **kwargs):
        yield self.body
    def close(self):self.closed = True
    def __enter__(self):return self
    def __exit__(self, *args):self.close()


@pytest.fixture
def policy():
    return json.loads((Path(__file__).resolve().parents[2] / ".security/branch-protection.json").read_text())


def remote_state(policy):
    return {**copy.deepcopy(policy), **{key: {"enabled": policy[key]} for key in ("enforce_admins", "allow_force_pushes", "allow_deletions", "required_conversation_resolution")}}


def test_branch_policy_requires_official_check_origin_and_review(policy):
    assert admin.policy_verified(policy)
    assert admin.protection_verified(remote_state(policy), policy["required_status_checks"])


@pytest.mark.parametrize("weakness", ["other_app", "any_app", "name_only", "not_strict", "admin_bypass", "force_push", "deletion", "no_review", "stale_review", "no_last_push_review", "review_bypass", "missing_proof", "truthy_string"])
def test_incomplete_or_spoofed_branch_protection_cannot_verify(policy, weakness):
    state = remote_state(policy);status = copy.deepcopy(policy["required_status_checks"])
    if weakness == "other_app":status["checks"][0]["app_id"] = 999
    elif weakness == "any_app":status["checks"][0]["app_id"] = -1
    elif weakness == "name_only":status = {"strict": True, "contexts": [admin.CHECK_NAME]}
    elif weakness == "not_strict":status["strict"] = False
    elif weakness == "admin_bypass":state["enforce_admins"]["enabled"] = False
    elif weakness == "force_push":state["allow_force_pushes"]["enabled"] = True
    elif weakness == "deletion":state["allow_deletions"]["enabled"] = True
    elif weakness == "no_review":state["required_pull_request_reviews"]["required_approving_review_count"] = 0
    elif weakness == "stale_review":state["required_pull_request_reviews"]["dismiss_stale_reviews"] = False
    elif weakness == "no_last_push_review":state["required_pull_request_reviews"]["require_last_push_approval"] = False
    elif weakness == "review_bypass":state["required_pull_request_reviews"]["bypass_pull_request_allowances"]["users"] = [{"login": "synthetic-bypass"}]
    elif weakness == "missing_proof":state.pop("enforce_admins")
    elif weakness == "truthy_string":state["enforce_admins"]["enabled"] = "true"
    assert not admin.protection_verified(state, status)


@pytest.mark.parametrize("apply", [False, True])
def test_admin_reads_back_both_branches_without_redirects(policy, monkeypatch, apply):
    calls = []
    class Session:
        def __init__(self):self.headers = {};self.trust_env = True
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs, dict(self.headers), self.trust_env))
            assert url.startswith("https://api.github.com/repos/Enhan540/cs4800/branches/")
            if method == "PUT":
                assert kwargs["json"] == policy
                return Response({"acknowledged": True})
            if not url.endswith(("/protection", "/required_status_checks")):
                return Response({"protected": False})
            return Response(policy["required_status_checks"] if url.endswith("/required_status_checks") else remote_state(policy))
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-administrator-credential")
    monkeypatch.setattr(admin.requests, "Session", Session)
    assert admin.main(["--apply"] if apply else []) == 0
    assert sum(call[0] == "PUT" for call in calls) == (2 if apply else 0)
    assert sum(call[0] == "GET" for call in calls) == (6 if apply else 4)
    assert all(call[2]["allow_redirects"] is False and call[4] is False for call in calls)
    assert all(call[3]["Content-Type"] == "application/json" for call in calls)


def test_weak_policy_is_rejected_before_any_admin_request(policy, tmp_path, monkeypatch):
    (tmp_path / ".security").mkdir()
    policy["allow_force_pushes"] = True
    (tmp_path / ".security/branch-protection.json").write_text(json.dumps(policy))
    monkeypatch.setattr(admin, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-administrator-credential")
    monkeypatch.setattr(admin.requests, "Session", lambda: pytest.fail("Do not send an unsafe protection policy"))
    assert admin.main(["--apply"]) == 1


def test_existing_stricter_protections_are_never_overwritten(policy, monkeypatch):
    class Session:
        def __init__(self):self.headers = {}
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def request(self, method, url, **kwargs):
            assert method == "GET", "Existing protection must not be replaced"
            if url.endswith("/required_status_checks"):
                return Response({"strict": True, "checks": [{"context": admin.CHECK_NAME, "app_id": -1}]})
            if url.endswith("/protection"):
                state = remote_state(policy)
                state["required_pull_request_reviews"]["required_approving_review_count"] = 3
                state["required_pull_request_reviews"]["require_code_owner_reviews"] = True
                return Response(state)
            return Response({"protected": True})
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-administrator-credential")
    monkeypatch.setattr(admin.requests, "Session", Session)
    assert admin.main(["--apply"]) == 1


@pytest.mark.parametrize("failure", ["redirect", "large", "malformed", "network"])
def test_admin_provider_failure_is_closed_and_redacted(monkeypatch, capsys, failure):
    canary = "SYNTHETIC-PRIVATE-PROVIDER-PAYLOAD"
    responses = []
    class Session:
        def __init__(self):self.headers = {}
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def request(self, *args, **kwargs):
            assert kwargs["allow_redirects"] is False
            if failure == "network":raise requests.Timeout(canary)
            response = Response(body=(b"x" * (1024 * 1024 + 1)) if failure == "large" else canary.encode(), status=302 if failure == "redirect" else 200)
            responses.append(response);return response
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-administrator-credential")
    monkeypatch.setattr(admin.requests, "Session", Session)
    assert admin.main([]) == 1
    assert all(response.closed for response in responses)
    assert canary not in capsys.readouterr().out


def fake_asvs_session(monkeypatch, response):
    class Session:
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def get(self, url, **kwargs):
            assert url == report.ASVS_URL and kwargs["allow_redirects"] is False and kwargs["stream"] is True
            assert self.trust_env is False
            return response
    monkeypatch.setattr(report.requests, "Session", Session)


def test_only_reviewed_checklist_bytes_are_accepted(monkeypatch):
    data = b"req_id,req_description\nV1.1.1,Synthetic test control\n"
    monkeypatch.setattr(report, "ASVS_SHA256", hashlib.sha256(data).hexdigest())
    response = Response(body=data)
    fake_asvs_session(monkeypatch, response)
    assert report.fetch_asvs() == data and response.closed


@pytest.mark.parametrize("failure", ["redirect", "modified", "large"])
def test_changed_redirected_or_oversized_checklist_is_rejected(monkeypatch, failure):
    response = Response(body=b"x" * (1024 * 1024 + 1) if failure == "large" else b"Unreviewed changed controls", status=302 if failure == "redirect" else 200)
    fake_asvs_session(monkeypatch, response)
    with pytest.raises(ValueError):
        report.fetch_asvs()
    assert response.closed


def test_failed_checklist_fetch_does_not_overwrite_existing_evidence(tmp_path, monkeypatch):
    files = ["asvs-5.0.0-source.csv", "asvs-checklist.json", "route-permission-matrix.json", "audit-report.json"]
    for name in files:(tmp_path / name).write_text("Existing private evidence")
    monkeypatch.setattr(report, "private_home", lambda: tmp_path)
    def fail():raise ValueError("Synthetic failed fetch")
    monkeypatch.setattr(report, "fetch_asvs", fail)
    with pytest.raises(ValueError):report.main()
    assert all((tmp_path / name).read_text() == "Existing private evidence" for name in files)

