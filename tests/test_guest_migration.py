import os
from copy import deepcopy
from datetime import datetime, timezone

import mongomock
import pytest
from werkzeug.security import check_password_hash

os.environ["MONGO_URI"] = ""
from app import create_app
from scripts.migrate_guest_accounts import migrate_guest_accounts


@pytest.fixture
def migrated_database():
    database = mongomock.MongoClient().guest_migration_test
    guest_ids = {}
    for name in ("guest1", "guest2", "guest3"):
        guest_ids[name] = database.users.insert_one(
            {
                "display_name": name,
                "normalized_name": name,
                "created_at": datetime.now(timezone.utc),
            }
        ).inserted_id
    unrelated_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id
    first_group_id = database.groups.insert_one(
        {
            "name": "Friday Friends",
            "member_ids": [guest_ids["guest1"], guest_ids["guest2"]],
            "created_at": datetime.now(timezone.utc),
        }
    ).inserted_id
    second_group_id = database.groups.insert_one(
        {
            "name": "D&D Group",
            "member_ids": [guest_ids["guest2"], guest_ids["guest3"]],
            "created_at": datetime.now(timezone.utc),
        }
    ).inserted_id
    availability_id = database.availability.insert_one(
        {
            "user_id": guest_ids["guest3"],
            "day": "Monday",
            "day_order": 0,
            "start_time": "16:00",
            "end_time": "22:00",
        }
    ).inserted_id
    plan_id = database.plans.insert_one(
        {
            "group_id": second_group_id,
            "title": "Legacy Plan",
            "invited_member_ids": [guest_ids["guest2"], guest_ids["guest3"]],
            "scheduled_available_member_ids": [guest_ids["guest2"]],
            "rsvps": {
                str(guest_ids["guest2"]): {
                    "status": "attending",
                    "arriving_late": True,
                    "arrival_time": "19:30",
                }
            },
        }
    ).inserted_id
    return database, guest_ids, unrelated_id, first_group_id, second_group_id, availability_id, plan_id


def test_migration_creates_linked_accounts_and_preserves_legacy_data(migrated_database):
    database, guest_ids, unrelated_id, first_group_id, second_group_id, availability_id, plan_id = (
        migrated_database
    )
    plan_before = deepcopy(database.plans.find_one({"_id": plan_id}))

    report = migrate_guest_accounts(database, output=lambda _message: None)

    assert report["found_guests"] == 3
    assert report["created_accounts"] == 3
    assert database.accounts.count_documents({}) == 3
    for username, member_id in guest_ids.items():
        account = database.accounts.find_one({"normalized_username": username})
        member = database.users.find_one({"_id": member_id})
        assert account["username"] == username
        assert check_password_hash(account["password_hash"], "guestpassword")
        assert account["must_change_password"] is True
        assert "password" not in account
        assert "plaintext_password" not in account
        assert member["linked_account_id"] == account["_id"]
        assert member["account_id"] == account["_id"]

    assert database.users.find_one({"_id": unrelated_id}).get("linked_account_id") is None
    assert database.availability.find_one({"_id": availability_id})["user_id"] == guest_ids["guest3"]
    assert database.plans.find_one({"_id": plan_id}) == plan_before

    guest1 = database.accounts.find_one({"normalized_username": "guest1"})
    guest2 = database.accounts.find_one({"normalized_username": "guest2"})
    for group_id in (first_group_id, second_group_id):
        group = database.groups.find_one({"_id": group_id})
        assert group["owner_account_id"] == guest1["_id"]
        assert guest1["_id"] in group["account_ids"]
        assert guest_ids["guest1"] in group["member_ids"]
        assert group["account_roles"][str(guest1["_id"])] == "owner"
    assert guest2["_id"] in database.groups.find_one({"_id": second_group_id})["account_ids"]


