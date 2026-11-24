import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pyotp
import pytest

from app import utc_now
from services import mfa_service as mfa
from tests.security.conftest import login, token

pytestmark = pytest.mark.security


@pytest.fixture
def fixed_otp_time(monkeypatch):
    value = int(time.time())
    monkeypatch.setattr(mfa, "otp_timestamp", lambda: value)
    return value


def csrf(client):
    with client.session_transaction() as state:
        return state["csrf_token"]


def enroll(secured, when):
    app, db, ids, password = secured
    client = app.test_client()
    proof = login(client, "owner", password)
    assert client.post("/account/mfa/setup", data={"current_password": password}, headers={"X-CSRF-Token": proof}).status_code == 302
    page = client.get("/account/mfa/setup")
    assert page.status_code == 200
    seed = re.search(rb'data-sensitive-key>([A-Z2-7]{32})</code>', page.data)[1].decode()
    qr = client.get("/account/mfa/setup/qr")
    assert qr.status_code == 200 and qr.mimetype == "image/svg+xml"
    assert seed.encode() not in qr.data
    assert "fonts.googleapis.com" not in page.text and page.headers["Cache-Control"] == "no-store"
    assert "/_vercel/insights" not in page.text
    code = pyotp.TOTP(seed).at(when)
    response = client.post("/account/mfa/enable", data={"mfa_code": code}, headers={"X-CSRF-Token": proof})
    assert response.status_code == 200
    codes = [value.decode() for value in re.findall(rb'data-mfa-recovery>([A-F0-9-]+)</code>', response.data)]
    assert len(codes) == 10
    return client, seed, codes


def first_factor(client, password):
    response = client.post("/login", data={"username": "audit-owner", "password": password, "csrf_token": token(client)})
    assert response.status_code == 302 and response.headers["Location"].endswith("/auth/mfa")
    return csrf(client)


def test_optional_enrollment_privacy_and_other_session_revocation(secured, fixed_otp_time):
    app, db, ids, password = secured
    other = app.test_client()
    login(other, "owner", password)
    current, seed, codes = enroll(secured, fixed_otp_time)
    assert current.get("/account").status_code == 200
    assert other.get("/account").status_code == 302
    stored = json.dumps(db.accounts.find_one({"_id": ids["owner"]}), default=str)
    assert seed not in stored and all(value not in stored for value in codes)
    with current.session_transaction() as state:
        assert seed not in str(dict(state)) and all(value not in str(dict(state)) for value in codes)
    page = current.get("/account/mfa")
    assert all(value.encode() not in page.data for value in codes)
    assert seed.encode() not in page.data
    assert current.get("/account/mfa/setup/qr").status_code == 404


def test_first_factor_never_grants_access_and_recovery_codes_are_one_use(secured, fixed_otp_time):
    app, db, ids, password = secured
    current, seed, codes = enroll(secured, fixed_otp_time)
    one, two = app.test_client(), app.test_client()
    proofs = [first_factor(actor, password) for actor in (one, two)]
    assert one.get("/account").status_code == 302
    assert one.get(f"/groups/{ids['group']}").status_code == 302
    with one.session_transaction() as state:
        assert "_user_id" not in state and "security_session_id" not in state
    assert one.post("/auth/mfa", data={"mfa_code": codes[0]}, headers={"X-CSRF-Token": proofs[0]}).status_code == 302
    assert one.get("/account").status_code == 200
    assert two.post("/auth/mfa", data={"mfa_code": codes[0]}, headers={"X-CSRF-Token": proofs[1]}).status_code == 401
    assert two.get("/account").status_code == 302


