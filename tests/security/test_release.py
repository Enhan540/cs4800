import copy
from pathlib import Path

import pytest
import requests

from scripts import security_release as release
from scripts.security_check import dependency_files

pytestmark = pytest.mark.security


@pytest.fixture
def release_config():
    return {"token": "synthetic-release-credential", "owner": "team_synthetic", "project": "prj_synthetic", "repository": "12345", "sha": "1" * 40, "hostname": "planner.example.invalid"}


class FakeClient:
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.project = {"id": config["project"], "accountId": config["owner"], "name": "synthetic-planner", "link": {"type": "github", "repoId": config["repository"], "productionBranch": "main"}}
        self.created = {"id": "dpl_synthetic"}
        self.ready = {"id": "dpl_synthetic", "projectId": config["project"], "target": "production", "gitSource": {"type": "github", "repoId": config["repository"], "ref": "main", "sha": config["sha"]}, "readyState": "READY", "aliasAssigned": True, "alias": [config["hostname"]]}
        self.polls = []

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.startswith("/v9/projects/"):
            return copy.deepcopy(self.project)
        if method == "POST":
            return copy.deepcopy(self.created)
        return copy.deepcopy(self.polls.pop(0) if self.polls else self.ready)


def test_release_pins_checked_commit_and_verifies_live_alias(release_config):
    client = FakeClient(release_config)
    client.polls = [dict(client.ready, readyState="BUILDING", aliasAssigned=False), dict(client.ready, aliasAssigned=False)]
    release.deploy(release_config, client, sleep=lambda _: None)
    post = next(call for call in client.calls if call[0] == "POST")
    assert post[1] == "/v13/deployments"
    assert post[2]["body"]["gitSource"] == {"type": "github", "repoId": "12345", "ref": "main", "sha": "1" * 40}
    assert post[2]["body"]["target"] == "production"
    assert all(call[2].get("with_git") is True for call in client.calls if call[1].startswith("/v13/deployments/"))


@pytest.mark.parametrize("field,value", [("id", "prj_other"), ("accountId", "team_other"), ("type", "gitlab"), ("repoId", "999"), ("productionBranch", "availability-overhaul")])
def test_wrong_project_owner_repository_or_branch_stops_before_creation(release_config, field, value):
    client = FakeClient(release_config)
    target = client.project if field in {"id", "accountId"} else client.project["link"]
    target[field] = value
    with pytest.raises(release.ReleaseError):
        release.deploy(release_config, client)
    assert not any(call[0] == "POST" for call in client.calls)


@pytest.mark.parametrize("field,value", [("id", "dpl_other"), ("projectId", "prj_other"), ("target", "preview"), ("sha", "2" * 40), ("repoId", "999"), ("ref", "availability-overhaul"), ("type", "gitlab")])
def test_wrong_deployment_proof_never_reports_success(release_config, field, value, capsys):
    client = FakeClient(release_config)
    target = client.ready if field in {"id", "projectId", "target"} else client.ready["gitSource"]
    target[field] = value
    with pytest.raises(release.ReleaseError):
        release.deploy(release_config, client, sleep=lambda _: None)
    assert "alias verified" not in capsys.readouterr().out


@pytest.mark.parametrize("state", ["ERROR", "CANCELED", "UNRECOGNIZED"])
def test_failed_or_unknown_build_state_stops(release_config, state):
    client = FakeClient(release_config)
    client.ready["readyState"] = state
    with pytest.raises(release.ReleaseError):
        release.deploy(release_config, client)


def test_missing_production_alias_or_timeout_cannot_succeed(release_config):
    client = FakeClient(release_config)
    client.ready["alias"] = ["another.example.invalid"]
    ticks = iter([0, 1, 901])
    with pytest.raises(release.ReleaseError):
        release.deploy(release_config, client, clock=lambda: next(ticks), sleep=lambda _: None)
    client.created["id"] = "https://untrusted.example.invalid/"
    with pytest.raises(release.ReleaseError):
        release.deploy(release_config, client)


