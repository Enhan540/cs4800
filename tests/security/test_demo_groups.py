"""Demo groups stay private, isolated from real scheduling, and fully removable."""

import copy
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from app import DEMO_MEMBERS, utc_now
from tests.security.conftest import login
from tests.security.test_authorization import snapshot

pytestmark = pytest.mark.security

DEMO_WINDOWS = sum(len(windows) for _name, windows in DEMO_MEMBERS)
MONDAY = "2027-01-04"


def create_demo(app, db, ids, password):
    client = app.test_client()
    proof = login(client, "owner", password)
    # Demo scheduling must never trigger a calendar fetch in tests.
    db.calendar_connections.delete_many({})
    response = client.post("/groups/demo", headers={"X-CSRF-Token": proof})
    assert response.status_code == 302
    group = db.groups.find_one({"is_demo": True, "owner_account_id": ids["owner"]})
    assert response.headers["Location"].endswith(f"/groups/{group['_id']}")
    return client, proof, group


def fake_ids(db, group):
    return [item["_id"] for item in db.users.find({"is_demo": True, "demo_group_id": group["_id"]})]


def scheduled_plan(db, group_id, member_ids, start_hour=9):
    plan_id = ObjectId()
    db.plans.insert_one({
        "_id": plan_id, "group_id": group_id, "title": "Synthetic scheduled plan", "status": "scheduled",
        "start_date": MONDAY, "end_date": MONDAY, "duration_minutes": 30, "minimum_attendance": 1,
        "invited_member_ids": member_ids, "rsvps": {},
        "scheduled_start": datetime(2027, 1, 4, start_hour), "scheduled_end": datetime(2027, 1, 4, start_hour, 30),
    })
    return plan_id


def preview(client, proof, group_id):
    response = client.post(f"/groups/{group_id}/schedule-preview", headers={"X-CSRF-Token": proof}, data={
        "start_date": MONDAY, "end_date": MONDAY, "duration_minutes": "30", "minimum_attendance": "1",
        "earliest_time": "09:00", "latest_time": "10:00",
    })
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["results"]


def test_demo_group_has_only_the_creator_and_fake_profiles(secured):
    app, db, ids, password = secured
    accounts_before = db.accounts.count_documents({})
    _client, _proof, group = create_demo(app, db, ids, password)
    fakes = list(db.users.find({"is_demo": True, "demo_group_id": group["_id"]}))
    assert group["account_ids"] == [ids["owner"]] and group["account_roles"] == {str(ids["owner"]): "owner"}
    assert group["member_ids"] == [ids["owner"], *[fake["_id"] for fake in fakes]]
    assert len(fakes) == len(DEMO_MEMBERS)
    for fake in fakes:
        assert fake["display_name"].endswith("(demo)") and fake["account_id"] == ids["owner"]
        assert "linked_account_id" not in fake and "email" not in fake
    assert db.accounts.count_documents({}) == accounts_before
    assert db.availability.count_documents({"user_id": {"$in": [fake["_id"] for fake in fakes]}}) == DEMO_WINDOWS
    assert aware(group["demo_expires_at"]) - utc_now() > timedelta(days=6)


def aware(value):
    from app import aware_utc
    return aware_utc(value)


def test_only_one_active_demo_group_per_account(secured):
    app, db, ids, password = secured
    client, proof, group = create_demo(app, db, ids, password)
    again = client.post("/groups/demo", headers={"X-CSRF-Token": proof})
    assert again.status_code == 302 and again.headers["Location"].endswith(f"/groups/{group['_id']}")
    assert db.groups.count_documents({"is_demo": True}) == 1
    assert db.users.count_documents({"is_demo": True}) == len(DEMO_MEMBERS)


@pytest.mark.parametrize("actor", ["admin", "member", "outsider"])
def test_other_accounts_cannot_reach_or_change_a_demo_group(secured, actor):
    app, db, ids, password = secured
    _client, _proof, group = create_demo(app, db, ids, password)
    plan_id = scheduled_plan(db, group["_id"], group["member_ids"])
    fake = fake_ids(db, group)[0]
    other = app.test_client()
    proof = login(other, actor, password)
    before = copy.deepcopy(snapshot(db))
    reads = [f"/groups/{group['_id']}", f"/plans/{plan_id}", f"/users/{fake}/availability"]
    writes = [f"/groups/{group['_id']}/demo/reset", f"/groups/{group['_id']}/delete", f"/plans/{plan_id}/delete",
              f"/groups/{group['_id']}/members/{fake}/remove"]
    for path in reads:
        response = other.get(path)
        assert response.status_code == 404 and b"(demo)" not in response.data
    for path in writes:
        assert other.post(path, headers={"X-CSRF-Token": proof}).status_code == 404
    assert snapshot(db) == before