def test_totp_replay_is_denied_across_independent_challenges(secured, fixed_otp_time, monkeypatch):
    app, db, ids, password = secured
    current, seed, codes = enroll(secured, fixed_otp_time)
    one, two = app.test_client(), app.test_client()
    proofs = [first_factor(actor, password) for actor in (one, two)]
    # The enrollment code was consumed; the next permitted time step is fresh.
    following = (fixed_otp_time // 30 + 1) * 30
    monkeypatch.setattr(mfa, "otp_timestamp", lambda: following)
    code = pyotp.TOTP(seed).at(following)
    assert one.post("/auth/mfa", data={"mfa_code": code}, headers={"X-CSRF-Token": proofs[0]}).status_code == 302
    assert two.post("/auth/mfa", data={"mfa_code": code}, headers={"X-CSRF-Token": proofs[1]}).status_code == 401


@pytest.mark.parametrize("tamper", ["browser", "version", "expired", "factor"])
def test_challenge_is_bound_to_browser_and_current_account_state(secured, fixed_otp_time, tamper):
    app, db, ids, password = secured
    current, seed, codes = enroll(secured, fixed_otp_time)
    actor = app.test_client();proof = first_factor(actor, password)
    if tamper == "browser":
        with actor.session_transaction() as state:
            state["mfa_browser_nonce"] = "other-browser"
    elif tamper == "version":
        db.accounts.update_one({"_id": ids["owner"]}, {"$inc": {"auth_version": 1}})
    elif tamper == "expired":
        db.mfa_challenges.update_one({}, {"$set": {"expires_at": utc_now() - timedelta(seconds=1)}})
    else:
        db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"mfa.epoch": "replaced-factor"}})
    actor.post("/auth/mfa", data={"mfa_code": codes[0], "account_id": str(ids["outsider"])}, headers={"X-CSRF-Token": proof})
    assert actor.get("/account").status_code == 302
    assert len(db.accounts.find_one({"_id": ids["owner"]})["mfa"]["recovery_code_hashes"]) == 10


def test_setup_is_bound_to_the_account_session_and_requires_primary_proof(secured):
    app, db, ids, password = secured
    owner, other = app.test_client(), app.test_client()
    proof = login(owner, "owner", password)
    login(other, "outsider", password)
    owner.post("/account/mfa/setup", data={"current_password": "wrong"}, headers={"X-CSRF-Token": proof})
    assert db.mfa_setups.count_documents({}) == 0
    owner.post("/account/mfa/setup", data={"current_password": password}, headers={"X-CSRF-Token": proof})
    with owner.session_transaction() as state:
        identifier = state["mfa_setup_id"]
    with other.session_transaction() as state:
        state["mfa_setup_id"] = identifier
    assert other.get("/account/mfa/setup").status_code == 404
    assert other.get("/account/mfa/setup/qr").status_code == 404
    db.accounts.update_one({"_id": ids["owner"]}, {"$inc": {"auth_version": 1}})
    assert owner.get("/account/mfa/setup/qr").status_code == 302


def test_password_reset_preserves_enabled_mfa(secured, fixed_otp_time):
    app, db, ids, password = secured
    current, seed, codes = enroll(secured, fixed_otp_time)
    account = db.accounts.find_one({"_id": ids["owner"]})
    raw = "synthetic-mfa-reset-proof"
    db.account_tokens.insert_one({"user_id": ids["owner"], "purpose": "password_reset", "email": account["email"], "auth_version": account["auth_version"], "token_hash": hashlib.sha256(raw.encode()).hexdigest(), "expires_at": utc_now() + timedelta(hours=1)})
    actor = app.test_client();proof = token(actor)
    data = {"password": password + "new", "confirm_password": password + "new", "csrf_token": proof}
    actor.post(f"/reset-password/{raw}", data=data)
    assert db.account_tokens.count_documents({"purpose": "password_reset"}) == 1
    assert db.accounts.find_one({"_id": ids["owner"]})["password_hash"] == account["password_hash"]
    actor.post(f"/reset-password/{raw}", data={**data, "mfa_code": codes[0]})
    assert db.account_tokens.count_documents({"purpose": "password_reset"}) == 0
    assert current.get("/account").status_code == 302
    assert db.accounts.find_one({"_id": ids["owner"]})["mfa"]["enabled"] is True
    first_factor(app.test_client(), password + "new")


def test_google_login_and_collision_link_both_require_mfa(secured, fixed_otp_time):
    from tests.test_auth import configured_google_client, start_google_login
    app, db, ids, password = secured
    current, seed, codes = enroll(secured, fixed_otp_time)
    configured_google_client(app, profile={"sub": "synthetic-mfa-google", "email": "owner@example.invalid", "email_verified": True})
    actor = app.test_client();_, state = start_google_login(actor)
    actor.get(f"/auth/google/callback?state={state}")
    proof = first_factor(actor, password)
    assert "google_sub" not in db.accounts.find_one({"_id": ids["owner"]})
    actor.post("/auth/mfa", data={"mfa_code": codes[0]}, headers={"X-CSRF-Token": proof})
    assert db.accounts.find_one({"_id": ids["owner"]})["google_sub"] == "synthetic-mfa-google"
    second = app.test_client();_, state = start_google_login(second)
    response = second.get(f"/auth/google/callback?state={state}")
    assert response.headers["Location"].endswith("/auth/mfa")
    assert second.get("/account").status_code == 302
    second.post("/auth/mfa", data={"mfa_code": codes[1]}, headers={"X-CSRF-Token": csrf(second)})
    assert second.get("/account").status_code == 200


