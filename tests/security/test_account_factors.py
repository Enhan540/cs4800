"""Account policy checks exercise real login and CSRF enforcement."""

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from werkzeug.security import check_password_hash

from app import utc_now
from tests.security.conftest import login, token

pytestmark = pytest.mark.security


@pytest.mark.parametrize("operation", ["register", "change", "set", "reset"])
def test_common_password_is_denied_on_every_creation_path(secured, operation):
    app, db, ids, original_password = secured
    client = app.test_client()
    before = db.accounts.find_one({"_id": ids["owner"]})
    common = "PASSWORD123"
    data = {"password": common, "new_password": common, "confirm_password": common}
    if operation == "register":
        path = "/register"
        data["username"] = "new-person"
        csrf = token(client)
    elif operation == "reset":
        raw = "synthetic-common-password-reset"
        db.account_tokens.insert_one({"user_id": ids["owner"], "purpose": "password_reset", "email": "owner@example.invalid", "token_hash": hashlib.sha256(raw.encode()).hexdigest(), "expires_at": utc_now() + timedelta(hours=1)})
        path = f"/reset-password/{raw}"
        csrf = token(client)
    else:
        csrf = login(client, "owner", original_password)
        path = "/account/password" if operation == "change" else "/account/password/set"
        data["current_password"] = original_password
        if operation == "set":
            db.accounts.update_one({"_id": ids["owner"]}, {"$unset": {"password_hash": ""}, "$set": {"google_sub": "synthetic-policy-sub", "auth_methods": ["google"]}})
            before = db.accounts.find_one({"_id": ids["owner"]})
    response = client.post(path, data=data, headers={"X-CSRF-Token": csrf}, follow_redirects=True)
    assert b"too common" in response.data
    assert db.accounts.find_one({"normalized_username": "new-person"}) is None
    assert db.accounts.find_one({"_id": ids["owner"]}) == before
    if operation == "reset":
        assert db.account_tokens.count_documents({"purpose": "password_reset"}) == 1


def test_password_policy_checks_whole_value_and_keeps_existing_logins(secured):
    from services.password_policy import password_error

    assert password_error("password123")
    assert password_error("PASSWORD123")
    assert password_error("x" * 1025)
    assert password_error("x" * 7)
    assert password_error("prefix-password123-uncommon-suffix") is None
    assert password_error(" Unusual snowman ☃ passphrase ") is None
    app, db, ids, _ = secured
    from werkzeug.security import generate_password_hash
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"password_hash": generate_password_hash("password123")}})
    client = app.test_client()
    login(client, "owner", "password123")
    assert client.get("/account").status_code == 200


def test_bundled_password_list_has_required_coverage_and_pinned_provenance():
    from services import common_passwords

    root = Path(__file__).resolve().parents[2]
    metadata = json.loads((root / ".security/password-blocklist.json").read_text())
    assert len(common_passwords.COMMON_PASSWORD_DIGESTS) >= 3000
    assert metadata["entries"] == len(common_passwords.COMMON_PASSWORD_DIGESTS)
    assert metadata["minimum_length"] == 8
    assert metadata["upstream_commit"] in metadata["source_url"]
    canonical = Path(common_passwords.__file__).read_text(encoding="utf-8").encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == metadata["generated_sha256"]
    assert (root / metadata["license"]).is_file()


