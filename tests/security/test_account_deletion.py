"""Deleting an account needs fresh proof and removes only that account's data."""

import copy
import time

import pytest
from bson import ObjectId

from tests.security.conftest import login, token
from tests.security.test_authorization import snapshot
from tests.security.test_mfa_flows import enroll, fixed_otp_time  # noqa: F401  (fixture)

pytestmark = pytest.mark.security


def signed_in(app, db, name, password):
    client = app.test_client()
    return client, login(client, name, password)


def delete(client, proof, password=None, username="audit-member", confirm="yes", **extra):
    data = {"confirm_username": username, "confirm_permanent": confirm, **extra}
    if password is not None:
        data["current_password"] = password
    return client.post("/account/delete", data=data, headers={"X-CSRF-Token": proof})


def test_member_deletion_removes_the_account_and_its_place_everywhere(secured):
    app, db, ids, password = secured
    member = ids["member"]
    db.calendar_connections.insert_one({"account_id": member, "provider": "ical", "source_key": "member", "busy_intervals": []})
    db.availability.insert_one({"user_id": member, "day": "Monday", "day_order": 0, "start_time": "09:00", "end_time": "10:00"})
    db.plans.update_one({"_id": ids["plan"]}, {"$set": {f"rsvps.{member}": {"status": "attending"},
                                                         "scheduled_available_member_ids": [ids["owner"], member]}})
    db.notifications.insert_one({"recipient_user_id": member, "type": "PLAN_SCHEDULED", "status": "unresolved", "group_id": ids["group"]})
    db.account_tokens.insert_one({"user_id": member, "purpose": "verify", "token_hash": "synthetic"})
    db.security_sessions.insert_one({"_id": "other-device", "account_id": member})
    owner_before = (db.accounts.find_one({"_id": ids["owner"]}), db.users.find_one({"_id": ids["owner"]}),
                    db.availability.find_one({"_id": ids["window"]}), db.calendar_connections.find_one({"_id": ids["connection"]}))
    client, proof = signed_in(app, db, "member", password)
    response = delete(client, proof, password)
    assert response.status_code == 302 and response.headers["Location"].endswith("/")
    assert db.accounts.find_one({"_id": member}) is None and db.users.find_one({"_id": member}) is None
    group = db.groups.find_one({"_id": ids["group"]})
    assert member not in group["account_ids"] and member not in group["member_ids"]
    assert str(member) not in group.get("account_roles", {}) and str(member) not in group.get("admin_permissions", {})
    plan = db.plans.find_one({"_id": ids["plan"]})
    assert member not in plan["invited_member_ids"] and member not in plan["scheduled_available_member_ids"]
    assert str(member) not in plan.get("rsvps", {})
    for collection, field in (("calendar_connections", "account_id"), ("notifications", "recipient_user_id"),
                              ("account_tokens", "user_id"), ("security_sessions", "account_id"),
                              ("group_invites", "invited_user_id")):
        assert db[collection].count_documents({field: member}) == 0, collection
    assert db.availability.count_documents({"user_id": member}) == 0
    assert (db.accounts.find_one({"_id": ids["owner"]}), db.users.find_one({"_id": ids["owner"]}),
            db.availability.find_one({"_id": ids["window"]}), db.calendar_connections.find_one({"_id": ids["connection"]})) == owner_before
    # The old session no longer works and the credentials no longer sign in.
    assert client.get("/dashboard").status_code == 302
    again = app.test_client()
    assert again.post("/login", data={"username": "audit-member", "password": password,
                                      "csrf_token": token(again)}).status_code != 302


@pytest.mark.parametrize("change", [
    {"password": "wrong-password"},
    {"username": "audit-owner"},
    {"username": ""},
    {"confirm": ""},
])
def test_wrong_or_missing_confirmation_changes_nothing(secured, change):
    app, db, ids, password = secured
    client, proof = signed_in(app, db, "member", password)
    before = copy.deepcopy(snapshot(db))
    accounts_before = list(db.accounts.find({}))
    response = delete(client, proof, change.get("password", password), username=change.get("username", "audit-member"),
                      confirm=change.get("confirm", "yes"))
    assert response.status_code == 400
    assert snapshot(db) == before and list(db.accounts.find({})) == accounts_before
    assert client.get("/dashboard").status_code == 200


def test_deletion_requires_csrf(secured):
    app, db, ids, password = secured
    client, _proof = signed_in(app, db, "member", password)
    response = client.post("/account/delete", data={"confirm_username": "audit-member", "confirm_permanent": "yes",
                                                     "current_password": password})
    assert response.status_code == 400 and db.accounts.find_one({"_id": ids["member"]})


