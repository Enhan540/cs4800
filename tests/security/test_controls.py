import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from werkzeug.security import check_password_hash

from app import utc_now
from tests.security.conftest import login, token

pytestmark = pytest.mark.security


def test_csrf_is_required_for_login_and_authenticated_mutations(secured):
    app, db, ids, password = secured
    client = app.test_client()
    assert client.post("/login", data={"username": "audit-owner", "password": password}).status_code == 400
    csrf = login(client, "owner", password)
    path = f"/availability/{ids['window']}/delete"
    assert client.post(path).status_code == 400
    assert client.post(path, headers={"X-CSRF-Token": csrf, "Origin": "https://attacker.invalid"}).status_code == 400
    assert db.availability.find_one({"_id": ids["window"]})
    assert client.post(path, headers={"X-CSRF-Token": csrf}).status_code == 302
    assert not db.availability.find_one({"_id": ids["window"]})


@pytest.mark.parametrize("length,valid", [(2, False), (3, True), (40, True), (41, False), (10000, False)])
def test_username_boundaries(secured, length, valid):
    app, db, ids, password = secured
    client = app.test_client()
    response = client.post("/register", data={"username": "x" * length, "password": password, "confirm_password": password, "csrf_token": token(client)})
    assert bool(db.accounts.find_one({"normalized_username": "x" * length})) is valid
    assert response.status_code < 500


@pytest.mark.parametrize("username", ["ab\x00cd", "ééé", "a\r\nb", '{"$ne":null}', "<img src=x onerror=alert(1)>"])
def test_malformed_identity_is_not_registered(secured, username):
    app, db, ids, password = secured
    c = app.test_client()
    c.post("/register", data={"username": username, "password": password, "confirm_password": password, "csrf_token": token(c)})
    assert db.accounts.count_documents({}) == 4


def test_request_size_password_size_and_batch_shape(secured):
    app, db, ids, password = secured
    c = app.test_client()
    csrf = login(c, "owner", password)
    headers = {"X-CSRF-Token": csrf}
    assert c.post("/account/password", data={"new_password": "x" * 1025}, headers=headers).status_code == 400
    assert c.post("/account/email", data="x" * (513 * 1024), content_type="application/x-www-form-urlencoded", headers=headers).status_code == 413
    for payload in ([1], {"ids": "bad"}, {"ids": [1]}, {"ids": ["x"] * 101}):
        assert c.post("/api/notifications/delete", json=payload, headers=headers).status_code == 400
    assert c.get(f"/groups/{ids['group']}/invitations/search?q=" + "x" * 41).status_code == 400


def test_login_rate_limit_is_database_backed(secured):
    app, db, ids, password = secured
    app.config["SECURITY_TEST_RATE_LIMITS"] = True
    c = app.test_client()
    csrf = token(c)
    codes = [c.post("/login", data={"username": "audit-owner", "password": "wrong", "csrf_token": csrf}).status_code for _ in range(21)]
    assert codes[-1] == 429
    assert db.security_rate_limits.count_documents({}) > 0


def test_password_change_revokes_other_sessions(secured):
    app, db, ids, password = secured
    first, stolen = app.test_client(), app.test_client()
    csrf = login(first, "owner", password)
    login(stolen, "owner", password)
    response = first.post("/account/password", data={"current_password": password, "new_password": password + "new", "confirm_password": password + "new"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 302
    assert first.get("/account").status_code == 200
    assert stolen.get("/account").status_code == 302


def test_reset_token_single_use_and_session_revocation(secured):
    app, db, ids, password = secured
    c, old_session = app.test_client(), app.test_client()
    login(old_session, "owner", password)
    raw = "synthetic-reset-proof"
    db.account_tokens.insert_one({"user_id": ids["owner"], "purpose": "password_reset", "email": "owner@example.invalid", "token_hash": hashlib.sha256(raw.encode()).hexdigest(), "expires_at": utc_now() + timedelta(hours=1)})
    csrf = token(c)
    data = {"password": password + "new", "confirm_password": password + "new", "csrf_token": csrf}
    assert c.get(f"/reset-password/{raw}").status_code == 200
    assert c.post(f"/reset-password/{raw}", data=data).status_code == 302
    saved = db.accounts.find_one({"_id": ids["owner"]})["password_hash"]
    c.post(f"/reset-password/{raw}", data={**data, "password": "different-password", "confirm_password": "different-password"})
    assert db.accounts.find_one({"_id": ids["owner"]})["password_hash"] == saved
    assert check_password_hash(saved, password + "new")
    assert old_session.get("/account").status_code == 302


def test_security_headers_and_no_private_cache(secured):
    app, *_ = secured
    response = app.test_client().get("/login")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Referrer-Policy"] == "no-referrer"