@pytest.mark.parametrize("flow", ["connect", "email_collision"])
def test_google_link_revokes_old_sessions_and_retains_proven_session(secured, flow):
    from tests.test_auth import configured_google_client, start_google_login

    app, db, ids, password = secured
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"email": "owner@example.invalid"}})
    configured_google_client(app, profile={"sub": "synthetic-link-sub", "email": "owner@example.invalid", "email_verified": True, "name": "Audit Owner"})
    acting, other = app.test_client(), app.test_client()
    login(other, "owner", password)
    if flow == "connect":
        login(acting, "owner", password)
        acting.get("/auth/google/connect")
        with acting.session_transaction() as state:
            oauth_state = state["google_oauth_state"]
    else:
        _, oauth_state = start_google_login(acting)
    acting.get(f"/auth/google/callback?state={oauth_state}")
    if flow == "email_collision":
        assert "google_sub" not in db.accounts.find_one({"_id": ids["owner"]})
        login(acting, "owner", password)
    saved = db.accounts.find_one({"_id": ids["owner"]})
    assert saved["google_sub"] == "synthetic-link-sub"
    assert saved["auth_version"] == 1
    assert db.accounts.count_documents({}) == 4
    assert acting.get("/account").status_code == 200
    assert other.get("/account").status_code == 302


def test_google_disconnect_revokes_other_sessions_but_keeps_acting_session(secured):
    app, db, ids, password = secured
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"google_sub": "synthetic-disconnect-sub", "google_email": "owner@example.invalid", "auth_methods": ["password", "google"]}})
    acting, other = app.test_client(), app.test_client()
    csrf = login(acting, "owner", password)
    login(other, "owner", password)
    assert acting.post("/account/google/disconnect", data={"current_password": password}, headers={"X-CSRF-Token": csrf}).status_code == 302
    assert acting.get("/account").status_code == 200
    assert other.get("/account").status_code == 302
    saved = db.accounts.find_one({"_id": ids["owner"]})
    assert "google_sub" not in saved
    assert saved["auth_version"] == 1
    with acting.session_transaction() as state:
        assert state["csrf_token"] != csrf
        new_csrf = state["csrf_token"]
    acting.post("/account/google/disconnect", data={"current_password": password}, headers={"X-CSRF-Token": new_csrf})
    assert db.accounts.find_one({"_id": ids["owner"]})["auth_version"] == 1


def test_setting_first_password_revokes_old_sessions_and_cannot_replace_it(secured):
    app, db, ids, password = secured
    acting, other = app.test_client(), app.test_client()
    csrf = login(acting, "owner", password)
    login(other, "owner", password)
    db.accounts.update_one({"_id": ids["owner"]}, {"$unset": {"password_hash": ""}, "$set": {"google_sub": "synthetic-first-password-sub", "auth_methods": ["google"]}})
    new_password = password + "☃ "
    response = acting.post("/account/password/set", data={"new_password": new_password, "confirm_password": new_password}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 302
    saved = db.accounts.find_one({"_id": ids["owner"]})
    assert check_password_hash(saved["password_hash"], new_password)
    assert acting.get("/account").status_code == 200
    assert other.get("/account").status_code == 302
    with acting.session_transaction() as state:
        csrf = state["csrf_token"]
    response = acting.post("/account/password/set", data={"new_password": password + "different", "confirm_password": password + "different"}, headers={"X-CSRF-Token": csrf}, follow_redirects=True)
    assert b"Password added successfully" not in response.data
    assert db.accounts.find_one({"_id": ids["owner"]}) == saved


@pytest.mark.parametrize("change", ["password", "email", "google_disconnect"])
def test_reset_link_is_invalid_after_account_security_changes(secured, change):
    app, db, ids, password = secured
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"email": "owner@example.invalid", "email_verified": True, "google_sub": "synthetic-reset-link-sub", "auth_methods": ["password", "google"]}})
    raw = "synthetic-account-version-reset"
    db.account_tokens.insert_one({"user_id": ids["owner"], "purpose": "password_reset", "email": "owner@example.invalid", "auth_version": 0, "token_hash": hashlib.sha256(raw.encode()).hexdigest(), "expires_at": utc_now() + timedelta(hours=1)})
    acting, recovery = app.test_client(), app.test_client()
    csrf = login(acting, "owner", password)
    if change == "password":
        path = "/account/password"
        data = {"current_password": password, "new_password": password + "changed", "confirm_password": password + "changed"}
    elif change == "email":
        path, data = "/account/email", {"email": "replacement@example.invalid", "current_password": password}
    else:
        path, data = "/account/google/disconnect", {"current_password": password}
    assert acting.post(path, data=data, headers={"X-CSRF-Token": csrf}).status_code == 302
    saved = db.accounts.find_one({"_id": ids["owner"]})
    assert recovery.get(f"/reset-password/{raw}").status_code == 302
    response = recovery.post(f"/reset-password/{raw}", data={"password": password + "replacement", "confirm_password": password + "replacement", "csrf_token": token(recovery)})
    assert response.status_code == 302
    assert db.accounts.find_one({"_id": ids["owner"]}) == saved