def test_expired_demo_group_is_unreachable_and_swept_without_touching_real_data(secured):
    app, db, ids, password = secured
    client, _proof, group = create_demo(app, db, ids, password)
    plan_id = scheduled_plan(db, group["_id"], group["member_ids"])
    db.groups.update_one({"_id": group["_id"]}, {"$set": {"demo_expires_at": utc_now() - timedelta(minutes=1)}})
    assert client.get(f"/groups/{group['_id']}").status_code == 404
    assert client.get(f"/plans/{plan_id}").status_code == 404
    real_before = (db.groups.find_one({"_id": ids["group"]}), db.plans.find_one({"_id": ids["plan"]}),
                   db.availability.find_one({"_id": ids["window"]}))
    assert client.get("/groups").status_code == 200
    assert db.groups.find_one({"_id": group["_id"]}) is None
    assert db.users.count_documents({"is_demo": True}) == 0 and db.plans.find_one({"_id": plan_id}) is None
    assert (db.groups.find_one({"_id": ids["group"]}), db.plans.find_one({"_id": ids["plan"]}),
            db.availability.find_one({"_id": ids["window"]})) == real_before


def test_demo_groups_refuse_invitations_roles_and_outside_members(secured):
    app, db, ids, password = secured
    client, proof, group = create_demo(app, db, ids, password)
    fake = fake_ids(db, group)[0]
    before = copy.deepcopy(snapshot(db))
    headers = {"X-CSRF-Token": proof}
    assert client.get(f"/groups/{group['_id']}/invitations/search", query_string={"q": "audit"}).status_code == 404
    assert client.post(f"/groups/{group['_id']}/invitations", headers=headers, data={"username": "audit-member"}).status_code == 404
    assert client.post(f"/groups/{group['_id']}/members/{fake}/role", headers=headers, data={"role": "admin"}).status_code == 404
    assert client.post("/users", headers=headers, data={"group_id": str(group["_id"]), "display_name": "Extra"}).status_code == 404
    # A fake profile never joins a real group, even one the creator owns.
    assert client.post(f"/groups/{ids['group']}/members", headers=headers, data={"user_id": str(fake)}).status_code == 404
    assert snapshot(db) == before
    page = client.get(f"/groups/{group['_id']}").get_data(as_text=True)
    assert "invite-dialog" not in page and "Practice group." in page and "Reset Demo" in page


def test_fake_members_never_inherit_the_creators_real_plans(secured):
    app, db, ids, password = secured
    client, proof, group = create_demo(app, db, ids, password)
    # The creator is busy 09:00-09:30 in a real group.
    scheduled_plan(db, ids["group"], [ids["owner"]])
    results = preview(client, proof, group["_id"])
    nine = next(result for result in results if result["start"] == "09:00")
    # Jordan (Mon 9-1) and Riley (Mon 7-10) are free even though the creator is not.
    assert nine["attendance"] == 2


def test_demo_plans_never_block_the_creator_in_a_real_group(secured):
    app, db, ids, password = secured
    client, proof, group = create_demo(app, db, ids, password)
    scheduled_plan(db, group["_id"], group["member_ids"])
    results = preview(client, proof, ids["group"])
    assert results and results[0]["start"] == "09:00"
    plan_page = client.get(f"/plans/{ids['plan']}").get_data(as_text=True)
    assert "Synthetic scheduled plan" not in plan_page
    real_group_page = client.get(f"/groups/{ids['group']}").get_data(as_text=True)
    assert "Synthetic scheduled plan" not in real_group_page