def test_disabling_and_regenerating_require_both_proofs(secured, fixed_otp_time):
    app, db, ids, password = secured
    current, seed, codes = enroll(secured, fixed_otp_time)
    proof = csrf(current)
    current.post("/account/mfa/disable", data={"current_password": "wrong", "mfa_code": codes[0]}, headers={"X-CSRF-Token": proof})
    assert len(db.accounts.find_one({"_id": ids["owner"]})["mfa"]["recovery_code_hashes"]) == 10
    current.post("/account/mfa/disable", data={"current_password": password}, headers={"X-CSRF-Token": proof})
    assert db.accounts.find_one({"_id": ids["owner"]})["mfa"]["enabled"]
    response = current.post("/account/mfa/recovery", data={"current_password": password, "mfa_code": codes[0]}, headers={"X-CSRF-Token": proof})
    new_codes = [v.decode() for v in re.findall(rb'data-mfa-recovery>([A-F0-9-]+)</code>', response.data)]
    assert len(new_codes) == 10 and not set(codes) & set(new_codes)
    updated = db.accounts.find_one({"_id": ids["owner"]})
    with pytest.raises(mfa.MFAError):mfa.proof(app, updated, codes[1])
    current.post("/account/mfa/disable", data={"current_password": password, "mfa_code": new_codes[0]}, headers={"X-CSRF-Token": csrf(current)})
    assert not mfa.enabled(db.accounts.find_one({"_id": ids["owner"]}))


def test_challenge_attempt_limit_cannot_be_reset_by_refreshing(secured, fixed_otp_time):
    app, db, ids, password = secured
    current, seed, codes = enroll(secured, fixed_otp_time)
    actor = app.test_client();proof = first_factor(actor, password)
    for _ in range(8):
        actor.post("/auth/mfa", data={"mfa_code": "invalid"}, headers={"X-CSRF-Token": proof})
    actor.post("/auth/mfa", data={"mfa_code": codes[0]}, headers={"X-CSRF-Token": proof})
    assert actor.get("/account").status_code == 302
    assert db.mfa_challenges.find_one({})["attempts"] == 8


def test_only_one_clone_can_complete_a_challenge(secured, fixed_otp_time):
    app, db, ids, password = secured
    current, seed, codes = enroll(secured, fixed_otp_time)
    original = app.test_client();proof = first_factor(original, password)
    name = app.config["SESSION_COOKIE_NAME"]
    cookie = original.get_cookie(name).value
    actors = [app.test_client(), app.test_client()]
    for actor in actors:actor.set_cookie(name, cookie)
    def complete(actor):
        actor.post("/auth/mfa", data={"mfa_code": codes[0]}, headers={"X-CSRF-Token": proof})
        return actor.get("/account").status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(complete, actors)) == [200, 302]


@pytest.mark.parametrize("path,fields", [
    ("/account/username", {"username": "replacement-owner"}),
    ("/account/email", {"email": "replacement@example.invalid"}),
    ("/account/password", {"new_password": "Synthetic uncommon replacement passphrase", "confirm_password": "Synthetic uncommon replacement passphrase"}),
    ("/account/google/disconnect", {}),
])
def test_sensitive_account_changes_require_fresh_one_use_factor(secured, fixed_otp_time, monkeypatch, path, fields):
    app, db, ids, password = secured
    current, seed, codes = enroll(secured, fixed_otp_time)
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"google_sub": "synthetic-sensitive-factor"}})
    monkeypatch.setattr("app.send_verification_email", lambda *args, **kwargs: True)
    original = db.accounts.find_one({"_id": ids["owner"]})
    proof = csrf(current)
    current.post(path, data={**fields, "current_password": password}, headers={"X-CSRF-Token": proof})
    assert db.accounts.find_one({"_id": ids["owner"]}) == original
    current.post(path, data={**fields, "current_password": "wrong", "mfa_code": codes[0]}, headers={"X-CSRF-Token": proof})
    assert db.accounts.find_one({"_id": ids["owner"]}) == original
    response = current.post(path, data={**fields, "current_password": password, "mfa_code": codes[0]}, headers={"X-CSRF-Token": proof})
    assert response.status_code == 302
    updated = db.accounts.find_one({"_id": ids["owner"]})
    assert updated["auth_version"] == original["auth_version"] + 1
    assert len(updated["mfa"]["recovery_code_hashes"]) == 9
    assert current.get("/account").status_code == 200