@pytest.mark.parametrize("invalid", ["expired", "wrong_purpose", "unverified_email", "other_email", "old_version"])
def test_reset_links_reject_invalid_account_or_token_state(secured, invalid):
    app, db, ids, password = secured
    raw = "synthetic-invalid-reset-state"
    record = {"user_id": ids["owner"], "purpose": "password_reset", "email": "owner@example.invalid", "auth_version": 0, "token_hash": hashlib.sha256(raw.encode()).hexdigest(), "expires_at": utc_now() + timedelta(hours=1)}
    if invalid == "expired":
        record["expires_at"] = utc_now() - timedelta(seconds=1)
    elif invalid == "wrong_purpose":
        record["purpose"] = "verify_email"
    elif invalid == "unverified_email":
        db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"email_verified": False}})
    elif invalid == "other_email":
        record["email"] = "different@example.invalid"
    else:
        db.accounts.update_one({"_id": ids["owner"]}, {"$inc": {"auth_version": 1}})
    db.account_tokens.insert_one(record)
    before = db.accounts.find_one({"_id": ids["owner"]})
    client = app.test_client()
    response = client.post(f"/reset-password/{raw}", data={"password": password + "new", "confirm_password": password + "new", "csrf_token": token(client)})
    assert response.status_code == 302
    assert db.accounts.find_one({"_id": ids["owner"]}) == before


def test_password_reset_cannot_overwrite_a_concurrent_security_change(secured, monkeypatch):
    from werkzeug.security import generate_password_hash

    app, db, ids, password = secured
    raw = "synthetic-concurrent-reset-state"
    db.account_tokens.insert_one({"user_id": ids["owner"], "purpose": "password_reset", "email": "owner@example.invalid", "auth_version": 0, "token_hash": hashlib.sha256(raw.encode()).hexdigest(), "expires_at": utc_now() + timedelta(hours=1)})
    replacement_hash = generate_password_hash(password + "concurrent")
    collection_type = type(db.accounts)
    original_update = collection_type.find_one_and_update
    raced = []

    def change_before_reset_write(collection, query, update, *args, **kwargs):
        if collection.name == "accounts" and "password_hash" in update.get("$set", {}) and not raced:
            raced.append(True)
            collection.update_one({"_id": ids["owner"]}, {"$set": {"password_hash": replacement_hash}, "$inc": {"auth_version": 1}})
        return original_update(collection, query, update, *args, **kwargs)

    monkeypatch.setattr(collection_type, "find_one_and_update", change_before_reset_write)
    client = app.test_client()
    client.post(f"/reset-password/{raw}", data={"password": password + "reset", "confirm_password": password + "reset", "csrf_token": token(client)})
    saved = db.accounts.find_one({"_id": ids["owner"]})
    assert raced
    assert saved["password_hash"] == replacement_hash
    assert saved["auth_version"] == 1


@pytest.mark.parametrize("path", ["/account/email", "/account/google/disconnect"])
@pytest.mark.parametrize("proof", ["", "wrong"])
def test_sensitive_settings_require_current_password(secured, path, proof):
    app, db, ids, password = secured
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"google_sub": "synthetic-proof-sub"}})
    client = app.test_client()
    csrf = login(client, "owner", password)
    before = db.accounts.find_one({"_id": ids["owner"]})
    response = client.post(path, data={"email": "replacement@example.invalid", "current_password": proof}, headers={"X-CSRF-Token": csrf}, follow_redirects=True)
    assert b"Confirm your current password" in response.data
    assert db.accounts.find_one({"_id": ids["owner"]}) == before


