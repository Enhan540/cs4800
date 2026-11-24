import copy
import os
from datetime import timedelta

import pytest
from werkzeug.security import generate_password_hash

from scripts.migrate_guest_accounts import migrate_guest_accounts
from tests.security.conftest import login, token
from app import utc_now

pytestmark = pytest.mark.security


@pytest.mark.parametrize("provider", ["missing", "common", "reused"])
def test_new_migration_validates_all_credentials_before_writing(secured, provider):
    app, db, ids, password = secured
    for name in ("guest1", "guest2"):
        db.users.insert_one({"display_name": name, "normalized_name": name})
    before = {name: copy.deepcopy(list(db[name].find({}))) for name in db.list_collection_names()}
    supplied = None if provider == "missing" else lambda name: "password123" if provider == "common" else password
    with pytest.raises(ValueError):
        migrate_guest_accounts(db, password_provider=supplied, output=lambda _: None)
    assert {name: list(db[name].find({})) for name in before} == before


def test_retired_bootstrap_cannot_authenticate_and_old_setup_sessions_expire(secured):
    app, db, ids, _ = secured
    # Synthetic historical record: the old bootstrap value must be rejected.
    retired = "guestpassword"
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"password_hash": generate_password_hash(retired), "migration_source": "legacy_guest", "must_change_password": True}})
    old, anonymous = app.test_client(), app.test_client()
    with old.session_transaction() as state:
        state.update(_user_id=str(ids["owner"]), _fresh=True, auth_version=0)
    assert old.get("/account").status_code == 302
    response = anonymous.post("/login", data={"username": "audit-owner", "password": retired, "csrf_token": token(anonymous)})
    assert response.status_code == 401
    assert anonymous.get(f"/groups/{ids['group']}").status_code == 302


def test_unique_temporary_password_requires_change_before_group_access(secured):
    app, db, ids, password = secured
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"migration_source": "legacy_guest", "must_change_password": True, "temporary_password_expires_at": utc_now() + timedelta(hours=24)}})
    client = app.test_client()
    csrf = login(client, "owner", password)
    response = client.get(f"/groups/{ids['group']}")
    assert response.status_code == 302 and response.headers["Location"].endswith("/account")
    assert b"Change your temporary password" in client.get("/account").data
    client.post("/account/password", data={"current_password": password, "new_password": password, "confirm_password": password}, headers={"X-CSRF-Token": csrf})
    assert db.accounts.find_one({"_id": ids["owner"]})["must_change_password"] is True
    replacement = os.urandom(24).hex()
    response = client.post("/account/password", data={"current_password": password, "new_password": replacement, "confirm_password": replacement}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 302
    assert client.get(f"/groups/{ids['group']}").status_code == 200
    assert db.accounts.find_one({"_id": ids["owner"]})["must_change_password"] is False
    assert db.users.find_one({"_id": ids["owner"]})["linked_account_id"] == ids["owner"]


@pytest.mark.parametrize("expiry", [None, "expired"])
def test_temporary_password_requires_a_current_expiry(secured, expiry):
    app, db, ids, password = secured
    value = None if expiry is None else utc_now() - timedelta(seconds=1)
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"migration_source": "legacy_guest", "must_change_password": True, "temporary_password_expires_at": value}})
    client = app.test_client()
    response = client.post("/login", data={"username": "audit-owner", "password": password, "csrf_token": token(client)})
    assert response.status_code == 401
    assert client.get("/account").status_code == 302
