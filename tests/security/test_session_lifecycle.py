import copy
import hashlib
from datetime import timedelta

import pytest

from app import utc_now
from tests.security.conftest import login

pytestmark = pytest.mark.security


def test_logout_revokes_copied_cookie_without_revoking_other_devices(secured):
    app, db, ids, password = secured
    original, copied, other = app.test_client(), app.test_client(), app.test_client()
    csrf = login(original, "owner", password)
    login(other, "owner", password)
    cookie_name = app.config["SESSION_COOKIE_NAME"]
    copied.set_cookie(cookie_name, original.get_cookie(cookie_name).value)
    assert copied.get("/account").status_code == 200
    assert original.post("/logout", headers={"X-CSRF-Token": csrf}).status_code == 302
    assert copied.get("/account").status_code == 302
    assert other.get("/account").status_code == 200


@pytest.mark.parametrize("field", ["last_seen_at", "expires_at"])
def test_server_expiry_blocks_a_still_signed_cookie(secured, field):
    app, db, ids, password = secured
    client = app.test_client()
    csrf = login(client, "owner", password)
    with client.session_transaction() as state:
        digest = hashlib.sha256(state["security_session_id"].encode()).hexdigest()
    value = utc_now() - timedelta(minutes=31 if field == "last_seen_at" else 1)
    db.security_sessions.update_one({"_id": digest}, {"$set": {field: value}})
    before = copy.deepcopy(db.accounts.find_one({"_id": ids["owner"]}))
    assert client.get("/account").status_code == 302
    client.post("/account/username", data={"username": "stale-session-rename"}, headers={"X-CSRF-Token": csrf})
    assert db.accounts.find_one({"_id": ids["owner"]}) == before


def test_background_polling_does_not_extend_idle_time_or_absolute_expiry(secured):
    app, db, ids, password = secured
    client = app.test_client()
    login(client, "owner", password)
    with client.session_transaction() as state:
        digest = hashlib.sha256(state["security_session_id"].encode()).hexdigest()
    seen = (utc_now() - timedelta(minutes=10)).replace(microsecond=0)
    db.security_sessions.update_one({"_id": digest}, {"$set": {"last_seen_at": seen}})
    before = db.security_sessions.find_one({"_id": digest})
    client.get("/api/notifications")
    after = db.security_sessions.find_one({"_id": digest})
    assert after["last_seen_at"] == before["last_seen_at"]
    client.get("/account")
    after = db.security_sessions.find_one({"_id": digest})
    assert after["last_seen_at"] > before["last_seen_at"]
    assert after["expires_at"] == before["expires_at"]
