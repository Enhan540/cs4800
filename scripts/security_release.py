"""Release only a checked Git commit through Vercel's HTTPS API."""

import json
import os
import re
import subprocess
import time
from urllib.parse import urlsplit

import requests


class ReleaseError(ValueError):
    """A generic failure that never includes provider payloads or credentials."""


def checked_out_sha():
    result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, check=True, timeout=10)
    return result.stdout.decode("ascii").strip()


def configuration(checkout):
    names = ("VERCEL_TOKEN", "VERCEL_ORG_ID", "VERCEL_PROJECT_ID", "CHECKED_SHA", "GITHUB_REPOSITORY_ID", "PRODUCTION_URL")
    values = {name: os.environ.get(name, "") for name in names}
    sha = values["CHECKED_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or checkout != sha:
        raise ReleaseError("The checked commit does not match this checkout")
    if not re.fullmatch(r"prj_[A-Za-z0-9_]{1,100}", values["VERCEL_PROJECT_ID"]):
        raise ReleaseError("Project configuration is incomplete")
    if not re.fullmatch(r"[A-Za-z0-9_]{3,100}", values["VERCEL_ORG_ID"]):
        raise ReleaseError("Owner configuration is incomplete")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", values["GITHUB_REPOSITORY_ID"]):
        raise ReleaseError("Repository configuration is incomplete")
    if not 16 <= len(values["VERCEL_TOKEN"]) <= 4096 or any(ch.isspace() for ch in values["VERCEL_TOKEN"]):
        raise ReleaseError("Release credential is incomplete")
    origin = urlsplit(values["PRODUCTION_URL"])
    if origin.scheme != "https" or not origin.hostname or origin.port not in {None, 443} or origin.username or origin.password or origin.path not in {"", "/"} or origin.query or origin.fragment:
        raise ReleaseError("Production origin is incomplete")
    return {"token": values["VERCEL_TOKEN"], "owner": values["VERCEL_ORG_ID"], "project": values["VERCEL_PROJECT_ID"], "repository": values["GITHUB_REPOSITORY_ID"], "sha": sha, "hostname": origin.hostname.casefold()}


class VercelClient:
    def __init__(self, config):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Authorization": "Bearer " + config["token"], "Accept": "application/json", "User-Agent": "GroupPlanner-checked-release/1"})
        self.scope = {"teamId": config["owner"]} if config["owner"].startswith("team_") else {}

    def close(self):
        self.session.close()

    def call(self, method, path, *, body=None, with_git=False):
        params = {**self.scope, **({"withGitRepoInfo": "true"} if with_git else {})}
        response = self.session.request(method, "https://api.vercel.com" + path, params=params, json=body, timeout=(3.05, 20), allow_redirects=False, stream=True)
        try:
            if response.status_code not in {200, 201}:
                raise ReleaseError("Provider request failed")
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise ReleaseError("Provider response exceeded its bound")
                chunks.append(chunk)
            value = json.loads(b"".join(chunks))
            if not isinstance(value, dict) or "error" in value:
                raise ReleaseError("Provider response is incomplete")
            return value
        finally:
            response.close()


def deploy(config, client, *, clock=time.monotonic, sleep=time.sleep):
    project = client.call("GET", "/v9/projects/" + config["project"])
    link = project.get("link") or {}
    if (project.get("id") != config["project"] or project.get("accountId") != config["owner"]
            or not isinstance(link, dict) or link.get("type") != "github"
            or str(link.get("repoId")) != config["repository"] or link.get("productionBranch") != "main"):
        raise ReleaseError("Project ownership or repository binding is unverified")
    name = project.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", name):
        raise ReleaseError("Project name is unverified")
    created = client.call("POST", "/v13/deployments", body={"name": name, "project": config["project"], "target": "production", "gitSource": {"type": "github", "repoId": config["repository"], "ref": "main", "sha": config["sha"]}})
    deployment_id = created.get("id")
    if not isinstance(deployment_id, str) or not re.fullmatch(r"dpl_[A-Za-z0-9_]{1,100}", deployment_id):
        raise ReleaseError("Deployment identity is unverified")
    print("Checked-commit deployment requested; waiting for provider verification.", flush=True)
    deadline = clock() + 15 * 60
    while clock() < deadline:
        current = client.call("GET", "/v13/deployments/" + deployment_id, with_git=True)
        source = current.get("gitSource") or {}
        project_info = current.get("project") or {}
        current_project = current.get("projectId") or (project_info.get("id") if isinstance(project_info, dict) else None)
        if (current.get("id") != deployment_id or current_project != config["project"] or current.get("target") != "production"
                or not isinstance(source, dict) or source.get("type") != "github"
                or str(source.get("repoId")) != config["repository"] or source.get("sha") != config["sha"] or source.get("ref") != "main"):
            raise ReleaseError("Deployment commit or project proof is unverified")
        state = current.get("readyState")
        if state == "READY":
            aliases = current.get("alias") or []
            if not isinstance(aliases, list):
                raise ReleaseError("Production routing response is incomplete")
            if current.get("aliasError"):
                raise ReleaseError("Production routing failed")
            if current.get("aliasAssigned") is True and config["hostname"] in {name.casefold() for name in aliases if isinstance(name, str)}:
                print("Checked commit and configured production alias verified.", flush=True)
                return
        elif state not in {"QUEUED", "INITIALIZING", "BUILDING"}:
            raise ReleaseError("Deployment did not reach a verified ready state")
        sleep(10)
    raise ReleaseError("Deployment verification timed out")


def main():
    client = None
    try:
        config = configuration(checked_out_sha())
        client = VercelClient(config)
        deploy(config, client)
        return 0
    except (ReleaseError, ValueError, TypeError, requests.RequestException, OSError, subprocess.SubprocessError):
        print("Release FAILED or incomplete; inspect the deployment privately. No verified success was recorded.")
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())

