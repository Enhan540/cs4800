import os

import mongomock
import pytest

os.environ["MONGO_URI"] = ""
from app import create_app


@pytest.fixture
def invitation_app_and_db():
    database = mongomock.MongoClient().invitation_test
    app = create_app(database_override=database)
    app.config.update(TESTING=True, SECRET_KEY="invitation-test", LOGIN_DISABLED=False)
    return app, database


def register(client, username):
    return client.post(
        "/register",
        data={"username": username, "password": "correct-horse", "confirm_password": "correct-horse"},
    )


def test_owner_invitation_is_pending_until_acceptance(invitation_app_and_db):
    app, database = invitation_app_and_db
    owner_client = app.test_client()
    invited_client = app.test_client()
    register(owner_client, "owner-one")
    owner_client.post("/groups/create", data={"name": "Friday Friends", "description": ""})
    group = database.groups.find_one({"name": "Friday Friends"})
    register(invited_client, "alex123")
    alex = database.accounts.find_one({"normalized_username": "alex123"})
    owner_client.post(
        f"/groups/{group['_id']}/invitations",
        data={"username": "alex123"},
    )
    invite = database.group_invites.find_one({"group_id": group["_id"]})
    assert invite["status"] == "pending"
    assert alex["_id"] not in database.groups.find_one({"_id": group["_id"]})["account_ids"]
    assert b"Group Invitations" in invited_client.get("/dashboard").data

    invited_client.post(f"/invitations/{invite['_id']}/accept")
    updated_group = database.groups.find_one({"_id": group["_id"]})
    assert alex["_id"] in updated_group["account_ids"]
    assert database.group_invites.find_one({"_id": invite["_id"]})["status"] == "accepted"


def test_invitation_rules_and_decline(invitation_app_and_db):
    app, database = invitation_app_and_db
    owner = app.test_client()
    member = app.test_client()
    register(owner, "owner-one")
    owner.post("/groups/create", data={"name": "Rules Group", "description": ""})
    group = database.groups.find_one({"name": "Rules Group"})
    register(member, "member-one")

    assert owner.post(f"/groups/{group['_id']}/invitations", data={"username": "member-one"}).status_code == 302
    duplicate = owner.post(f"/groups/{group['_id']}/invitations", data={"username": "member-one"})
    assert b"already been invited" in owner.get(f"/groups/{group['_id']}").data or duplicate.status_code == 302
    invite = database.group_invites.find_one({"group_id": group["_id"], "status": "pending"})
    member.post(f"/invitations/{invite['_id']}/accept")
    assert member.post(f"/groups/{group['_id']}/invitations", data={"username": "owner-one"}).status_code == 403

    target = app.test_client()
    register(target, "decline-target")
    owner.post(f"/groups/{group['_id']}/invitations", data={"username": "decline-target"})
    invite = database.group_invites.find_one({"group_id": group["_id"], "status": "pending"})
    unrelated = app.test_client()
    register(unrelated, "unrelated")
    assert unrelated.post(f"/invitations/{invite['_id']}/accept").status_code == 404
    assert target.post(f"/invitations/{invite['_id']}/decline").status_code == 302
    assert database.group_invites.find_one({"_id": invite["_id"]})["status"] == "declined"


def test_group_member_ui_uses_invite_and_per_member_settings(invitation_app_and_db):
    app, database = invitation_app_and_db
    client = app.test_client()
    register(client, "owner-one")
    client.post("/groups/create", data={"name": "UI Group", "description": ""})
    group = database.groups.find_one({"name": "UI Group"})
    page = client.get(f"/groups/{group['_id']}").get_data(as_text=True)
    assert "+ Invite" in page
    assert "Manage Members" not in page
    assert "Create New Member" not in page


def test_member_settings_hides_permissions_for_member_role(invitation_app_and_db):
    app, database = invitation_app_and_db
    owner = app.test_client()
    invited = app.test_client()
    register(owner, "owner-one")
    owner.post("/groups/create", data={"name": "Role UI Group", "description": ""})
    group = database.groups.find_one({"name": "Role UI Group"})
    register(invited, "member-one")
    owner.post(f"/groups/{group['_id']}/invitations", data={"username": "member-one"})
    invite = database.group_invites.find_one({"group_id": group["_id"]})
    invited.post(f"/invitations/{invite['_id']}/accept")

    member_page = owner.get(f"/groups/{group['_id']}").get_data(as_text=True)
    assert 'data-admin-permissions hidden' in member_page

    member_account = database.accounts.find_one({"normalized_username": "member-one"})
    member = database.users.find_one({"linked_account_id": member_account["_id"]})
    owner.post(
        f"/groups/{group['_id']}/members/{member['_id']}/role",
        data={"role": "admin", "permissions": "manage_members"},
    )
    admin_page = owner.get(f"/groups/{group['_id']}").get_data(as_text=True)
    assert 'data-admin-permissions hidden' not in admin_page


def test_plan_page_shows_only_authorized_user_conflicts(invitation_app_and_db):
    from datetime import datetime

    app, database = invitation_app_and_db
    owner = app.test_client()
    register(owner, "owner-one")
    owner.post("/groups/create", data={"name": "Conflict Group", "description": ""})
    group = database.groups.find_one({"name": "Conflict Group"})
    member = database.users.find_one({"linked_account_id": database.accounts.find_one({"normalized_username": "owner-one"})["_id"]})
    first = database.plans.insert_one({
        "group_id": group["_id"], "title": "Movie Night", "status": "scheduled",
        "scheduled_start": datetime(2026, 10, 23, 19), "scheduled_end": datetime(2026, 10, 23, 22),
        "invited_member_ids": [member["_id"]], "rsvps": {str(member["_id"]): {"status": "attending"}},
    }).inserted_id
    second = database.plans.insert_one({
        "group_id": group["_id"], "title": "Dinner", "status": "scheduled",
        "scheduled_start": datetime(2026, 10, 23, 18, 30), "scheduled_end": datetime(2026, 10, 23, 20),
        "invited_member_ids": [member["_id"]], "rsvps": {str(member["_id"]): {"status": "pending"}},
    }).inserted_id
    page = owner.get(f"/plans/{first}").get_data(as_text=True)
    assert "Scheduling conflict" in page
    assert "Dinner" in page
    assert f"/plans/{second}" in page