@pytest.mark.parametrize("path", ["/account/email", "/account/password/set"])
@pytest.mark.parametrize("proof", [None, "invalid", "expired", 0, 999999999999])
def test_google_only_settings_reject_stale_or_invalid_freshness(secured, path, proof):
    app, db, ids, password = secured
    client = app.test_client()
    csrf = login(client, "owner", password)
    db.accounts.update_one({"_id": ids["owner"]}, {"$unset": {"password_hash": ""}, "$set": {"google_sub": "synthetic-freshness-sub", "auth_methods": ["google"]}})
    with client.session_transaction() as state:
        import time
        state["reauthenticated_at"] = time.time() - 301 if proof == "expired" else proof
    before = db.accounts.find_one({"_id": ids["owner"]})
    response = client.post(path, data={"email": "replacement@example.invalid", "new_password": password + "new", "confirm_password": password + "new"}, headers={"X-CSRF-Token": csrf}, follow_redirects=True)
    assert b"sign in with Google again" in response.data
    assert db.accounts.find_one({"_id": ids["owner"]}) == before


def test_email_recovery_cannot_create_a_password_for_google_only_accounts(secured, monkeypatch):
    app, db, ids, password = secured
    db.accounts.update_one({"_id": ids["owner"]}, {"$unset": {"password_hash": ""}, "$set": {"google_sub": "synthetic-recovery-sub", "auth_methods": ["google"]}})
    def unexpected_email(*args, **kwargs):
        pytest.fail("Google-only accounts must add a password after provider sign-in")
    monkeypatch.setattr("app.send_password_reset_email", unexpected_email)
    client = app.test_client()
    assert client.post("/forgot-password", data={"email": "owner@example.invalid", "csrf_token": token(client)}).status_code == 302
    assert db.account_tokens.count_documents({}) == 0
    raw = "synthetic-google-only-reset"
    db.account_tokens.insert_one({"user_id": ids["owner"], "purpose": "password_reset", "email": "owner@example.invalid", "token_hash": hashlib.sha256(raw.encode()).hexdigest(), "expires_at": utc_now() + timedelta(hours=1)})
    before = db.accounts.find_one({"_id": ids["owner"]})
    client.post(f"/reset-password/{raw}", data={"password": password + "new", "confirm_password": password + "new", "csrf_token": token(client)})
    assert db.accounts.find_one({"_id": ids["owner"]}) == before


def test_recent_google_sign_in_can_confirm_an_email_change(secured, monkeypatch):
    from tests.test_auth import configured_google_client, start_google_login

    app, db, ids, password = secured
    db.accounts.update_one({"_id": ids["owner"]}, {"$unset": {"password_hash": ""}, "$set": {"google_sub": "synthetic-email-proof", "auth_methods": ["google"]}})
    configured_google_client(app, profile={"sub": "synthetic-email-proof", "email": "owner@example.invalid", "email_verified": True})
    monkeypatch.setattr("app.send_verification_email", lambda *args, **kwargs: True)
    client = app.test_client()
    _, state = start_google_login(client)
    client.get(f"/auth/google/callback?state={state}")
    with client.session_transaction() as session_state:
        csrf = session_state["csrf_token"]
    assert client.post("/account/email", data={"email": "replacement@example.invalid"}, headers={"X-CSRF-Token": csrf}).status_code == 302
    assert db.accounts.find_one({"_id": ids["owner"]})["email"] == "replacement@example.invalid"
    assert client.get("/account").status_code == 200