def test_owner_of_a_shared_group_must_delete_it_first(secured):
    app, db, ids, password = secured
    client, proof = signed_in(app, db, "owner", password)
    page = client.get("/account/delete").get_data(as_text=True)
    assert "You can't delete your account yet." in page and "Private audit group" in page and "Delete My Account" not in page
    before = copy.deepcopy(snapshot(db))
    response = delete(client, proof, password, username="audit-owner")
    assert response.status_code == 302 and response.headers["Location"].endswith("/account/delete")
    assert snapshot(db) == before and db.accounts.find_one({"_id": ids["owner"]})


def test_groups_owned_alone_are_deleted_with_their_plans_and_members_without_accounts(secured):
    app, db, ids, password = secured
    db.groups.update_one({"_id": ids["group"]}, {"$set": {"account_ids": [ids["owner"]], "member_ids": [ids["owner"]]}})
    managed = ObjectId()
    db.users.insert_one({"_id": managed, "display_name": "Grandma", "normalized_name": f"managed:{ids['group']}:grandma",
                         "account_id": ids["owner"], "is_managed": True, "managed_group_id": ids["group"]})
    db.availability.insert_one({"user_id": managed, "day": "Monday", "day_order": 0, "start_time": "09:00", "end_time": "10:00"})
    db.groups.update_one({"_id": ids["group"]}, {"$push": {"member_ids": managed}})
    client, proof = signed_in(app, db, "owner", password)
    client.post("/groups/demo", headers={"X-CSRF-Token": proof})
    assert db.groups.count_documents({"is_demo": True}) == 1
    others_before = [db.accounts.find_one({"_id": ids[name]}) for name in ("admin", "member", "outsider")]
    assert delete(client, proof, password, username="audit-owner").status_code == 302
    assert db.groups.count_documents({"owner_account_id": ids["owner"]}) == 0
    assert db.plans.count_documents({"group_id": ids["group"]}) == 0
    assert db.users.count_documents({"$or": [{"_id": managed}, {"is_demo": True}]}) == 0
    assert db.availability.count_documents({"user_id": managed}) == 0
    assert db.group_invites.count_documents({"group_id": ids["group"]}) == 0
    assert [db.accounts.find_one({"_id": ids[name]}) for name in ("admin", "member", "outsider")] == others_before


def test_members_without_accounts_added_to_others_groups_stay(secured):
    app, db, ids, password = secured
    managed = ObjectId()
    db.users.insert_one({"_id": managed, "display_name": "Coach", "normalized_name": f"managed:{ids['group']}:coach",
                         "account_id": ids["member"], "created_by_account_id": ids["member"],
                         "is_managed": True, "managed_group_id": ids["group"]})
    db.groups.update_one({"_id": ids["group"]}, {"$push": {"member_ids": managed}})
    client, proof = signed_in(app, db, "member", password)
    assert delete(client, proof, password).status_code == 302
    assert db.users.find_one({"_id": managed}) and managed in db.groups.find_one({"_id": ids["group"]})["member_ids"]


def test_google_only_accounts_need_a_recent_google_sign_in(secured):
    app, db, ids, password = secured
    client, proof = signed_in(app, db, "member", password)
    db.accounts.update_one({"_id": ids["member"]}, {"$unset": {"password_hash": ""},
                                                     "$set": {"google_sub": "synthetic-sub", "auth_methods": ["google"]}})
    # Signing in counts as recent proof for 5 minutes; after that a new Google sign-in is needed.
    with client.session_transaction() as state:
        state["reauthenticated_at"] = time.time() - 600
    assert delete(client, proof).status_code == 400 and db.accounts.find_one({"_id": ids["member"]})
    with client.session_transaction() as state:
        state["reauthenticated_at"] = time.time()
    assert delete(client, proof).status_code == 302 and db.accounts.find_one({"_id": ids["member"]}) is None


def test_authenticator_code_is_required_and_consumed(secured, fixed_otp_time):
    app, db, ids, password = secured
    db.groups.update_one({"_id": ids["group"]}, {"$set": {"account_ids": [ids["owner"]], "member_ids": [ids["owner"]]}})
    client, _seed, codes = enroll(secured, fixed_otp_time)
    with client.session_transaction() as state:
        proof = state["csrf_token"]
    page = client.get("/account/delete").get_data(as_text=True)
    assert 'name="mfa_code"' in page
    response = delete(client, proof, password, username="audit-owner", mfa_code="000000")
    assert response.status_code == 302 and response.headers["Location"].endswith("/account/delete")
    assert db.accounts.find_one({"_id": ids["owner"]})
    assert delete(client, proof, password, username="audit-owner").status_code == 302
    assert db.accounts.find_one({"_id": ids["owner"]})
    assert delete(client, proof, password, username="audit-owner", mfa_code=codes[0]).status_code == 302
    assert db.accounts.find_one({"_id": ids["owner"]}) is None


def test_account_page_links_to_deletion(secured):
    app, db, ids, password = secured
    client, _proof = signed_in(app, db, "member", password)
    page = client.get("/account").get_data(as_text=True)
    assert 'href="/account/delete"' in page
