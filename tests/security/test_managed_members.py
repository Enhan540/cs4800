"""Members without accounts are managed only through their group and never stand in for a real account."""

import copy
from datetime import datetime

import pytest
from bson import ObjectId

from app import MANAGED_MEMBER_LIMIT
from tests.security.conftest import login
from tests.security.test_authorization import snapshot

pytestmark = pytest.mark.security

MONDAY = "2027-01-04"


def signed_in(app, db, name, password):
    db.calendar_connections.delete_many({})
    client = app.test_client()
    return client, {"X-CSRF-Token": login(client, name, password)}


def add_managed(client, headers, group_id, name="Grandma"):
    return client.post(f"/groups/{group_id}/managed-members", headers=headers, data={"display_name": name})


def managed(db, group_id, name="Grandma"):
    return db.users.find_one({"is_managed": True, "managed_group_id": group_id, "display_name": name})


def grant(db, ids, permissions):
    db.groups.update_one({"_id": ids["group"]}, {"$set": {f"admin_permissions.{ids['admin']}": permissions}})


def preview(client, headers, group_id):
    response = client.post(f"/groups/{group_id}/schedule-preview", headers=headers, data={
        "start_date": MONDAY, "end_date": MONDAY, "duration_minutes": "30", "minimum_attendance": "1",
        "earliest_time": "09:00", "latest_time": "10:00",
    })
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["results"]


def scheduled_plan(db, group_id, member_ids):
    plan_id = ObjectId()
    db.plans.insert_one({
        "_id": plan_id, "group_id": group_id, "title": "Synthetic scheduled plan", "status": "scheduled",
        "start_date": MONDAY, "end_date": MONDAY, "duration_minutes": 30, "minimum_attendance": 1,
        "invited_member_ids": member_ids, "rsvps": {},
        "scheduled_start": datetime(2027, 1, 4, 9), "scheduled_end": datetime(2027, 1, 4, 9, 30),
    })
    return plan_id


def second_owned_group(db, ids):
    group_id = ObjectId()
    db.groups.insert_one({"_id": group_id, "name": "Second owned group", "owner_account_id": ids["owner"],
                          "account_ids": [ids["owner"]], "member_ids": [ids["owner"]], "account_roles": {}})
    return group_id


def test_owner_adds_a_member_without_an_account(secured):
    app, db, ids, password = secured
    client, headers = signed_in(app, db, "owner", password)
    accounts_before = db.accounts.count_documents({})
    group_before = db.groups.find_one({"_id": ids["group"]})
    assert add_managed(client, headers, ids["group"], "  Grandma   Lee ").status_code == 302
    member = managed(db, ids["group"], "Grandma Lee")
    assert member and member["account_id"] == ids["owner"] and member["created_by_account_id"] == ids["owner"]
    assert "linked_account_id" not in member and "email" not in member
    group = db.groups.find_one({"_id": ids["group"]})
    assert member["_id"] in group["member_ids"] and group["account_ids"] == group_before["account_ids"]
    assert db.accounts.count_documents({}) == accounts_before
    page = client.get(f"/groups/{ids['group']}").get_data(as_text=True)
    assert "No account" in page and "+ Add member without account" in page


@pytest.mark.parametrize("name", ["", "   ", "x" * 61, "bad\x07name"])
def test_invalid_names_are_refused(secured, name):
    app, db, ids, password = secured
    client, headers = signed_in(app, db, "owner", password)
    before = copy.deepcopy(snapshot(db))
    assert add_managed(client, headers, ids["group"], name).status_code == 302
    assert snapshot(db) == before


def test_duplicate_names_and_the_group_limit_are_enforced(secured):
    app, db, ids, password = secured
    client, headers = signed_in(app, db, "owner", password)
    add_managed(client, headers, ids["group"], "Grandma")
    add_managed(client, headers, ids["group"], "GRANDMA")
    assert db.users.count_documents({"is_managed": True}) == 1
    for index in range(1, MANAGED_MEMBER_LIMIT):
        add_managed(client, headers, ids["group"], f"Person {index}")
    assert db.users.count_documents({"is_managed": True, "managed_group_id": ids["group"]}) == MANAGED_MEMBER_LIMIT
    add_managed(client, headers, ids["group"], "One too many")
    assert managed(db, ids["group"], "One too many") is None
    # Names are scoped to their group: another group may use the same name.
    other = second_owned_group(db, ids)
    add_managed(client, headers, other, "Grandma")
    assert managed(db, other, "Grandma")


