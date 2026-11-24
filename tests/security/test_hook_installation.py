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