@pytest.mark.parametrize("first_path,first_values,second_path,second_values", [
    ("/account/username", {"username": "changed-audit-owner"},
     "/account/email", {"email": "later@example.invalid"}),
    ("/account/email", {"email": "first@example.invalid"},
     "/account/username", {"username": "later-audit-owner"}),
])
def test_settings_changes_do_not_extend_google_primary_proof(
    secured, monkeypatch, first_path, first_values, second_path, second_values
):
    import time
    from tests.test_auth import configured_google_client, start_google_login

    app, db, ids, _password = secured
    clock = [time.time()]
    monkeypatch.setattr("app.time.time", lambda: clock[0])
    db.accounts.update_one({"_id": ids["owner"]}, {
        "$unset": {"password_hash": ""},
        "$set": {"google_sub": "synthetic-freshness-boundary", "auth_methods": ["google"]},
    })
    configured_google_client(app, profile={
        "sub": "synthetic-freshness-boundary", "email": "owner@example.invalid", "email_verified": True,
    })
    monkeypatch.setattr("app.send_verification_email", lambda *args, **kwargs: True)
    client = app.test_client()
    _, state = start_google_login(client)
    assert client.get(f"/auth/google/callback?state={state}").status_code == 302

    clock[0] += 250
    response = client.post(first_path, data=first_values, headers={"X-CSRF-Token": token(client)})
    assert response.status_code == 302
    saved = db.accounts.find_one({"_id": ids["owner"]})
    field = "username" if first_path.endswith("username") else "email"
    assert saved[field] == first_values[field]

    clock[0] += 61
    before = db.accounts.find_one({"_id": ids["owner"]})
    response = client.post(second_path, data=second_values,
                           headers={"X-CSRF-Token": token(client)}, follow_redirects=True)
    assert b"sign in with Google again" in response.data
    assert db.accounts.find_one({"_id": ids["owner"]}) == before
    assert client.get("/account").status_code == 200


def test_contextual_passwords_are_rejected_without_changing_login_verification(secured):
    from services.password_policy import password_error

    assert password_error("MY-USERNAME", username="my-username")
    assert password_error("member@example.invalid", email="member@example.invalid")
    assert password_error("GroupPlanner")
    assert password_error("prefix-my-username-unique-suffix", username="my-username") is None
    assert password_error("Uncommon passphrase without metadata", username=None, email=None) is None
    app, db, ids, password = secured
    client = app.test_client()
    value = "synthetic-context-account"
    response = client.post("/register", data={"username": value, "password": value, "confirm_password": value, "csrf_token": token(client)})
    assert response.status_code == 400
    assert db.accounts.find_one({"normalized_username": value}) is None


def test_legacy_account_with_null_email_can_change_its_password(secured):
    app, db, ids, password = secured
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"email": None}})
    client = app.test_client()
    csrf = login(client, "owner", password)
    replacement = password + "new"
    client.post("/account/password", data={"current_password": password, "new_password": replacement, "confirm_password": replacement}, headers={"X-CSRF-Token": csrf})
    assert check_password_hash(db.accounts.find_one({"_id": ids["owner"]})["password_hash"], replacement)
    assert client.get("/account").status_code == 200


def test_email_change_cannot_restore_a_concurrently_revoked_session(secured, monkeypatch):
    app, db, ids, password = secured
    client = app.test_client()
    csrf = login(client, "owner", password)
    collection_type = type(db.accounts)
    original = collection_type.find_one_and_update
    raced = []
    def revoke_before_change(collection, query, update, *args, **kwargs):
        if collection.name == "accounts" and "email" in update.get("$set", {}) and not raced:
            raced.append(True)
            collection.update_one({"_id": ids["owner"]}, {"$inc": {"auth_version": 1}})
        return original(collection, query, update, *args, **kwargs)
    monkeypatch.setattr(collection_type, "find_one_and_update", revoke_before_change)
    client.post("/account/email", data={"email": "replacement@example.invalid", "current_password": password}, headers={"X-CSRF-Token": csrf})
    assert raced
    assert db.accounts.find_one({"_id": ids["owner"]})["email"] == "owner@example.invalid"
    assert client.get("/account").status_code == 302