@pytest.mark.parametrize("actor,permissions,status", [
    ("admin", ["edit_member_availability"], 302),
    ("admin", ["manage_members"], 403),
    ("member", [], 403),
    ("outsider", [], 404),
])
def test_availability_follows_group_permissions(secured, actor, permissions, status):
    app, db, ids, password = secured
    owner, owner_headers = signed_in(app, db, "owner", password)
    add_managed(owner, owner_headers, ids["group"])
    member = managed(db, ids["group"])
    grant(db, ids, permissions)
    client, headers = signed_in(app, db, actor, password)
    response = client.post(f"/users/{member['_id']}/availability", headers=headers,
                           data={"day": "Monday", "start_time": "09:00", "end_time": "10:00"})
    assert response.status_code == status
    assert (db.availability.count_documents({"user_id": member["_id"]}) == 1) == (status == 302)


def test_creator_loses_access_after_leaving_the_group(secured):
    app, db, ids, password = secured
    grant(db, ids, ["manage_members", "edit_member_availability"])
    admin, admin_headers = signed_in(app, db, "admin", password)
    assert add_managed(admin, admin_headers, ids["group"]).status_code == 302
    member = managed(db, ids["group"])
    assert member["account_id"] == ids["admin"]
    owner, owner_headers = signed_in(app, db, "owner", password)
    assert owner.post(f"/groups/{ids['group']}/members/{ids['admin']}/remove", headers=owner_headers).status_code == 302
    before = copy.deepcopy(snapshot(db))
    assert admin.get(f"/users/{member['_id']}/availability").status_code == 404
    assert admin.post(f"/users/{member['_id']}/availability", headers=admin_headers,
                      data={"day": "Monday", "start_time": "09:00", "end_time": "10:00"}).status_code == 404
    assert snapshot(db) == before


def test_rsvps_follow_group_permissions(secured):
    app, db, ids, password = secured
    owner, owner_headers = signed_in(app, db, "owner", password)
    add_managed(owner, owner_headers, ids["group"])
    member = managed(db, ids["group"])
    plan_id = scheduled_plan(db, ids["group"], [ids["owner"], member["_id"]])
    other, other_headers = signed_in(app, db, "member", password)
    assert other.post(f"/plans/{plan_id}/rsvp", headers=other_headers,
                      data={"member_id": str(member["_id"]), "status": "attending"}).status_code == 403
    assert owner.post(f"/plans/{plan_id}/rsvp", headers=owner_headers,
                      data={"member_id": str(member["_id"]), "status": "not_attending"}).status_code == 302
    assert db.plans.find_one({"_id": plan_id})["rsvps"][str(member["_id"])]["status"] == "not_attending"


def test_creators_real_plans_never_block_a_member_without_an_account(secured):
    app, db, ids, password = secured
    client, headers = signed_in(app, db, "owner", password)
    add_managed(client, headers, ids["group"])
    member = managed(db, ids["group"])
    client.post(f"/users/{member['_id']}/availability", headers=headers,
                data={"day": "Monday", "start_time": "09:00", "end_time": "10:00"})
    # The creator is busy 09:00-09:30 in another of their groups.
    scheduled_plan(db, second_owned_group(db, ids), [ids["owner"]])
    nine = next(result for result in preview(client, headers, ids["group"]) if result["start"] == "09:00")
    assert nine["attendance"] == 1


def test_plans_of_a_member_without_an_account_never_block_the_creator(secured):
    app, db, ids, password = secured
    client, headers = signed_in(app, db, "owner", password)
    add_managed(client, headers, ids["group"])
    member = managed(db, ids["group"])
    other = second_owned_group(db, ids)
    scheduled_plan(db, ids["group"], [member["_id"]])
    results = preview(client, headers, other)
    assert results and results[0]["start"] == "09:00"


def test_scheduling_never_notifies_on_behalf_of_members_without_accounts(secured):
    app, db, ids, password = secured
    client, headers = signed_in(app, db, "owner", password)
    add_managed(client, headers, ids["group"])
    member = managed(db, ids["group"])
    plan_id = ObjectId()
    match = {"date": MONDAY, "day": "Monday", "start_time": "09:00", "end_time": "09:30", "attendance_count": 1,
             "total_members": 2, "available_member_ids": [str(member["_id"])], "is_exact": True, "available_duration_minutes": 30}
    db.plans.insert_one({"_id": plan_id, "group_id": ids["group"], "title": "Plan", "status": "planning",
                         "start_date": MONDAY, "end_date": MONDAY, "duration_minutes": 30, "minimum_attendance": 1,
                         "earliest_time": "09:00", "latest_time": "17:00", "matches": [match],
                         "invited_member_ids": [member["_id"]]})
    assert client.post(f"/plans/{plan_id}/schedule", headers=headers, data={"match_index": "0"}).status_code == 302
    assert db.notifications.count_documents({"plan_id": plan_id}) == 0