def set_environment(monkeypatch, config):
    names = {"token": "VERCEL_TOKEN", "owner": "VERCEL_ORG_ID", "project": "VERCEL_PROJECT_ID", "repository": "GITHUB_REPOSITORY_ID", "sha": "CHECKED_SHA"}
    for field, name in names.items():
        monkeypatch.setenv(name, config[field])
    monkeypatch.setenv("PRODUCTION_URL", "https://" + config["hostname"])


@pytest.mark.parametrize("field,value", [("CHECKED_SHA", "main"), ("CHECKED_SHA", "2" * 40), ("GITHUB_REPOSITORY_ID", "other/repo"), ("VERCEL_PROJECT_ID", "../other"), ("PRODUCTION_URL", "http://planner.example.invalid"), ("PRODUCTION_URL", "https://private@planner.example.invalid"), ("PRODUCTION_URL", "https://planner.example.invalid:8443")])
def test_invalid_configuration_or_unchecked_checkout_fails(release_config, monkeypatch, field, value):
    set_environment(monkeypatch, release_config)
    monkeypatch.setenv(field, value)
    with pytest.raises(release.ReleaseError):
        release.configuration(release_config["sha"])


def test_provider_transport_never_follows_redirects_or_exposes_payload(release_config, monkeypatch):
    calls = []
    class Response:
        status_code = 302
        closed = False
        def close(self):self.closed = True
        def iter_content(self, **kwargs):raise AssertionError("Do not read a redirect payload")
    response = Response()
    class Session:
        headers = {}
        trust_env = True
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
            return response
        def close(self):pass
    monkeypatch.setattr(release.requests, "Session", Session)
    client = release.VercelClient(release_config)
    with pytest.raises(release.ReleaseError):
        client.call("GET", "/v9/projects/prj_synthetic")
    assert calls[0][0][1].startswith("https://api.vercel.com/")
    assert calls[0][1]["allow_redirects"] is False
    assert client.session.trust_env is False and response.closed


def test_provider_body_is_bounded(release_config, monkeypatch):
    class Response:
        status_code = 200
        closed = False
        def close(self):self.closed = True
        def iter_content(self, **kwargs):yield b"x" * (2 * 1024 * 1024 + 1)
    response = Response()
    client = release.VercelClient(release_config)
    monkeypatch.setattr(client.session, "request", lambda *args, **kwargs: response)
    with pytest.raises(release.ReleaseError):
        client.call("GET", "/v9/projects/prj_synthetic")
    assert response.closed
    client.close()


def test_malformed_alias_response_never_counts_as_routing_proof(release_config):
    client = FakeClient(release_config)
    client.ready["alias"] = {release_config["hostname"]: "untrusted-shape"}
    with pytest.raises(release.ReleaseError):
        release.deploy(release_config, client)


def test_provider_exception_is_redacted_and_session_closed(release_config, monkeypatch, capsys):
    set_environment(monkeypatch, release_config)
    monkeypatch.setattr(release, "checked_out_sha", lambda: release_config["sha"])
    closed = []
    class BrokenClient:
        def __init__(self, _config):pass
        def call(self, *args, **kwargs):raise requests.Timeout("SYNTHETIC-DO-NOT-PRINT-TOKEN")
        def close(self):closed.append(True)
    monkeypatch.setattr(release, "VercelClient", BrokenClient)
    assert release.main() == 1 and closed
    assert "SYNTHETIC-DO-NOT-PRINT" not in capsys.readouterr().out


def test_every_python_dependency_manifest_is_audited(tmp_path):
    for name in ["requirements.txt", "requirements-dev.txt", "requirements-security.txt", "requirements-release.txt"]:
        (tmp_path / name).write_text("# Synthetic dependency manifest")
    assert {path.name for path in dependency_files(tmp_path)} == {path.name for path in tmp_path.glob("*.txt")}
    with pytest.raises(ValueError):
        dependency_files(tmp_path / "missing")


def test_release_workflow_uses_audited_http_dependencies_and_existing_gate():
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/release.yml").read_text()
    assert "npx" not in workflow and "npm install" not in workflow
    assert "python scripts/security_release.py" in workflow
    assert "pip install -r requirements-release.txt" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "environment: production" in workflow
    assert root / "requirements-release.txt" in dependency_files(root)