def test_migration_is_idempotent_and_dry_run_does_not_write(migrated_database):
    database, *_rest = migrated_database
    dry_report = migrate_guest_accounts(database, dry_run=True, output=lambda _message: None)
    assert dry_report["created_accounts"] == 3
    assert database.accounts.count_documents({}) == 0
    assert database.users.count_documents({"linked_account_id": {"$exists": True}}) == 0

    migrate_guest_accounts(database, output=lambda _message: None)
    counts_after_first = {
        "accounts": database.accounts.count_documents({}),
        "users": database.users.count_documents({}),
        "groups": database.groups.count_documents({}),
        "availability": database.availability.count_documents({}),
        "plans": database.plans.count_documents({}),
    }
    groups_after_first = list(database.groups.find({}))
    second_report = migrate_guest_accounts(database, output=lambda _message: None)

    assert second_report["created_accounts"] == 0
    assert second_report["linked_members"] == 0
    assert second_report["ownership_assignments"] == 0
    assert counts_after_first == {
        "accounts": database.accounts.count_documents({}),
        "users": database.users.count_documents({}),
        "groups": database.groups.count_documents({}),
        "availability": database.availability.count_documents({}),
        "plans": database.plans.count_documents({}),
    }
    assert groups_after_first == list(database.groups.find({}))


def test_guest_owner_permissions_and_regular_member_restrictions(migrated_database):
    database, guest_ids, _unrelated_id, first_group_id, second_group_id, *_rest = migrated_database
    migrate_guest_accounts(database, output=lambda _message: None)
    app = create_app(database_override=database)
    app.config.update(TESTING=True, SECRET_KEY="migration-test", LOGIN_DISABLED=False)
    client = app.test_client()

    assert client.post(
        "/login", data={"username": "guest2", "password": "guestpassword"}
    ).status_code == 302
    assert b"Friday Friends" in client.get("/groups").data
    assert b"D&amp;D Group" in client.get("/groups").data
    assert client.get(f"/groups/{first_group_id}/edit").status_code == 403
    assert client.post(
        f"/groups/{first_group_id}/members",
        data={"user_id": str(guest_ids["guest3"])},
    ).status_code == 403
    assert client.post(f"/groups/{first_group_id}/delete").status_code == 403
    client.post("/logout")

    assert client.post(
        "/login", data={"username": "guest1", "password": "guestpassword"}
    ).status_code == 302
    assert client.post(
        f"/groups/{first_group_id}/edit",
        data={"name": "Friday Friends Updated", "description": ""},
    ).status_code == 302
    client.post("/users", data={"display_name": "New Member", "email": ""})
    new_member = database.users.find_one({"normalized_name": "new member"})
    assert client.post(
        f"/groups/{first_group_id}/members", data={"user_id": str(new_member["_id"])}
    ).status_code == 302
    assert new_member["_id"] in database.groups.find_one({"_id": first_group_id})["member_ids"]
    assert client.post(
        f"/groups/{first_group_id}/members/{guest_ids['guest2']}/remove"
    ).status_code == 302
    assert guest_ids["guest2"] not in database.groups.find_one({"_id": first_group_id})["member_ids"]
    assert client.post(f"/groups/{second_group_id}/delete").status_code == 302
    assert database.groups.find_one({"_id": second_group_id}) is None


def test_new_group_includes_creator_as_owner_and_member():
    database = mongomock.MongoClient().new_group_owner_test
    app = create_app(database_override=database)
    app.config.update(TESTING=True, SECRET_KEY="new-owner-test", LOGIN_DISABLED=False)
    client = app.test_client()
    client.post(
        "/register",
        data={
            "username": "brandNewUser",
            "password": "long-enough",
            "confirm_password": "long-enough",
        },
    )
    account = database.accounts.find_one({"normalized_username": "brandnewuser"})
    assert database.groups.count_documents({"account_ids": account["_id"]}) == 0
    assert database.users.count_documents({"linked_account_id": account["_id"]}) == 0

    client.post("/groups/create", data={"name": "New Private Group", "description": ""})
    group = database.groups.find_one({"name": "New Private Group"})
    member = database.users.find_one({"linked_account_id": account["_id"]})
    assert group["owner_account_id"] == account["_id"]
    assert group["account_roles"][str(account["_id"])] == "owner"
    assert group["account_ids"] == [account["_id"]]
    assert group["member_ids"] == [member["_id"]]
    assert database.availability.count_documents({"user_id": member["_id"]}) == 0