def test_members_without_accounts_cannot_leave_their_group_or_gain_roles(secured):
    app, db, ids, password = secured
    client, headers = signed_in(app, db, "owner", password)
    add_managed(client, headers, ids["group"])
    member = managed(db, ids["group"])
    other = second_owned_group(db, ids)
    before = copy.deepcopy(snapshot(db))
    assert client.post(f"/groups/{other}/members", headers=headers, data={"user_id": str(member["_id"])}).status_code == 404
    assert client.post(f"/groups/{other}/managed-members/{member['_id']}/rename", headers=headers,
                       data={"display_name": "Moved"}).status_code == 404
    assert client.post(f"/groups/{ids['group']}/members/{member['_id']}/role", headers=headers,
                       data={"role": "admin"}).status_code in {403, 404}
    assert snapshot(db) == before


def test_rename_keeps_names_unique_in_the_group(secured):
    app, db, ids, password = secured
    client, headers = signed_in(app, db, "owner", password)
    add_managed(client, headers, ids["group"], "Grandma")
    add_managed(client, headers, ids["group"], "Coach")
    coach = managed(db, ids["group"], "Coach")
    client.post(f"/groups/{ids['group']}/managed-members/{coach['_id']}/rename", headers=headers, data={"display_name": "grandma"})
    assert db.users.find_one({"_id": coach["_id"]})["display_name"] == "Coach"
    client.post(f"/groups/{ids['group']}/managed-members/{coach['_id']}/rename", headers=headers, data={"display_name": "Coach Lee"})
    assert db.users.find_one({"_id": coach["_id"]})["display_name"] == "Coach Lee"
    # Rename only applies to members without accounts.
    assert client.post(f"/groups/{ids['group']}/managed-members/{ids['member']}/rename", headers=headers,
                       data={"display_name": "Renamed"}).status_code == 404


@pytest.mark.parametrize("actor", ["owner", "admin"])
def test_removing_deletes_the_profile_and_its_traces(secured, actor):
    app, db, ids, password = secured
    grant(db, ids, ["manage_members"])
    owner, owner_headers = signed_in(app, db, "owner", password)
    add_managed(owner, owner_headers, ids["group"])
    member = managed(db, ids["group"])
    owner.post(f"/users/{member['_id']}/availability", headers=owner_headers,
               data={"day": "Monday", "start_time": "09:00", "end_time": "10:00"})
    plan_id = scheduled_plan(db, ids["group"], [ids["owner"], member["_id"]])
    db.plans.update_one({"_id": plan_id}, {"$set": {f"rsvps.{member['_id']}": {"status": "attending"},
                                                     "scheduled_available_member_ids": [member["_id"]]}})
    client, headers = signed_in(app, db, actor, password)
    assert client.post(f"/groups/{ids['group']}/members/{member['_id']}/remove", headers=headers).status_code == 302
    assert db.users.find_one({"_id": member["_id"]}) is None
    assert db.availability.count_documents({"user_id": member["_id"]}) == 0
    group = db.groups.find_one({"_id": ids["group"]})
    plan = db.plans.find_one({"_id": plan_id})
    assert member["_id"] not in group["member_ids"] and member["_id"] not in plan["invited_member_ids"]
    assert str(member["_id"]) not in plan["rsvps"] and member["_id"] not in plan["scheduled_available_member_ids"]
    assert ids["member"] in group["member_ids"] and db.users.find_one({"_id": ids["owner"]})


def test_deleting_a_group_deletes_its_members_without_accounts(secured):
    app, db, ids, password = secured
    client, headers = signed_in(app, db, "owner", password)
    add_managed(client, headers, ids["group"])
    member = managed(db, ids["group"])
    client.post(f"/users/{member['_id']}/availability", headers=headers,
                data={"day": "Monday", "start_time": "09:00", "end_time": "10:00"})
    other = second_owned_group(db, ids)
    add_managed(client, headers, other, "Keeper")
    users_before = db.users.count_documents({"is_managed": {"$ne": True}})
    assert client.post(f"/groups/{ids['group']}/delete", headers=headers).status_code == 302
    assert db.users.find_one({"_id": member["_id"]}) is None
    assert db.availability.count_documents({"user_id": member["_id"]}) == 0
    assert db.availability.find_one({"_id": ids["window"]})
    assert managed(db, other, "Keeper") and db.users.count_documents({"is_managed": {"$ne": True}}) == users_before


def test_demo_groups_refuse_members_without_accounts(secured):
    app, db, ids, password = secured
    client, headers = signed_in(app, db, "owner", password)
    client.post("/groups/demo", headers=headers)
    demo = db.groups.find_one({"is_demo": True})
    assert add_managed(client, headers, demo["_id"]).status_code == 404
    assert db.users.count_documents({"is_managed": True}) == 0


def test_add_button_is_hidden_without_manage_members(secured):
    app, db, ids, password = secured
    client, _headers = signed_in(app, db, "member", password)
    page = client.get(f"/groups/{ids['group']}").get_data(as_text=True)
    assert "Add member without account" not in page
