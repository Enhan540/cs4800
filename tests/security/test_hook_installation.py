import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.security_install_hooks import install_hooks

pytestmark = pytest.mark.security


def git(root, *arguments, env=None):
    return subprocess.run([shutil.which("git"), *arguments], cwd=root, env=env, capture_output=True, text=True, check=False)


@pytest.fixture
def hook_repo(tmp_path):
    assert git(tmp_path, "init", "-b", "hook-test").returncode == 0
    (tmp_path / "scripts").mkdir()
    return tmp_path


def test_commit_hook_uses_the_installing_interpreter_without_activation(hook_repo):
    root = hook_repo
    marker = root / "hook-ran.txt"
    (root / "scripts/security_check.py").write_text("import pathlib,sys\npathlib.Path('hook-ran.txt').write_text(sys.argv[1])\nraise SystemExit(1)\n")
    (root / "example.txt").write_text("Synthetic fixture")
    assert git(root, "add", "scripts", "example.txt").returncode == 0
    assert install_hooks(root, sys.executable) == 0
    environment = os.environ.copy()
    git_directory = Path(shutil.which("git")).parent
    environment["PATH"] = str(git_directory) + os.pathsep + str(git_directory.parent / "usr/bin")
    environment.pop("VIRTUAL_ENV", None)
    result = git(root, "-c", "user.name=Security fixture", "-c", "user.email=security@example.invalid", "commit", "-m", "Synthetic fixture", env=environment)
    assert result.returncode != 0
    assert marker.read_text() == "commit"
    assert git(root, "rev-parse", "--verify", "HEAD").returncode != 0


def test_unknown_hooks_are_preserved_before_either_hook_is_changed(hook_repo):
    root = hook_repo
    hooks = root / ".git/hooks"
    commit = hooks / "pre-commit"
    commit.write_text("#!/bin/sh\n# User-owned hook\nexit 0\n")
    assert install_hooks(root) == 1
    assert "User-owned hook" in commit.read_text()
    assert not (hooks / "pre-push").exists()


def test_custom_hook_path_is_preserved(hook_repo):
    root = hook_repo
    assert git(root, "config", "core.hooksPath", ".user-hooks").returncode == 0
    assert install_hooks(root) == 1
    assert not (root / ".user-hooks").exists()


def test_installing_owned_hooks_twice_is_idempotent(hook_repo):
    assert install_hooks(hook_repo) == 0
    before = (hook_repo / ".git/hooks/pre-commit").read_text()
    assert install_hooks(hook_repo) == 0
    assert (hook_repo / ".git/hooks/pre-commit").read_text() == before


@pytest.mark.parametrize("restage_during_check", [False, True])
def test_commit_hook_rejects_content_staged_after_its_snapshot(hook_repo, restage_during_check):
    import json
    from scripts import security_check

    root = hook_repo
    reports = root.parent / (root.name + "-private-reports")
    reports.mkdir()
    driver = f'''import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("gate_under_test", {str(Path(security_check.__file__).resolve())!r})
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
gate.ROOT = Path.cwd()
reports = Path({str(reports)!r})
gate.private_home = lambda: reports
real_execute = gate.execute

def external_check(command, **options):
    if command[0] == "git":
        return real_execute(command, **options)
    if "pip_audit" in command:
        (reports / "checked-tree.txt").write_text(gate.git("write-tree"))
        if {restage_during_check!r}:
            (gate.ROOT / "example.txt").write_text("Unchecked replacement content")
            staged = real_execute(["git", "add", "example.txt"], cwd=gate.ROOT)
            if staged.returncode:
                raise RuntimeError("Synthetic restaging failed")
        output = {{"dependencies": []}}
    elif "audit" in command:
        output = {{"vulnerabilities": {{}}}}
    elif any(str(argument).endswith("security_inventory.py") for argument in command):
        output = {{}}
    else:
        raise RuntimeError("Unexpected external check")
    return subprocess.CompletedProcess(command, 0, json.dumps(output).encode(), b"")

gate.execute = external_check
arguments = SimpleNamespace(mode="commit", ref=None, branch="hook-test", worktree=False, advisories_only=True)
raise SystemExit(gate.collect(arguments))
'''
    (root / "scripts/security_check.py").write_text(driver)
    (root / "example.txt").write_text("Checked original content")
    (root / "requirements.txt").write_text("")
    assert git(root, "add", "scripts", "example.txt", "requirements.txt").returncode == 0
    assert install_hooks(root, sys.executable) == 0

    result = git(root, "-c", "user.name=Security fixture", "-c", "user.email=security@example.invalid",
                 "commit", "-m", "Synthetic checked snapshot")
    summaries = list(reports.glob("run-*/summary.json"))
    assert len(summaries) == 1, result.stderr
    summary = json.loads(summaries[0].read_text())
    checked_tree = (reports / "checked-tree.txt").read_text()
    assert summary["identity"] == checked_tree
    if restage_during_check:
        assert git(root, "write-tree").stdout.strip() != checked_tree
        assert result.returncode != 0, "Commit accepted content absent from its checked snapshot"
        assert summary["passed"] is False
        assert git(root, "rev-parse", "--verify", "HEAD").returncode != 0
    else:
        assert result.returncode == 0, result.stderr
        assert summary["passed"] is True
        assert git(root, "show", "HEAD:example.txt").stdout == "Checked original content"