def test_scheduling_a_demo_plan_notifies_only_the_creator_once(secured):
    app, db, ids, password = secured
    client, proof, group = create_demo(app, db, ids, password)
    plan_id = ObjectId()
    match = {"date": MONDAY, "day": "Monday", "start_time": "09:00", "end_time": "09:30", "attendance_count": 2,
             "total_members": len(group["member_ids"]), "available_member_ids": [str(member) for member in group["member_ids"][1:3]],
             "is_exact": True, "available_duration_minutes": 30}
    db.plans.insert_one({"_id": plan_id, "group_id": group["_id"], "title": "Demo plan", "status": "planning",
                         "start_date": MONDAY, "end_date": MONDAY, "duration_minutes": 30, "minimum_attendance": 1,
                         "earliest_time": "09:00", "latest_time": "17:00", "matches": [match],
                         "invited_member_ids": group["member_ids"]})
    response = client.post(f"/plans/{plan_id}/schedule", headers={"X-CSRF-Token": proof}, data={"match_index": "0"})
    assert response.status_code == 302
    notes = list(db.notifications.find({"plan_id": plan_id}))
    assert [note["recipient_user_id"] for note in notes] == [ids["owner"]]


def test_deleting_a_demo_group_removes_only_its_practice_data(secured):
    app, db, ids, password = secured
    client, proof, group = create_demo(app, db, ids, password)
    fakes = fake_ids(db, group)
    plan_id = scheduled_plan(db, group["_id"], group["member_ids"])
    db.notifications.insert_one({"recipient_user_id": ids["owner"], "type": "plan_scheduled", "status": "unresolved",
                                 "group_id": group["_id"], "plan_id": plan_id, "created_at": utc_now()})
    real_before = (db.groups.find_one({"_id": ids["group"]}), db.users.find_one({"_id": ids["owner"]}),
                   db.availability.find_one({"_id": ids["window"]}), db.notifications.count_documents({"group_id": {"$ne": group["_id"]}}))
    assert client.post(f"/groups/{group['_id']}/delete", headers={"X-CSRF-Token": proof}).status_code == 302
    assert db.groups.find_one({"_id": group["_id"]}) is None
    assert db.users.count_documents({"_id": {"$in": fakes}}) == 0
    assert db.availability.count_documents({"user_id": {"$in": fakes}}) == 0
    assert db.plans.count_documents({"group_id": group["_id"]}) == 0
    assert db.notifications.count_documents({"group_id": group["_id"]}) == 0
    assert (db.groups.find_one({"_id": ids["group"]}), db.users.find_one({"_id": ids["owner"]}),
            db.availability.find_one({"_id": ids["window"]}), db.notifications.count_documents({"group_id": {"$ne": group["_id"]}})) == real_before


def test_reset_restores_the_default_fake_members_and_removes_plans(secured):
    app, db, ids, password = secured
    client, proof, group = create_demo(app, db, ids, password)
    first = fake_ids(db, group)
    db.availability.delete_many({"user_id": first[0]})
    scheduled_plan(db, group["_id"], group["member_ids"])
    assert client.post(f"/groups/{group['_id']}/demo/reset", headers={"X-CSRF-Token": proof}).status_code == 302
    after = fake_ids(db, group)
    assert len(after) == len(DEMO_MEMBERS) and not set(after) & set(first)
    assert db.users.count_documents({"_id": {"$in": first}}) == 0
    assert db.availability.count_documents({"user_id": {"$in": after}}) == DEMO_WINDOWS
    assert db.plans.count_documents({"group_id": group["_id"]}) == 0
    assert db.groups.find_one({"_id": group["_id"]})["member_ids"] == [ids["owner"], *after]


def test_reset_is_refused_for_real_groups(secured):
    app, db, ids, password = secured
    client = app.test_client()
    proof = login(client, "owner", password)
    before = copy.deepcopy(snapshot(db))
    assert client.post(f"/groups/{ids['group']}/demo/reset", headers={"X-CSRF-Token": proof}).status_code == 404
    assert snapshot(db) == before


def test_demo_creation_requires_login_and_csrf(secured):
    app, db, ids, password = secured
    anonymous = app.test_client()
    assert anonymous.post("/groups/demo").status_code in {302, 400, 401}
    signed_in = app.test_client()
    login(signed_in, "owner", password)
    assert signed_in.post("/groups/demo").status_code == 400
    assert db.groups.count_documents({"is_demo": True}) == 0


def test_dashboard_and_member_lists_exclude_demo_data(secured):
    app, db, ids, password = secured
    client, _proof, group = create_demo(app, db, ids, password)
    dashboard = client.get("/dashboard").get_data(as_text=True)
    assert "(demo)" not in dashboard and "Practice planning with fake members" not in dashboard
    groups_page = client.get("/groups").get_data(as_text=True)
    assert "Demo group" in groups_page and "Try a demo group" not in groups_page
