"""Check every non-deleted ref in Git's pre-push input."""

from pathlib import Path
import re
import subprocess
import sys


def parse_updates(data):
    result = []
    for line in data.splitlines():
        local_ref, local_sha, _remote_ref, _remote_sha = line.split()
        if not re.fullmatch(r"[0-9a-f]{40,64}", local_sha):
            raise ValueError("Invalid pushed commit")
        if set(local_sha) != {"0"}:
            result.append((local_ref.removeprefix("refs/heads/"), local_sha))
    return result


def main():
    try:
        updates = parse_updates(sys.stdin.read())
    except ValueError:
        print("Security push check FAILED: invalid ref input.")
        return 1
    script = Path(__file__).with_name("security_check.py")
    for branch, commit in updates:
        result = subprocess.run([sys.executable, str(script), "push", "--ref", commit, "--branch", branch], check=False)
        if result.returncode:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
