"""Verify that each route has an explicit access classification."""

import argparse
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# MFA endpoints expose only a browser-bound, expiring first-factor challenge.
# Feedback stays public so people can report problems when they can't sign in;
# it only accepts input (rate limited, CSRF protected) and never shows stored reports.
PUBLIC = {"index", "privacy", "register", "login", "google_auth", "google_callback", "verify_email", "forgot_password", "reset_password", "mfa_challenge", "mfa_cancel", "feedback", "feedback_thanks"}


def inventory(root=ROOT):
    result = []
    for node in ast.walk(ast.parse((root / "app.py").read_text(encoding="utf-8"))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        guards = [ast.unparse(value) for value in node.decorator_list]
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if ast.unparse(decorator.func.value) != "app" or decorator.func.attr not in {"route", "get", "post", "delete", "put", "patch"}:
                continue
            path = ast.literal_eval(decorator.args[0])
            keywords = {item.arg: item.value for item in decorator.keywords}
            methods = ast.literal_eval(keywords["methods"]) if "methods" in keywords else ["GET" if decorator.func.attr == "route" else decorator.func.attr.upper()]
            result.append({"endpoint": node.name, "path": path, "methods": sorted(methods), "access": "public" if node.name in PUBLIC else "authenticated", "login_guard": "login_required" in guards})
    return sorted(result, key=lambda row: row["endpoint"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    routes = inventory()
    path = ROOT / ".security" / "routes.json"
    if args.write:
        path.write_text(json.dumps(routes, indent=2) + "\n", encoding="utf-8")
    if args.check:
        declared = json.loads(path.read_text(encoding="utf-8"))
        if routes != declared or any(row["access"] == "authenticated" and not row["login_guard"] for row in routes):
            print("Route policy FAILED: review route changes and authentication guards.")
            return 1
        print(f"Route policy: {len(routes)} explicitly classified routes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
