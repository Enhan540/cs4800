"""Authenticated DAST against disposable synthetic data only."""

import argparse
from contextlib import closing
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlencode

import requests
from werkzeug.serving import make_server, WSGIRequestHandler

from security_check import IMAGES, finding

MONGO_IMAGE = "mongo@sha256:bdc216e9d5a6ec2cfdac789b5832fdab64cd32055f59885cae918a92de63c361"


def docker(*arguments):
    result = subprocess.run(["docker", *arguments], capture_output=True, timeout=120, check=False)
    if result.returncode:
        raise RuntimeError("Disposable scanner infrastructure unavailable")
    return result.stdout


def free_port():
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(function, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = function()
            if value:
                return value
        except (requests.RequestException, OSError):
            pass
        time.sleep(1)
    raise RuntimeError("Disposable scan timed out")


class QuietHandler(WSGIRequestHandler):
    def log(self, _type, _message, *args):
        pass


def verify_optional_mfa_http(local, db, ids, password):
    """Exercise enrollment and competing sign-ins with a disposable real DB."""
    import pyotp
    from concurrent.futures import ThreadPoolExecutor

    def csrf(actor, path="/login"):
        page = actor.get(local + path, timeout=10)
        return re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]

    def first_factor(actor):
        response = actor.post(local + "/login", data={"username": "audit-outsider", "password": password, "csrf_token": csrf(actor)}, timeout=10, allow_redirects=False)
        if not response.headers.get("Location", "").endswith("/auth/mfa") or actor.get(local + "/account", timeout=10, allow_redirects=False).status_code != 302:
            raise RuntimeError("MFA first factor granted account access")
        return csrf(actor, "/auth/mfa")

    current = requests.Session()
    current.post(local + "/login", data={"username": "audit-outsider", "password": password, "csrf_token": csrf(current)}, timeout=10)
    proof = csrf(current, "/account/mfa")
    page = current.post(local + "/account/mfa/setup", data={"current_password": password, "csrf_token": proof}, timeout=10)
    seed = re.search(r'data-sensitive-key>([A-Z2-7]{32})</code>', page.text)[1]
    page = current.post(local + "/account/mfa/enable", data={"mfa_code": pyotp.TOTP(seed).now(), "csrf_token": proof}, timeout=10)
    codes = re.findall(r'data-mfa-recovery>([A-F0-9-]+)</code>', page.text)
    if len(codes) != 10:
        raise RuntimeError("Real-database MFA enrollment failed")
    original = requests.Session()
    proof = first_factor(original)
    actors = [requests.Session(), requests.Session()]
    for actor in actors:
        actor.cookies.update(original.cookies)
    def complete(actor):
        actor.post(local + "/auth/mfa", data={"mfa_code": codes[0], "csrf_token": proof}, timeout=15, allow_redirects=False)
        return actor.get(local + "/account", timeout=10, allow_redirects=False).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        if sorted(pool.map(complete, actors)) != [200, 302]:
            raise RuntimeError("Concurrent MFA challenge consumption failed")
    replay = requests.Session()
    proof = first_factor(replay)
    response = replay.post(local + "/auth/mfa", data={"mfa_code": codes[0], "csrf_token": proof}, timeout=10, allow_redirects=False)
    factor = db.accounts.find_one({"_id": ids["outsider"]})["mfa"]
    if response.status_code != 401 or len(factor["recovery_code_hashes"]) != 9 or seed in json.dumps(factor):
        raise RuntimeError("Real-database MFA replay or storage check failed")
    print("Real MongoDB optional MFA and recovery-code isolation verified", flush=True)