def test_restarting_first_factor_does_not_reset_account_attempt_quota(secured, fixed_otp_time):
    app, db, ids, password = secured
    current, seed, codes = enroll(secured, fixed_otp_time)
    db.security_rate_limits.delete_many({})
    # Freeze only the quota clock so the fixed 15-minute bucket cannot roll over.
    from unittest.mock import patch
    with patch("app.time.time", return_value=fixed_otp_time):
        statuses = []
        for _ in range(3):
            actor = app.test_client();proof = first_factor(actor, password)
            for _ in range(8):
                statuses.append(actor.post("/auth/mfa", data={"mfa_code": "invalid"}, headers={"X-CSRF-Token": proof}).status_code)
        assert statuses.count(401) == 20 and statuses.count(429) == 4


@pytest.mark.parametrize("path", ["/auth/mfa", "/auth/mfa/cancel", "/account/mfa/setup", "/account/mfa/enable", "/account/mfa/disable", "/account/mfa/recovery"])
def test_mfa_mutations_reject_missing_csrf(secured, path):
    app, db, ids, password = secured
    actor = app.test_client()
    login(actor, "owner", password)
    before = db.accounts.find_one({"_id": ids["owner"]})
    assert actor.post(path, data={"current_password": password}).status_code == 400
    assert db.accounts.find_one({"_id": ids["owner"]}) == before


def test_cancel_and_oversized_code_cannot_complete_a_challenge(secured, fixed_otp_time):
    app, db, ids, password = secured
    current, seed, codes = enroll(secured, fixed_otp_time)
    actor = app.test_client();proof = first_factor(actor, password)
    assert actor.post("/auth/mfa", data={"mfa_code": "1" * 81}, headers={"X-CSRF-Token": proof}).status_code == 400
    assert actor.post("/auth/mfa/cancel", headers={"X-CSRF-Token": proof}).status_code == 302
    assert db.mfa_challenges.count_documents({}) == 0
    actor.post("/auth/mfa", data={"mfa_code": codes[0]}, headers={"X-CSRF-Token": proof})
    assert actor.get("/account").status_code == 302


def test_google_only_account_enrollment_and_adding_password_keep_mfa(secured, fixed_otp_time):
    from tests.test_auth import configured_google_client, start_google_login
    app, db, ids, password = secured
    db.accounts.update_one({"_id": ids["owner"]}, {"$unset": {"password_hash": ""}, "$set": {"google_sub": "synthetic-google-only-factor", "auth_methods": ["google"]}})
    configured_google_client(app, profile={"sub": "synthetic-google-only-factor", "email": "owner@example.invalid", "email_verified": True})
    actor = app.test_client();_, state = start_google_login(actor)
    actor.get(f"/auth/google/callback?state={state}")
    actor.post("/account/mfa/setup", headers={"X-CSRF-Token": csrf(actor)})
    page = actor.get("/account/mfa/setup")
    seed = re.search(rb'data-sensitive-key>([A-Z2-7]{32})</code>', page.data)[1].decode()
    response = actor.post("/account/mfa/enable", data={"mfa_code": pyotp.TOTP(seed).at(fixed_otp_time)}, headers={"X-CSRF-Token": csrf(actor)})
    codes = [v.decode() for v in re.findall(rb'data-mfa-recovery>([A-F0-9-]+)</code>', response.data)]
    assert len(codes) == 10
    data = {"new_password": password, "confirm_password": password}
    actor.post("/account/password/set", data=data, headers={"X-CSRF-Token": csrf(actor)})
    assert not db.accounts.find_one({"_id": ids["owner"]}).get("password_hash")
    actor.post("/account/password/set", data={**data, "mfa_code": codes[0]}, headers={"X-CSRF-Token": csrf(actor)})
    assert db.accounts.find_one({"_id": ids["owner"]})["mfa"]["enabled"]
    first_factor(app.test_client(), password)
