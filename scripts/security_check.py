"""Fail-closed security checks. Detailed evidence never goes to shared logs."""

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {"bandit": "1.9.4", "pip-audit": "2.10.1", "playwright": "1.65.2", "pytest": "9.0.3"}
IMAGES = {"gitleaks": "ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f", "semgrep": "semgrep/semgrep@sha256:6e1b04340e60e910fbb4ddbf3c857ab0918f064417ad5ac530014ccb3cf8c8f1", "zap": "ghcr.io/zaproxy/zaproxy@sha256:9624669db72f54d86c4b46a7064518bd9bf79d8c61a4c31befa9923489f328af"}
BLOCKING_OUTCOMES = {"credential_exposure", "account_takeover", "cross_user_access"}
OPEN_STATES = {"open", "unverified", "candidate"}
PRIVATE_ARTIFACT_NAMES = {
    "security-risk-register.json", "asvs-evidence.json", "asvs-checklist.json",
    "coverage-summary.json", "route-permission-matrix.json", "audit-report.json",
    "audit-report.html", "resume-checkpoint.json", "RESUME_CHECKPOINT.txt",
    "current-route-permission-matrix.json", "current-authentication-review.json",
    "release-tool-remediation-review.json", "active-release-audit-state.json",
    "active-work-checkpoint.json", "work-checkpoint.json", "usage-guard.json",
    "deployment-dependency-audit.json", "deployment-candidate-package-lock.json",
    "release-python-dependency-audit.json", "release-remaining-npm-audit.json",
    "main-source.zip", "availability-overhaul-source.zip",
    "group-permission-scope-review.json", "group-permission-review.json",
    "audit-tooling-scope-review.json", "audit-tooling-review.json",
    "scanner-triage-review.json", "asvs-download-verification.json",
    "github-actions-app-evidence.json",
    "pending-blocking-findings.json", "removed-member-notification-probe.json",
    "notification-revocation-review.json",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def execute(command, cwd=None, env=None, timeout=600, data=None):
    return subprocess.run(command, cwd=cwd or ROOT, env=env, input=data, capture_output=True, timeout=timeout, check=False)


def git(*args, cwd=None):
    result = execute(["git", *args], cwd=cwd or ROOT)
    if result.returncode:
        raise RuntimeError("Git snapshot unavailable")
    return result.stdout.decode("utf-8").strip()


def private_home():
    if os.getenv("CI"):
        base = Path(tempfile.mkdtemp(prefix="group-planner-security-"))
    else:
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "GroupPlanner" / "Security"
    resolved = base.resolve()
    if resolved.is_relative_to(ROOT) or "onedrive" in str(resolved).casefold():
        raise RuntimeError("Security reports require local storage outside the repository and sync folders")
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        resolved.chmod(0o700)
    return resolved


def is_blocking(finding):
    return finding.get("status") != "resolved" and (
        finding.get("severity", "").upper() in {"HIGH", "CRITICAL", "UNKNOWN"}
        or finding.get("outcome") in BLOCKING_OUTCOMES
    )


def parse_secret_report(returncode, data):
    if returncode not in (0, 42):
        raise ValueError("Secret scanner execution failed")
    report = json.loads(data or (b"[]" if returncode == 0 else b""))
    if not isinstance(report, list) or (returncode == 42 and not report):
        raise ValueError("Secret scanner report incomplete")
    return report


def merge_findings(register, incoming, branch, completed_tools):
    existing = register.setdefault("findings", [])
    by_key = {(f.get("fingerprint"), f.get("branch")): f for f in existing}
    seen = set()
    for finding in incoming:
        key = (finding["fingerprint"], branch)
        seen.add(key)
        previous = by_key.get(key)
        if previous:
            previous.update({k: v for k, v in finding.items() if k not in {"status", "owner", "deadline", "first_seen"}})
            previous["last_seen"] = now()
            # A matching rule/location can now refer to different source content.
            # Private historical triage cannot suppress a fresh scanner result.
            if previous.get("status") not in OPEN_STATES:
                previous["status"] = "candidate"
                previous["triage_invalidated_at"] = now()
        else:
            days = {"MEDIUM": 14, "LOW": 30, "INFO": 90}.get(finding["severity"], 0)
            item = {**finding, "branch": branch, "affected_branches": [branch], "reproduction_steps": f"Run the pinned {finding['tool']} check against the recorded commit or snapshot.", "retest_result": "Pending", "first_seen": now(), "last_seen": now(), "owner": "repository maintainer", "deadline": (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()}
            existing.append(item)
            by_key[key] = item
    for finding in existing:
        if finding.get("branch") == branch and finding.get("tool") in completed_tools and (finding.get("fingerprint"), branch) not in seen:
            finding.update(status="resolved", retest_result="Absent in a completed repeat scan", last_reviewed=now())
    return register


def finding(tool, identifier, severity, location="", line=0, outcome="", **extra):
    fingerprint = hashlib.sha256(f"{tool}:{identifier}:{location}:{line}".encode()).hexdigest()
    return {"id": fingerprint[:12], "fingerprint": fingerprint, "tool": tool, "rule": identifier, "severity": severity.upper(), "location": location, "line": line, "outcome": outcome, "confidence": "scanner candidate", "status": "candidate", "evidence": "Reproduced by the recorded scanner run; source and matched secret values are deliberately omitted", "remediation": "Review the rule and fix the cause; record evidence for any false positive", **extra}


def private_reports_absent(tree):
    return not any(path.name in PRIVATE_ARTIFACT_NAMES for path in tree.rglob("*"))


def dependency_files(tree):
    files = sorted(path for path in tree.glob("requirements*.txt") if path.is_file())
    if not files:
        raise ValueError("Dependency manifests are missing")
    return files


def test_environment():
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME", "VIRTUAL_ENV", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    for name in ("MONGO_URI", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "GOOGLE_CALENDAR_REDIRECT_URI", "RESEND_API_KEY", "FROM_EMAIL", "VERCEL", "VERCEL_ENV", "APP_ENV", "APP_BASE_URL"):
        env[name] = ""
    env.update(SECRET_KEY=os.urandom(32).hex(), EMAIL_DELIVERY_MODE="console", SESSION_COOKIE_SECURE="false", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", RUN_SECURITY_BROWSER="1", RUN_SECURITY_INTEGRATION="1")
    return env


def make_snapshot(destination, mode, ref, worktree=False):
    if worktree:
        names = set(git("ls-files", "--cached", "--others", "--exclude-standard").splitlines())
        for name in names:
            source = ROOT / name
            if source.is_file() and not source.is_symlink():
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        identity = hashlib.sha256(b"".join(name.encode() + (destination / name).read_bytes() for name in sorted(names) if (destination / name).is_file())).hexdigest()
    elif mode == "commit":
        # Capture one immutable index tree so staging another file during a check
        # cannot change either the tested content or the recorded identity.
        identity = git("write-tree")
        result = execute(["git", "archive", "--format=tar", identity])
        if result.returncode:
            raise RuntimeError("Cannot export the staged snapshot")
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            archive.extractall(destination, filter="data")
    else:
        identity = git("rev-parse", f"{ref}^{{commit}}")
        result = execute(["git", "archive", "--format=tar", identity])
        if result.returncode:
            raise RuntimeError("Cannot export requested commit")
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            archive.extractall(destination, filter="data")
    return identity


def collect(args):
    home = private_home()
    lock = home / "security-run.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(descriptor, str(os.getpid()).encode())
    os.close(descriptor)
    try:
        return collect_locked(args, home)
    finally:
        lock.unlink(missing_ok=True)


def collect_locked(args, home):
    register_path = home / "security-risk-register.json"
    register = json.loads(register_path.read_text(encoding="utf-8")) if register_path.exists() else {"schema_version": 1, "findings": [], "runs": []}
    symbolic = execute(["git", "symbolic-ref", "--short", "-q", "HEAD"])
    branch = args.branch or args.ref or os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME") or symbolic.stdout.decode().strip() or ("detached-" + git("rev-parse", "--short", "HEAD"))
    ref = args.ref or os.environ.get("PRE_COMMIT_TO_REF") or "HEAD"
    directory = home / ("run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
    directory.mkdir(mode=0o700)
    checks, findings, completed = [], [], set()

    def run(name, command, *, cwd, env=None, expected=(0,), timeout=600):
        try:
            result = execute(command, cwd=cwd, env=env, timeout=timeout)
            # Keep command output private; report generic check status in CI.
            (directory / f"{name}.log").write_text(json.dumps({"name": name, "exit_code": result.returncode, "output_bytes": len(result.stdout) + len(result.stderr)}), encoding="utf-8")
            ok = result.returncode in expected
            checks.append({"name": name, "completed": ok, "exit_code": result.returncode, "at": now()})
            print(f"{name}: {'completed' if ok else 'FAILED / incomplete'}")
            return result
        except (OSError, subprocess.TimeoutExpired):
            checks.append({"name": name, "completed": False, "at": now()})
            print(f"{name}: FAILED / unavailable")
            return None

    with tempfile.TemporaryDirectory(prefix="group-planner-snapshot-") as scratch:
        tree = Path(scratch)
        identity = make_snapshot(tree, args.mode, ref, args.worktree)
        private_files_absent = private_reports_absent(tree)
        checks.append({"name": "private-report-exclusion", "completed": private_files_absent})
        print("private-artifact-policy: " + ("completed" if private_files_absent else "FAILED / private report included"))
        env = test_environment()
        env["PYTHONPATH"] = str(tree)
        # Controls come from this checkout; audited application code comes only from the target snapshot.
        if args.mode == "audit" and args.ref:
            for relative in ("tests/security", ".security", "scripts/security_inventory.py"):
                source, target = ROOT / relative, tree / relative
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                elif source.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            (tree / "tests/security/test_gate.py").unlink(missing_ok=True)
        for package, version in TOOLS.items():
            try:
                installed = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                installed = "missing"
            checks.append({"name": f"tool-{package}", "completed": installed == version, "expected": version, "installed": installed})
        inventory = run("route-policy", [sys.executable, "scripts/security_inventory.py", "--check"], cwd=tree, env=env)
        if not args.advisories_only:
            test_python = sys.executable
            runtime_context = None
            mismatched = False
            for line in (tree / "requirements.txt").read_text(encoding="utf-8").splitlines():
                match = re.fullmatch(r"([\w.-]+)==([^\s]+)", line)
                if match:
                    try:
                        mismatched |= importlib.metadata.version(match[1]) != match[2]
                    except importlib.metadata.PackageNotFoundError:
                        mismatched = True
            if mismatched:
                runtime_context = tempfile.TemporaryDirectory(prefix="group-planner-test-runtime-")
                environment = Path(runtime_context.name)
                venv.EnvBuilder(with_pip=True).create(environment)
                test_python = str(environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
                run("snapshot-dependencies", [test_python, "-m", "pip", "install", "-r", "requirements-dev.txt", "playwright==" + TOOLS["playwright"]], cwd=tree, env=env)
            test_args = ["-m", "security and not browser"] if args.mode == "commit" else []
            junit = directory / "tests.xml"
            run("python-tests", [test_python, "-m", "pytest", "-q", "--disable-warnings", f"--junitxml={junit}", *test_args], cwd=tree, env=env)
            try:
                cases = ET.parse(junit).getroot().findall(".//testcase")
                policy = json.loads((tree / ".security/routes.json").read_text(encoding="utf-8"))
                expected = sum(len(row["methods"]) for row in policy if row["access"] == "authenticated")
                actual = sum(case.get("name", "").startswith("test_every_protected_route_rejects_anonymous[") for case in cases)
                security_cases = [case for case in cases if ".security." in case.get("classname", "")]
                checks.append({"name": "required-security-coverage", "completed": actual == expected and bool(security_cases) and not any(case.find("skipped") is not None for case in security_cases)})
                safe_results = [{"test": case.get("name", "").split("[", 1)[0], "class": case.get("classname", ""), "status": "failed" if case.find("failure") is not None or case.find("error") is not None else "skipped" if case.find("skipped") is not None else "passed"} for case in cases]
                (directory / "test-results.json").write_text(json.dumps(safe_results, indent=2), encoding="utf-8")
            except (OSError, ValueError, ET.ParseError):
                checks.append({"name": "required-security-coverage", "completed": False})
            finally:
                junit.unlink(missing_ok=True)
            if runtime_context is not None:
                runtime_context.cleanup()
            js_tests = sorted(str(p.relative_to(tree)) for p in (tree / "tests").rglob("*.test.cjs"))
            run("javascript-tests", [shutil.which("node") or "node", "--test", *js_tests], cwd=tree, env=env)
            bandit = run("bandit", [sys.executable, "-m", "bandit", "--ignore-nosec", "-q", "-r", "app.py", "services", "scripts", "-f", "json"], cwd=tree, expected=(0, 1))
            if bandit and bandit.returncode in (0, 1):
                try:
                    report = json.loads(bandit.stdout)
                    if report.get("errors"):
                        raise ValueError("Scanner skipped files")
                    findings += [finding("bandit", item["test_id"], item["issue_severity"], item["filename"].replace(str(tree), ""), item["line_number"]) for item in report["results"]]
                    completed.add("bandit")
                except (ValueError, KeyError):
                    checks.append({"name": "bandit-report", "completed": False})
            semgrep = run("semgrep", ["docker", "run", "--rm", "--network=none", "-v", f"{tree}:/src:ro", IMAGES["semgrep"], "semgrep", "scan", "--config=/src/.security/semgrep.yml", "--json", "--metrics=off", "--disable-version-check", "--disable-nosem", "/src/app.py", "/src/services", "/src/static/js", "/src/scripts"], cwd=tree, expected=(0, 1))
            if semgrep and semgrep.returncode in (0, 1):
                try:
                    report = json.loads(semgrep.stdout)
                    if report.get("errors"):
                        raise ValueError("Scanner skipped files")
                    findings += [finding("semgrep", item["check_id"], {"ERROR": "HIGH", "WARNING": "MEDIUM", "INFO": "LOW"}[item["extra"]["severity"]], item["path"], item["start"]["line"]) for item in report["results"]]
                    completed.add("semgrep")
                except (ValueError, KeyError):
                    checks.append({"name": "semgrep-report", "completed": False})
            secret_scans = [
                ("gitleaks-tree", tree, ["dir", "/scan"]),
                ("gitleaks-history", ROOT, ["git", "/scan", f"--log-opts={ref}"]),
            ]
            if args.mode == "commit":
                secret_scans = secret_scans[:1]
            for label, mount, command in secret_scans:
                # Scanner JSON is the only output accepted; --redact removes matched credentials.
                git_options = []
                if label == "gitleaks-history":
                    common = Path(git("rev-parse", "--git-common-dir"))
                    common = common if common.is_absolute() else ROOT / common
                    git_options = ["-v", f"{common.resolve()}:/git-data:ro", "-e", "GIT_DIR=/git-data", "-e", "GIT_WORK_TREE=/scan", "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory", "-e", "GIT_CONFIG_VALUE_0=/scan"]
                report_path = directory / (label + ".json")
                result = run(label, ["docker", "run", "--rm", "--network=none", "-v", f"{mount}:/scan:ro", "-v", f"{tree / '.security'}:/policy:ro", "-v", f"{directory}:/reports", *git_options, IMAGES["gitleaks"], *command, "--config=/policy/gitleaks.toml", "--redact=100", "--ignore-gitleaks-allow", "--exit-code=42", "--report-format=json", f"--report-path=/reports/{label}.json", "--no-banner"], cwd=tree, expected=(0, 42))
                if result and result.returncode in (0, 42):
                    try:
                        report = parse_secret_report(result.returncode, report_path.read_bytes() if report_path.exists() else b"")
                        normalized = [finding(label, item["RuleID"], "CRITICAL", item["File"], item["StartLine"], "credential_exposure", affected_commit=item.get("Commit", "")) for item in report]
                        findings += normalized
                        report_path.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
                        completed.add(label)
                    except (ValueError, KeyError):
                        checks.append({"name": label + "-report", "completed": False})
        requirement_args = [argument for path in dependency_files(tree) for argument in ("-r", path.name)]
        deps = run("pip-audit", [sys.executable, "-m", "pip_audit", *requirement_args, "--format=json", "--progress-spinner=off"], cwd=tree, expected=(0, 1))
        if deps and deps.returncode in (0, 1):
            try:
                report = json.loads(deps.stdout)
                for dependency in report["dependencies"]:
                    if dependency.get("skip_reason"):
                        checks.append({"name": "dependency-skipped", "completed": False})
                    for item in dependency.get("vulns", []):
                        findings.append(finding("pip-audit", item["id"], "UNKNOWN", dependency["name"], installed_version=dependency["version"], fix_versions=item.get("fix_versions", [])))
                completed.add("pip-audit")
            except (ValueError, KeyError):
                checks.append({"name": "pip-audit-report", "completed": False})
        npm = run("npm-audit", [shutil.which("npm.cmd") or shutil.which("npm") or "npm", "audit", "--json", "--ignore-scripts"], cwd=tree, expected=(0, 1))
        if npm and npm.returncode in (0, 1):
            try:
                report = json.loads(npm.stdout)
                if "error" in report or "vulnerabilities" not in report:
                    raise ValueError("Incomplete npm audit")
                findings += [finding("npm-audit", name, item["severity"], name) for name, item in report["vulnerabilities"].items()]
                completed.add("npm-audit")
            except (ValueError, KeyError):
                checks.append({"name": "npm-audit-report", "completed": False})
        if args.mode != "commit" and not args.advisories_only:
            dynamic_file = directory / "dynamic-findings.json"
            dynamic = run("dynamic-scan", [sys.executable, str(ROOT / "scripts/security_dynamic.py"), "--source", str(tree), "--findings-file", str(dynamic_file)], cwd=tree, env=env, timeout=900)
            if dynamic_file.exists():
                findings += json.loads(dynamic_file.read_text(encoding="utf-8"))
                if dynamic and dynamic.returncode == 0:
                    completed.add("zap")

    for item in findings:
        item.update(commit=identity, scanned_at=now())
    register = merge_findings(register, findings, branch, completed)
    current = [item for item in register["findings"] if item.get("branch") == branch and item.get("status") != "resolved"]
    prior_runs = [run for run in register.get("runs", []) if run.get("branch") == branch]
    if not os.getenv("CI"):
        if not prior_runs or prior_runs[-1].get("identity") != identity:
            print("Private review: missing or stale for this snapshot; a fresh scan is being recorded.")
        for item in current:
            print(f"RISK {item['id']} {item['severity']} {item.get('rule', '')}: {item.get('location', '')} owner={item.get('owner')} deadline={item.get('deadline')}")
    blocking = sum(is_blocking(item) for item in current)
    complete = all(check["completed"] for check in checks)
    run_record = {"at": now(), "branch": branch, "identity": identity, "mode": args.mode, "worktree": args.worktree, "tools": TOOLS, "images": IMAGES, "advisories_checked_at": now(), "checks": checks, "open_findings": len(current), "blocking_findings": blocking, "passed": complete and blocking == 0}
    register["runs"] = [*register.get("runs", []), run_record][-200:]
    (directory / "summary.json").write_text(json.dumps(run_record, indent=2), encoding="utf-8")
    temporary_register = register_path.with_suffix(".tmp")
    temporary_register.write_text(json.dumps(register, indent=2), encoding="utf-8")
    temporary_register.replace(register_path)
    print(f"Security: {len(current)} unresolved findings; {blocking} blocking; {'all checks complete' if complete else 'INCOMPLETE checks'}." )
    if os.getenv("CI"):
        shutil.rmtree(home)
    return 0 if run_record["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("commit", "push", "ci", "audit"))
    parser.add_argument("--ref")
    parser.add_argument("--branch")
    parser.add_argument("--worktree", action="store_true")
    parser.add_argument("--advisories-only", action="store_true")
    args = parser.parse_args()
    if args.worktree and args.mode != "audit":
        parser.error("Working-tree checks are audit-only and cannot satisfy commit/push/CI gates")
    try:
        return collect(args)
    except Exception:
        print("Security check FAILED: incomplete execution. No clean result was recorded.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