def verify_group_capabilities_http(local, db, ids, password):
    """Check every named Admin capability and revocation against real MongoDB."""
    from datetime import datetime
    from bson import ObjectId
    from app import utc_now

    def sign_in(name, secret):
        actor = requests.Session()
        page = actor.get(local + "/login", timeout=10)
        proof = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
        actor.post(local + "/login", data={"username": "audit-" + name, "password": secret, "csrf_token": proof}, timeout=10)
        page = actor.get(local + "/account", timeout=10)
        if page.status_code != 200 or "audit-" + name not in page.text:
            raise RuntimeError("Permission-check authentication failed")
        actor.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
        return actor

    owner = sign_in("owner", password + "new")
    admin = sign_in("admin", password)

    def post(actor, path, data=None):
        return actor.post(local + path, data=data or {}, allow_redirects=False, timeout=15)

    def grant(permission):
        response = post(owner, f"/groups/{ids['group']}/members/{ids['admin']}/role", {"role": "admin", "permissions": [permission]})
        group = db.groups.find_one({"_id": ids["group"]})
        if response.status_code != 302 or group["admin_permissions"][str(ids["admin"])] != [permission]:
            raise RuntimeError("Real-database capability grant failed")

    window = f"/availability/{ids['window']}/edit"
    group_edit = f"/groups/{ids['group']}/edit"
    plan_edit = f"/plans/{ids['plan']}/edit"
    availability = {"day": "Monday", "start_time": "10:00", "end_time": "11:00"}
    grant("edit_member_availability")
    if post(admin, window, availability).status_code != 302 or db.availability.find_one({"_id": ids["window"]})["start_time"] != "10:00":
        raise RuntimeError("Real-database availability capability failed")
    if post(admin, group_edit, {"name": "UNAUTHORIZED"}).status_code != 403:
        raise RuntimeError("An unrelated Admin capability granted group editing")

    grant("manage_members")
    result = admin.get(local + f"/groups/{ids['group']}/invitations/search", params={"q": "audit"}, timeout=10)
    if result.status_code != 200 or not result.json().get("results"):
        raise RuntimeError("Real-database member-management capability failed")
    before = db.availability.find_one({"_id": ids["window"]})
    if post(admin, window, {**availability, "start_time": "09:00"}).status_code != 403 or db.availability.find_one({"_id": ids["window"]}) != before:
        raise RuntimeError("Revoked availability capability remained usable")

    grant("manage_plans")
    values = {"title": "Role-verified plan", "start_date": "2027-01-04", "end_date": "2027-01-04", "duration_minutes": "30", "minimum_attendance": "1", "earliest_time": "09:00", "latest_time": "17:00"}
    if post(admin, plan_edit, values).status_code != 302 or db.plans.find_one({"_id": ids["plan"]})["title"] != values["title"]:
        raise RuntimeError("Real-database plan-management capability failed")

    grant("edit_member_rsvp")
    db.plans.update_one({"_id": ids["plan"]}, {"$set": {"status": "scheduled", "scheduled_start": datetime(2027, 1, 4, 9), "scheduled_end": datetime(2027, 1, 4, 10)}})
    response = post(admin, f"/plans/{ids['plan']}/rsvp", {"member_id": str(ids["member"]), "status": "not_attending"})
    plan = db.plans.find_one({"_id": ids["plan"]})
    if response.status_code != 302 or plan.get("rsvps", {}).get(str(ids["member"]), {}).get("status") != "not_attending":
        raise RuntimeError("Real-database delegated RSVP capability failed")
    if post(admin, plan_edit, values).status_code != 403:
        raise RuntimeError("Revoked plan-management capability remained usable")

    grant("edit_group")
    response = post(admin, group_edit, {"name": "Role-verified group", "owner_account_id": str(ids["outsider"])})
    group = db.groups.find_one({"_id": ids["group"]})
    if response.status_code != 302 or group["name"] != "Role-verified group" or group["owner_account_id"] != ids["owner"]:
        raise RuntimeError("Real-database group-edit capability or field isolation failed")
    if post(admin, f"/groups/{ids['group']}/delete").status_code != 403:
        raise RuntimeError("An Admin capability granted an Owner-only action")
    if post(owner, f"/plans/{ids['plan']}/invite", {"member_id": str(ids["admin"])}).status_code != 302 or ids["admin"] not in db.plans.find_one({"_id": ids["plan"]})["invited_member_ids"]:
        raise RuntimeError("Real-database plan invitation setup failed")
    if post(owner, f"/groups/{ids['group']}/members/{ids['admin']}/remove").status_code != 302:
        raise RuntimeError("Real-database member removal failed")
    if admin.get(local + f"/groups/{ids['group']}", allow_redirects=False, timeout=10).status_code != 404 or post(admin, window, availability).status_code != 404:
        raise RuntimeError("Removed membership retained group access")
    if db.availability.find_one({"_id": ids["window"]}) != before:
        raise RuntimeError("Denied requests changed another member's records")
    db.calendar_connections.delete_many({})
    canary = "SYNTHETIC-POST-REVOCATION-PLAN"
    match = {"date": "2027-01-04", "day": "Monday", "start_time": "09:00", "end_time": "09:30", "attendance_count": 1, "total_members": 2, "available_member_ids": [str(ids["owner"])], "is_exact": True, "available_duration_minutes": 30}
    db.plans.update_one({"_id": ids["plan"]}, {"$set": {"title": canary, "matches": [match]}})
    if post(owner, f"/plans/{ids['plan']}/schedule", {"match_index": "0"}).status_code != 302 or db.plans.find_one({"_id": ids["plan"]})["status"] != "scheduled":
        raise RuntimeError("Real-database rescheduling setup failed")
    if not db.notifications.find_one({"recipient_user_id": ids["owner"], "plan_id": ids["plan"], "metadata.plan_title": canary}):
        raise RuntimeError("Authorized plan notification was not created")
    if db.notifications.find_one({"recipient_user_id": ids["admin"], "plan_id": ids["plan"]}):
        raise RuntimeError("Removed membership received a new plan notification")
    notice_id = ObjectId()
    db.notifications.insert_one({"_id": notice_id, "recipient_user_id": ids["admin"], "group_id": ids["group"], "plan_id": ids["plan"], "type": "PLAN_SCHEDULED", "status": "unresolved", "read_at": None, "created_at": utc_now(), "metadata": {"plan_title": canary}})
    notices = admin.get(local + "/api/notifications", timeout=10)
    summary = admin.get(local + "/api/notifications/summary", timeout=10)
    if notices.status_code != 200 or canary in notices.text or summary.json().get("unread_count") != 0 or summary.json().get("latest_created_at") is not None:
        raise RuntimeError("Stored notification bypassed group revocation")
    prior = db.plans.find_one({"_id": ids["plan"]})
    if post(admin, f"/notifications/{notice_id}/rsvp", {"status": "attending"}).status_code != 404 or db.plans.find_one({"_id": ids["plan"]}) != prior:
        raise RuntimeError("Stored notification action bypassed group revocation")
    owner.close()
    admin.close()
    print("Real MongoDB Admin capabilities, revocation and notification isolation verified", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--findings-file", required=True)
    args = parser.parse_args()
    source = Path(args.source).resolve()
    sys.path.insert(0, str(source))
    # Never inherit real app credentials when this script is invoked directly.
    for key in ("MONGO_URI", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "RESEND_API_KEY", "VERCEL", "APP_ENV"):
        os.environ[key] = ""
    os.environ.update(SECRET_KEY=secrets.token_hex(32), EMAIL_DELIVERY_MODE="console", APP_BASE_URL="")
    mongo_name, zap_name = "gp-audit-mongo-" + uuid.uuid4().hex, "gp-audit-zap-" + uuid.uuid4().hex
    mongo_port, zap_port = free_port(), free_port()
    server, client = None, None
    findings = []
    try:
        docker("run", "--rm", "-d", "--name", mongo_name, "-p", f"127.0.0.1:{mongo_port}:27017", MONGO_IMAGE)
        from pymongo import MongoClient
        client = MongoClient(f"mongodb://127.0.0.1:{mongo_port}", serverSelectionTimeoutMS=500)
        deadline = time.monotonic() + 60
        while True:
            try:
                client.admin.command("ping")
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError("Disposable MongoDB did not start")
                time.sleep(1)
        fixtures = importlib.import_module("tests.security.conftest")
        db = client["security_" + uuid.uuid4().hex]
        app, db, ids, password = fixtures.seeded_app(db)
        # Feed fetching is not needed for DAST and must never call arbitrary third parties.
        module = importlib.import_module("app")
        def reject_feed(*_args, **_kwargs):
            raise ValueError("External feeds are disabled in the synthetic audit")
        module.fetch_calendar_url = reject_feed
        server = make_server("0.0.0.0", 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_port
        local = f"http://127.0.0.1:{port}"
        target = f"http://host.docker.internal:{port}"
        session = requests.Session()
        login_page = session.get(local + "/login", timeout=10)
        match = re.search(r'name="csrf-token" content="([^"]+)"', login_page.text)
        if not match:
            raise RuntimeError("No CSRF token in the authentication page")
        response = session.post(local + "/login", data={"username": "audit-owner", "password": password, "csrf_token": match[1]}, timeout=10)
        account = session.get(local + "/account", timeout=10)
        if account.status_code != 200 or "audit-owner" not in account.text:
            raise RuntimeError("Synthetic authentication failed")
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', account.text)[1]
        cookie = "; ".join(f"{key}={value}" for key, value in session.cookies.get_dict().items())
        key = secrets.token_hex(24)
        host_mapping = [] if os.name == "nt" else ["--add-host=host.docker.internal:host-gateway"]
        docker("run", "--rm", "-d", "--name", zap_name, *host_mapping, IMAGES["zap"], "zap.sh", "-daemon", "-host", "0.0.0.0", "-port", "8080", "-config", f"api.key={key}", "-config", "api.addrs.addr.name=.*", "-config", "api.addrs.addr.regex=true")

        def api(component, kind, action, **params):
            url = f"http://127.0.0.1:8080/JSON/{component}/{kind}/{action}/?" + urlencode({"apikey": key, **params})
            result = subprocess.run(["docker", "exec", zap_name, "python3", "-c", "import urllib.request,sys; print(urllib.request.urlopen(sys.argv[1],timeout=20).read().decode())", url], capture_output=True, timeout=30, check=False)
            if result.returncode:
                raise OSError("Disposable ZAP API unavailable")
            value = json.loads(result.stdout)
            if "code" in value:
                raise RuntimeError(f"ZAP rejected {component}/{kind}/{action}: {value.get('code')}")
            return value

        print("Waiting for disposable ZAP API", flush=True)
        wait_until(lambda: api("core", "view", "version").get("version"))
        scope = rf"^http://host\.docker\.internal:{port}/.*"
        for description, header, value in (("synthetic-session", "Cookie", cookie), ("synthetic-csrf", "X-CSRF-Token", csrf)):
            api("replacer", "action", "addRule", description=description, enabled="true", matchType="REQ_HEADER", matchRegex="false", matchString=header, replacement=value, url=scope)
        context_id = api("context", "action", "newContext", contextName="synthetic-only")["contextId"]
        read_surfaces = rf"^http://host\.docker\.internal:{port}/(account|groups/{ids['group']}|users/{ids['owner']}/availability)(\?.*)?$"
        api("context", "action", "includeInContext", contextName="synthetic-only", regex=read_surfaces)
        exclusions = rf".*/(logout|auth/.*|account/(password.*|email.*|google/.*|calendars/.*)|.*delete.*|.*remove.*|.*disconnect.*|.*sync.*|.*invitations.*|forgot-password|reset-password/.*).*"
        api("context", "action", "excludeFromContext", contextName="synthetic-only", regex=exclusions)
        api("context", "action", "setContextInScope", contextName="synthetic-only", booleanInScope="true")
        seen = api("core", "action", "accessUrl", url=target + "/account", followRedirects="true")
        if "audit-owner" not in json.dumps(seen):
            raise RuntimeError("ZAP did not prove authenticated access")
        print("ZAP authenticated access verified", flush=True)
        spider = api("spider", "action", "scan", url=target + "/account", contextName="synthetic-only", subtreeOnly="true")["scan"]
        wait_until(lambda: api("spider", "view", "status", scanId=spider).get("status") == "100", timeout=120)
        # Scan a small authenticated surface; destructive handlers are outside context.
        policy = "group-planner-focused"
        api("ascan", "action", "addScanPolicy", scanPolicyName=policy)
        api("ascan", "action", "disableAllScanners", scanPolicyName=policy)
        scanners = api("ascan", "view", "scanners", scanPolicyName=policy)["scanners"]
        selected = [item["id"] for item in scanners if item["name"] == "Cross Site Scripting (Reflected)" or "nosql" in item["name"].casefold() or "server side template" in item["name"].casefold()]
        if not selected:
            raise RuntimeError("Required active scan rules unavailable")
        api("ascan", "action", "enableScanners", ids=",".join(selected), scanPolicyName=policy)
        scan = api("ascan", "action", "scan", url=target + "/account", recurse="false", inScopeOnly="true", contextId=context_id, scanPolicyName=policy)["scan"]
        print("Running scoped active scan", flush=True)
        wait_until(lambda: api("ascan", "view", "status", scanId=scan).get("status") == "100", timeout=600)
        wait_until(lambda: int(api("pscan", "view", "recordsToScan")["recordsToScan"]) == 0, timeout=90)
        final = api("core", "action", "accessUrl", url=target + "/account", followRedirects="true")
        if "audit-owner" not in json.dumps(final):
            raise RuntimeError("ZAP lost authenticated coverage")
        for alert in api("core", "view", "alerts", baseurl=target, count="10000")["alerts"]:
            rating = str(alert.get("riskcode", alert.get("risk", "UNKNOWN"))).upper()
            severity = {"0": "INFO", "1": "LOW", "2": "MEDIUM", "3": "HIGH", "INFORMATIONAL": "INFO", "LOW": "LOW", "MEDIUM": "MEDIUM", "HIGH": "HIGH"}.get(rating, "HIGH")
            findings.append(finding("zap", alert["pluginId"], severity, alert.get("url", "").replace(target, "")))
        outsider = requests.Session()
        page = outsider.get(local + "/login", timeout=10)
        proof = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
        outsider.post(local + "/login", data={"username": "audit-outsider", "password": password, "csrf_token": proof}, timeout=10)
        account_page = outsider.get(local + "/account", timeout=10)
        if account_page.status_code != 200 or "audit-outsider" not in account_page.text:
            raise RuntimeError("Second-account authentication failed")
        proof = re.search(r'name="csrf-token" content="([^"]+)"', account_page.text)[1]
        before = db.availability.find_one({"_id": ids["window"]})
        for path in (f"/groups/{ids['group']}", f"/plans/{ids['plan']}", f"/users/{ids['owner']}/availability"):
            denied = outsider.get(local + path, allow_redirects=False, timeout=10)
            if denied.status_code != 404 or "Private audit" in denied.text:
                raise RuntimeError("Real-database ownership check failed")
        denied = outsider.post(local + f"/availability/{ids['window']}/delete", data={"csrf_token": proof}, allow_redirects=False, timeout=10)
        if denied.status_code != 404 or db.availability.find_one({"_id": ids["window"]}) != before:
            raise RuntimeError("Real-database write isolation failed")
        replay = requests.Session()
        replay.cookies.update(outsider.cookies)
        outsider.post(local + "/logout", data={"csrf_token": proof}, allow_redirects=False, timeout=10)
        if replay.get(local + "/account", allow_redirects=False, timeout=10).status_code != 302:
            raise RuntimeError("Real-database logout revocation failed")
        print("Real MongoDB account isolation verified", flush=True)
        # Real database atomicity and expiry are checked in this disposable database.
        from concurrent.futures import ThreadPoolExecutor
        from datetime import timedelta
        from app import utc_now
        db.account_tokens.insert_one({"token_hash": "synthetic-atomicity", "expires_at": utc_now() + timedelta(minutes=1)})
        def take(_):
            return db.account_tokens.find_one_and_delete({"token_hash": "synthetic-atomicity", "expires_at": {"$gt": utc_now()}})
        with ThreadPoolExecutor(max_workers=8) as pool:
            if sum(value is not None for value in pool.map(take, range(16))) != 1:
                raise RuntimeError("Concurrent token consumption failed")
        # Exercise the actual recovery handler concurrently against real MongoDB.
        import hashlib
        raw = secrets.token_urlsafe(32)
        version_before = db.accounts.find_one({"_id": ids["owner"]}).get("auth_version", 0)
        db.account_tokens.insert_one({"user_id": ids["owner"], "purpose": "password_reset", "email": "owner@example.invalid", "auth_version": version_before, "token_hash": hashlib.sha256(raw.encode()).hexdigest(), "expires_at": utc_now() + timedelta(minutes=5)})
        def reset(_):
            actor = requests.Session()
            page = actor.get(local + "/login", timeout=10)
            proof = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
            return actor.post(local + "/reset-password/" + raw, data={"password": password + "new", "confirm_password": password + "new", "csrf_token": proof}, allow_redirects=False, timeout=20).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            if not all(code == 302 for code in pool.map(reset, range(2))):
                raise RuntimeError("Concurrent password reset failed")
        if db.accounts.find_one({"_id": ids["owner"]}).get("auth_version", 0) != version_before + 1:
            raise RuntimeError("Password reset was not single-use")
        if session.get(local + "/account", allow_redirects=False, timeout=10).status_code != 302:
            raise RuntimeError("Password reset did not revoke old sessions")
        verify_group_capabilities_http(local, db, ids, password)
        verify_optional_mfa_http(local, db, ids, password)
        Path(args.findings_file).write_text(json.dumps(findings, indent=2), encoding="utf-8")
        print("Authenticated ZAP scan and disposable MongoDB checks completed.")
        return 1 if any(item["severity"] in {"HIGH", "CRITICAL"} for item in findings) else 0
    except Exception as exc:
        Path(args.findings_file).write_text(json.dumps(findings), encoding="utf-8")
        print("Dynamic security checks incomplete; no clean result recorded.")
        if isinstance(exc, RuntimeError):
            print(str(exc))
        else:
            print("Failure category: " + type(exc).__name__)
        return 1
    finally:
        if server:
            server.shutdown()
        if client:
            client.close()
        for name in (zap_name, mongo_name):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
