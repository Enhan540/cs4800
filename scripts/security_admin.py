"""Inspect protection or explicitly protect an unprotected branch; preserve existing rules."""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
ACTIONS_APP_ID = 15368
API_VERSION = "2026-03-10"
CHECK_NAME = "security (current)"
REPOSITORY = "Enhan540/cs4800"


def response_json(response):
    try:
        if response.status_code != 200:
            raise ValueError("Protection response unavailable")
        content, size = [], 0
        for chunk in response.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > 1024 * 1024:
                raise ValueError("Protection response exceeded its bound")
            content.append(chunk)
        value = json.loads(b"".join(content))
        if not isinstance(value, dict) or "error" in value:
            raise ValueError("Protection response is incomplete")
        return value
    finally:
        response.close()


def protection_verified(state, status):
    reviews = state.get("required_pull_request_reviews") or {}
    required = [item for item in status.get("checks", []) if isinstance(item, dict) and item.get("context") == CHECK_NAME]
    bypass = reviews.get("bypass_pull_request_allowances") or {}
    count = reviews.get("required_approving_review_count")
    return bool(
        required and all(item.get("app_id") == ACTIONS_APP_ID for item in required)
        and status.get("strict") is True
        and state.get("enforce_admins", {}).get("enabled") is True
        and isinstance(count, int) and not isinstance(count, bool) and count >= 1
        and reviews.get("dismiss_stale_reviews") is True
        and reviews.get("require_last_push_approval") is True
        and isinstance(bypass, dict) and not any(bypass.get(key) for key in ("users", "teams", "apps"))
        and state.get("allow_force_pushes", {}).get("enabled") is False
        and state.get("allow_deletions", {}).get("enabled") is False
        and state.get("required_conversation_resolution", {}).get("enabled") is True
    )


def policy_verified(policy):
    if not isinstance(policy, dict):
        return False
    state = {**policy, **{key: {"enabled": policy.get(key)} for key in ("enforce_admins", "allow_force_pushes", "allow_deletions", "required_conversation_resolution")}}
    try:
        return protection_verified(state, policy.get("required_status_checks") or {})
    except (AttributeError, TypeError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Protect unprotected branches; preserve and inspect existing rules")
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Branch protection unverified: an administrator GITHUB_TOKEN is required.")
        return 1
    try:
        policy = json.loads((ROOT / ".security/branch-protection.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("Branch protection unverified: reviewed policy is unavailable.")
        return 1
    if not policy_verified(policy):
        print("Branch protection unverified: policy does not meet required safeguards.")
        return 1
    complete = True
    with requests.Session() as client:
        client.trust_env = False
        client.headers.update({"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "Content-Type": "application/json", "X-GitHub-Api-Version": API_VERSION})
        for branch in ("main",):
            url = "https://api.github.com/repos/" + REPOSITORY + "/branches/" + quote(branch, safe="") + "/protection"
            def call(method, target, body=None):
                return response_json(client.request(method, target, json=body, timeout=(3.05, 20), allow_redirects=False, stream=True))
            try:
                if args.apply:
                    branch_state = call("GET", url.removesuffix("/protection"))
                    if branch_state.get("protected") is False:
                        call("PUT", url, policy)
                    elif branch_state.get("protected") is not True:
                        raise ValueError("Existing protection state is unverified")
                state = call("GET", url)
                status = call("GET", url + "/required_status_checks")
                passed = protection_verified(state, status)
                complete = complete and passed
                print(branch + (": protection verified" if passed else ": protection incomplete"))
                if args.apply and branch_state.get("protected") is True and not passed:
                    print(branch + ": existing rules preserved; strengthen the missing safeguards without replacing stricter settings")
            except (requests.RequestException, ValueError, TypeError, AttributeError):
                complete = False
                print(branch + ": protection unverified; inspect access and settings privately")
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
