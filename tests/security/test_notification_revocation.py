"""Group revocation must apply to indirect notification paths."""

from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from app import utc_now
from services.notification_service import create_notification, GROUP_INVITE, PLAN_SCHEDULED
from tests.security.conftest import login

pytestmark = pytest.mark.security
CANARY = "SYNTHETIC-POST-REVOCATION-PRIVATE-PLAN"


def actors(secured):
    app, db, ids, password = secured
    db.calendar_connections.delete_many({})
    owner, member = app.test_client(), app.test_client()
    owner_proof = login(owner, "owner", password)
    member_proof = login(member, "member", password)
    return owner, member, owner_proof, member_proof


def revoke(owner, proof, ids, mode="remove"):
    path = f"/groups/{ids['group']}/delete" if mode == "delete" else f"/groups/{ids['group']}/members/{ids['member']}/remove"
    assert owner.post(path, headers={"X-CSRF-Token": proof}).status_code == 302


def stale_notice(db, ids):
    document = {"_id": ObjectId(), "recipient_user_id": ids["member"], "group_id": ids["group"], "plan_id": ids["plan"], "type": PLAN_SCHEDULED, "status": "unresolved", "read_at": None, "created_at": utc_now() + timedelta(seconds=1), "metadata": {"plan_title": CANARY, "scheduled_start": "2027-01-04T09:00:00", "scheduled_end": "2027-01-04T09:30:00"}}
    db.notifications.insert_one(document)
    return document


def test_scheduling_existing_plan_cannot_notify_a_removed_participant(secured):
    app, db, ids, password = secured
    owner, member, proof, _ = actors(secured)
    revoke(owner, proof, ids)
    match = {"date": "2027-01-04", "day": "Monday", "start_time": "09:00", "end_time": "09:30", "attendance_count": 1, "total_members": 2, "available_member_ids": [str(ids["owner"])], "is_exact": True, "available_duration_minutes": 30}
    db.plans.update_one({"_id": ids["plan"]}, {"$set": {"title": CANARY, "matches": [match]}})
    assert owner.post(f"/plans/{ids['plan']}/schedule", data={"match_index": "0"}, headers={"X-CSRF-Token": proof}).status_code == 302
    assert db.plans.find_one({"_id": ids["plan"]})["status"] == "scheduled"
    assert db.notifications.count_documents({"recipient_user_id": ids["member"], "plan_id": ids["plan"]}) == 0
    assert CANARY not in member.get("/api/notifications").text
    assert member.get(f"/plans/{ids['plan']}").status_code == 404


@pytest.mark.parametrize("mode", ["remove", "delete"])
@pytest.mark.parametrize("path", ["/api/notifications", "/api/notifications/summary", "/account", "/dashboard"])
def test_stored_or_racing_plan_notices_do_not_bypass_group_revocation(secured, mode, path):
    app, db, ids, password = secured
    owner, member, proof, _ = actors(secured)
    revoke(owner, proof, ids, mode)
    # Represents legacy queued data or a producer racing membership removal.
    notice = stale_notice(db, ids)
    response = member.get(path)
    assert response.status_code == 200
    assert CANARY not in response.text
    if path.startswith("/api/notifications"):
        assert response.get_json()["unread_count"] == 0
    if path.endswith("/summary"):
        assert response.get_json()["latest_created_at"] is None
    assert db.notifications.find_one({"_id": notice["_id"]}), "Filtering must not delete unrelated owned records"


def test_pending_invitations_and_private_account_alerts_remain_visible(secured):
    app, db, ids, password = secured
    owner, member, proof, _ = actors(secured)
    revoke(owner, proof, ids)
    stale_notice(db, ids)
    invite = create_notification(db, ids["member"], GROUP_INVITE, group_id=ids["group"], invite_id=ids["invite"], metadata={"group_name": "Explicit new invitation"})
    alert_id = ObjectId()
    db.notifications.insert_one({"_id": alert_id, "recipient_user_id": ids["member"], "type": "SECURITY_CHANGE", "status": "unresolved", "read_at": None, "created_at": utc_now(), "metadata": {"message": "Private account security update"}})
    result = member.get("/api/notifications").get_json()
    assert {row["id"] for row in result["notifications"]} == {str(invite["_id"]), str(alert_id)}
    assert result["unread_count"] == 2


def test_plan_notice_requires_matching_group_and_current_account_membership(secured):
    app, db, ids, password = secured
    other = ObjectId()
    db.groups.insert_one({"_id": other, "owner_account_id": ids["member"], "account_ids": [ids["member"]], "member_ids": [ids["member"]]})
    assert create_notification(db, ids["member"], PLAN_SCHEDULED, group_id=other, plan_id=ids["plan"], metadata={"plan_title": CANARY}) is None
    assert create_notification(db, ids["outsider"], PLAN_SCHEDULED, group_id=ids["group"], plan_id=ids["plan"], metadata={"plan_title": CANARY}) is None
    assert db.notifications.count_documents({"metadata.plan_title": CANARY}) == 0


@pytest.mark.parametrize("mode", ["remove", "delete"])
def test_stored_notification_actions_cannot_mutate_a_revoked_plan(secured, mode):
    app, db, ids, password = secured
    owner, member, proof, member_proof = actors(secured)
    revoke(owner, proof, ids, mode)
    notice = stale_notice(db, ids)
    before_plan = db.plans.find_one({"_id": ids["plan"]})
    before_notice = db.notifications.find_one({"_id": notice["_id"]})
    response = member.post(f"/notifications/{notice['_id']}/rsvp", data={"status": "attending"}, headers={"X-CSRF-Token": member_proof})
    assert response.status_code == 404 and CANARY not in response.text
    assert db.plans.find_one({"_id": ids["plan"]}) == before_plan
    assert db.notifications.find_one({"_id": notice["_id"]}) == before_notice


def test_members_keep_own_actions_but_lose_group_actions_after_removal(secured):
    app, db, ids, password = secured
    owner, member, owner_proof, member_proof = actors(secured)
    db.plans.update_one({"_id": ids["plan"]}, {"$set": {"status": "scheduled", "scheduled_start": datetime(2027, 1, 4, 9), "scheduled_end": datetime(2027, 1, 4, 10)}})
    data = {"member_id": str(ids["member"]), "status": "not_attending"}
    assert member.post(f"/plans/{ids['plan']}/rsvp", data=data, headers={"X-CSRF-Token": member_proof}).status_code == 302
    assert db.plans.find_one({"_id": ids["plan"]})["rsvps"][str(ids["member"])]["status"] == "not_attending"
    revoke(owner, owner_proof, ids)
    before = db.plans.find_one({"_id": ids["plan"]})
    assert member.post(f"/plans/{ids['plan']}/rsvp", data={**data, "status": "attending"}, headers={"X-CSRF-Token": member_proof}).status_code == 404
    assert db.plans.find_one({"_id": ids["plan"]}) == before
    assert member.get("/account").status_code == 200
    assert member.get(f"/users/{ids['member']}/availability").status_code == 200

