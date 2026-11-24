"""Read-only production checks. Never log response bodies or cookies."""

import os
from urllib.parse import urlsplit

import requests


def main():
    base = os.environ.get("PRODUCTION_URL", "").rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        print("Production smoke checks incomplete: configure PRODUCTION_URL.")
        return 1
    try:
        for path in ("/privacy", "/login"):
            result = requests.get(base + path, timeout=20, allow_redirects=False)
            if result.status_code != 200 or "Strict-Transport-Security" not in result.headers:
                raise ValueError("Public route or transport check failed")
        result = requests.get(base + "/account", timeout=20, allow_redirects=False)
        if result.status_code not in (302, 401, 403):
            raise ValueError("Anonymous account access was not rejected")
    except (requests.RequestException, ValueError):
        print("Production smoke checks FAILED; inspect the release privately.")
        return 1
    print("Production smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
