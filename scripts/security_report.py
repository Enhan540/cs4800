"""Build the private audit inventory and ASVS worksheet without publishing evidence."""

import ast
import csv
import hashlib
import html
import io
import json
from pathlib import Path
import subprocess
import requests

if __package__:
    from .security_check import ROOT, now, private_home
else:
    from security_check import ROOT, now, private_home

ASVS_URL = "https://raw.githubusercontent.com/OWASP/ASVS/v5.0.0/5.0/docs_en/OWASP_Application_Security_Verification_Standard_5.0.0_en.csv"
ASVS_SHA256 = "98c8fe911b9edb403af8ee05d3ce8201ecac2659e313b053890a62847cdcf680"
EVIDENCE_STATES = {"passed", "finding", "not applicable with evidence", "unverified"}


def fetch_asvs():
    with requests.Session() as client:
        client.trust_env = False
        with client.get(ASVS_URL, timeout=(3.05, 30), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError("Reviewed ASVS source is unavailable")
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > 1024 * 1024:
                    raise ValueError("ASVS source exceeded its size bound")
                chunks.append(chunk)
    data = b"".join(chunks)
    if hashlib.sha256(data).hexdigest() != ASVS_SHA256:
        raise ValueError("ASVS source changed; review the standard before updating evidence")
    return data


def read_private_evidence(home):
    path = home / "asvs-evidence.json"
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("Private evidence has an unsupported format")
    return document


def control_evidence(document, branch, commit, requirement):
    default = ("unverified", "No current private evidence establishes this requirement for the recorded commit.")
    branches = document.get("branches", {})
    entry = branches.get(branch, {}) if isinstance(branches, dict) else {}
    if not isinstance(entry, dict) or entry.get("commit") != commit:
        return default
    controls = entry.get("controls", {})
    item = controls.get(requirement, {}) if isinstance(controls, dict) else {}
    if not isinstance(item, dict) or item.get("status") not in EVIDENCE_STATES or not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
        return default
    return item["status"], item["evidence"]


def source(ref, name):
    result = subprocess.run(["git", "show", f"{ref}:{name}"], cwd=ROOT, capture_output=True, check=True)
    return result.stdout.decode("utf-8")


def main():
    home = private_home()
    if (home / "security-run.lock").exists():
        raise RuntimeError("Finish active security checks before updating the audit report")
    private_evidence = read_private_evidence(home)
    data = fetch_asvs()
    (home / "asvs-5.0.0-source.csv").write_bytes(data)
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    checklist = []
    branch_inventory = {}
    for branch in ("main",):
        app_source = source(branch, "app.py")
        tree = ast.parse(app_source)
        operations, collections, integrations, routes = [], set(), set(), []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Attribute) and isinstance(node.func.value.value, ast.Name) and node.func.value.value.id in {"db", "database", "require_database"}:
                    collections.add(node.func.value.attr)
                if node.func.attr in {"get", "post", "request", "authorize_redirect", "authorize_access_token"}:
                    integrations.add(ast.unparse(node.func))
            if not isinstance(node, ast.FunctionDef):
                continue
            operations.append({"name": node.name, "line": node.lineno})
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute) or ast.unparse(dec.func.value) != "app" or dec.func.attr not in {"route", "get", "post", "delete", "patch", "put"}:
                    continue
                methods = next((ast.literal_eval(k.value) for k in dec.keywords if k.arg == "methods"), ["GET" if dec.func.attr == "route" else dec.func.attr.upper()])
                path = ast.literal_eval(dec.args[0])
                routes.append({"endpoint": node.name, "path": path, "methods": methods, "authentication": "login_required" in [ast.unparse(d) for d in node.decorator_list], "anonymous_coverage": "parameterized route security regression", "object_isolation_coverage": "parameterized unrelated-user denial and no-write checks" if "<" in path and "login_required" in [ast.unparse(d) for d in node.decorator_list] else "Own-account/public behavior: focused tests plus manual review required", "positive_role_coverage": "Some focused owner/admin/member tests; full per-route authorized and per-permission matrix remains unverified"})
        files = subprocess.run(["git", "ls-tree", "-r", "--name-only", branch], cwd=ROOT, capture_output=True, check=True).stdout.decode().splitlines()
        services = {}
        for file in files:
            if file.startswith(("services/", "scripts/")) and file.endswith(".py"):
                services[file] = [node.name for node in ast.walk(ast.parse(source(branch, file))) if isinstance(node, ast.FunctionDef)]
        sha = subprocess.run(["git", "rev-parse", branch], cwd=ROOT, capture_output=True, check=True).stdout.decode().strip()
        branch_inventory[branch] = {"commit": sha, "routes": routes, "operations": operations, "services": services, "collections": sorted(collections), "integrations": sorted(integrations), "trust_boundaries": ["browser to Flask", "Flask session to account identity", "account to scheduling member", "group role to shared document", "Flask to Atlas", "Flask to Google OIDC/Calendar", "Flask to untrusted iCal origin", "Flask to Resend", "GitHub runner to Vercel", "public HTTP/2 edge to WSGI application"]}
        for row in rows:
            status, evidence = control_evidence(private_evidence, branch, sha, row["req_id"])
            checklist.append({"branch": branch, "commit": sha, "requirement": row, "status": status, "evidence": evidence})
    (home / "asvs-checklist.json").write_text(json.dumps({"standard": "OWASP ASVS 5.0.0", "source": ASVS_URL, "sha256": hashlib.sha256(data).hexdigest(), "at": now(), "items": checklist}, indent=2), encoding="utf-8")
    (home / "route-permission-matrix.json").write_text(json.dumps(branch_inventory, indent=2), encoding="utf-8")
    topics = private_evidence.get("coverage", [])
    if not isinstance(topics, list):
        raise ValueError("Private coverage evidence must be a list")
    current = {branch: item["commit"] for branch, item in branch_inventory.items()}
    coverage_current = private_evidence.get("coverage_commits") == current
    topics = [dict(item, status=item.get("status") if coverage_current and item.get("status") in EVIDENCE_STATES else "unverified") for item in topics if isinstance(item, dict)]
    if not topics:
        topics = [{"area": "Manual and hosted coverage", "status": "unverified", "evidence": "No current private coverage evidence was supplied."}]
    (home / "coverage-summary.json").write_text(json.dumps(topics, indent=2), encoding="utf-8")
    register_file = home / "security-risk-register.json"
    register = json.loads(register_file.read_text(encoding="utf-8")) if register_file.exists() else {}
    # Normalize a report view without changing the private source register.
    unique = {}
    for item in register.get("findings", []):
        unique[(item["fingerprint"], item["branch"])] = item
    register["findings"] = list(unique.values())
    for item in register["findings"]:
        others = [candidate for candidate in register["findings"] if candidate.get("tool") == item.get("tool") and candidate.get("rule") == item.get("rule") and candidate.get("location", "").replace("\\", "/").removeprefix("/scan/") == item.get("location", "").replace("\\", "/").removeprefix("/scan/") and candidate.get("branch") != item.get("branch")]
        item["classification"] = "shared candidate" if others else "main-only candidate; context review required"
    report = {"at": now(), "branches": {branch: {"commit": item["commit"], "route_count": len(item["routes"])} for branch, item in branch_inventory.items()}, "coverage": topics, "coverage_evidence_current": coverage_current, "runs": register.get("runs", []), "findings": register.get("findings", []), "release_readiness": "unverified"}
    (home / "audit-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    document = '<!doctype html><meta charset="utf-8"><title>Private security audit</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>Private Group Planner security audit</h1><pre>' + html.escape(json.dumps(report, indent=2)) + '</pre>'
    (home / "audit-report.html").write_text(document, encoding="utf-8")
    print(f"Private report refreshed: {len(checklist)} ASVS branch/control entries; {sum(len(item['routes']) for item in branch_inventory.values())} branch/route entries.")


if __name__ == "__main__":
    main()
