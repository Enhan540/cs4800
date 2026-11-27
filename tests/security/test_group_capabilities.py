"""Exercise individual group capabilities through real login and CSRF."""

import ast
import copy
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from bson import ObjectId

from app import ADMIN_PERMISSIONS
from tests.security.conftest import login
from tests.security.test_authorization import ROUTES, snapshot

pytestmark = pytest.mark.security


@dataclass(frozen=True)
class Operation:
    endpoint: str
    permission: str | None
    method: str = "POST"


OPERATIONS = [
    Operation("users", "manage_members"),
    Operation("edit_group", "edit_group", "GET"),
    Operation("edit_group", "edit_group"),
    Operation("add_group_member", "manage_members"),
    Operation("search_group_invitation_accounts", "manage_members", "GET"),
    Operation("invite_group_account", "manage_members"),
    Operation("retract_group_invitation", "manage_members"),
    Operation("update_group_member_role", None),
    Operation("remove_group_member", "manage_members"),
    Operation("delete_group", None),
    Operation("reset_demo_group", None),
    Operation("member_availability", "edit_member_availability", "GET"),
    Operation("member_availability", "edit_member_availability"),
    Operation("bulk_edit_availability", "edit_member_availability"),
    Operation("edit_availability", "edit_member_availability"),
    Operation("delete_availability", "edit_member_availability"),
    Operation("create_plan", "manage_plans", "GET"),
    Operation("create_plan", "manage_plans"),
    Operation("schedule_preview", "manage_plans"),
    Operation("edit_plan", "manage_plans", "GET"),
    Operation("edit_plan", "manage_plans"),
    Operation("schedule_plan", "manage_plans"),
    Operation("invite_plan_member", "manage_plans"),
    Operation("update_rsvp", "edit_member_rsvp"),
    Operation("delete_plan", "manage_plans"),
    Operation("find_plan_times", "manage_plans"),
]
STATES = ["owner", "granted", "other_permission", "ordinary_member", "revoked", "removed", "outsider", "unknown_permission"]


def prepare_operation(db, ids, actor, operation):
    # No calendar refresh or third-party request is part of these role tests.
    db.calendar_connections.delete_many({})
    legacy = ObjectId()
    db.users.insert_one({"_id": legacy, "account_id": ids[actor], "display_name": "Synthetic legacy profile", "normalized_name": "synthetic-legacy-profile"})
    db.availability.update_one({"_id": ids["window"]}, {"$set": {"user_id": ids["member"]}})
    match = {"date": "2027-01-04", "day": "Monday", "start_time": "09:00", "end_time": "09:30", "attendance_count": 1, "total_members": 2, "available_member_ids": [str(ids["member"])], "is_exact": True, "available_duration_minutes": 30}
    db.plans.update_one({"_id": ids["plan"]}, {"$set": {"earliest_time": "09:00", "latest_time": "17:00", "matches": [match]}})
    if operation.endpoint in {"update_rsvp", "invite_plan_member"}:
        db.plans.update_one({"_id": ids["plan"]}, {"$set": {"status": "scheduled", "scheduled_start": datetime(2027, 1, 4, 9), "scheduled_end": datetime(2027, 1, 4, 9, 30)}})
    row = next(row for row in ROUTES if row["endpoint"] == operation.endpoint)
    path = row["path"]
    replacements = {"group_id": ids["group"], "plan_id": ids["plan"], "user_id": ids["member"], "availability_id": ids["window"], "invitation_id": ids["invite"]}
    for field, value in replacements.items():
        path = path.replace("<" + field + ">", str(value))
    data = {"owner_account_id": str(ids["outsider"]), "account_ids": [str(ids["outsider"])], "admin_permissions": json.dumps({str(ids["outsider"]): list(ADMIN_PERMISSIONS)}), "group_id": str(ids["group"])}
    plan = {"title": "Permission-controlled plan", "start_date": "2027-01-04", "end_date": "2027-01-04", "duration_minutes": "30", "minimum_attendance": "1", "earliest_time": "09:00", "latest_time": "17:00"}
    endpoint = operation.endpoint
    if endpoint == "users":data.update(display_name="Permission-created legacy profile")
    elif endpoint == "edit_group":data.update(name="Permission-controlled group", description="Synthetic description")
    elif endpoint == "add_group_member":data.update(user_id=str(legacy))
    elif endpoint == "invite_group_account":data.update(username="audit-outsider", account_id=str(ids["owner"]))
    elif endpoint == "update_group_member_role":data.update(role="admin", permissions=["edit_group", "unknown-permission"])
    elif endpoint == "member_availability":data.update(day="Tuesday", start_time="12:00", end_time="13:00")
    elif endpoint == "bulk_edit_availability":data.update(availability_payload=json.dumps([{"id": str(ids["window"]), "start_time": "10:00", "end_time": "11:00"}]))
    elif endpoint == "edit_availability":data.update(day="Monday", start_time="10:00", end_time="11:00")
    elif endpoint in {"create_plan", "edit_plan", "schedule_preview"}:data.update(plan)
    elif endpoint == "schedule_plan":data.update(match_index="0")
    elif endpoint == "invite_plan_member":data.update(member_id=str(ids["admin"]))
    elif endpoint == "update_rsvp":data.update(member_id=str(ids["member"]), status="not_attending")
    return path, data, legacy


def assert_authorized_effect(db, ids, actor, operation, response, legacy):
    assert response.status_code in {200, 302}
    endpoint = operation.endpoint
    if operation.method == "GET":
        assert response.status_code == 200
        if endpoint == "search_group_invitation_accounts":
            rows = response.get_json()["results"]
            assert rows and all(set(row) <= {"id", "username", "state"} for row in rows)
        return
    group = db.groups.find_one({"_id": ids["group"]})
    plan = db.plans.find_one({"_id": ids["plan"]})
    if endpoint == "users":
        record = db.users.find_one({"normalized_name": "permission-created legacy profile"})
        assert record and record["account_id"] == ids[actor] and record["_id"] in group["member_ids"]
    elif endpoint == "edit_group":
        assert group["name"] == "Permission-controlled group" and group["owner_account_id"] == ids["owner"]
        assert ids["outsider"] not in group["account_ids"]
    elif endpoint == "add_group_member":assert legacy in group["member_ids"]
    elif endpoint == "invite_group_account":
        assert db.group_invites.find_one({"invited_user_id": ids["outsider"], "group_id": ids["group"], "status": "pending"})
        assert ids["outsider"] not in group["account_ids"]
    elif endpoint == "retract_group_invitation":assert db.group_invites.find_one({"_id": ids["invite"]})["status"] == "revoked"
    elif endpoint == "update_group_member_role":
        assert group["account_roles"][str(ids["member"])] == "admin"
        assert group["admin_permissions"][str(ids["member"])] == ["edit_group"]
    elif endpoint == "remove_group_member":
        assert ids["member"] not in group["member_ids"] and ids["member"] not in group["account_ids"]
        assert db.users.find_one({"_id": ids["member"]}) and db.accounts.find_one({"_id": ids["member"]})
    elif endpoint == "delete_group":
        assert group is None and db.plans.count_documents({"group_id": ids["group"]}) == 0
        assert db.accounts.count_documents({}) == 4
    elif endpoint == "reset_demo_group":
        fakes = list(db.users.find({"is_demo": True, "demo_group_id": ids["group"]}))
        assert len(fakes) == 5 and all(fake["_id"] in group["member_ids"] and "linked_account_id" not in fake for fake in fakes)
        assert db.plans.count_documents({"group_id": ids["group"]}) == 0
        assert db.accounts.count_documents({}) == 4
    elif endpoint == "member_availability":assert db.availability.find_one({"user_id": ids["member"], "day": "Tuesday", "start_time": "12:00"})
    elif endpoint in {"bulk_edit_availability", "edit_availability"}:assert db.availability.find_one({"_id": ids["window"]})["start_time"] == "10:00"
    elif endpoint == "delete_availability":assert db.availability.find_one({"_id": ids["window"]}) is None
    elif endpoint == "create_plan":assert db.plans.find_one({"title": "Permission-controlled plan", "group_id": ids["group"]})
    elif endpoint == "edit_plan":assert plan["title"] == "Permission-controlled plan" and plan["group_id"] == ids["group"]
    elif endpoint == "schedule_preview":assert response.get_json()["success"] is True
    elif endpoint == "schedule_plan":assert plan["status"] == "scheduled"
    elif endpoint == "invite_plan_member":assert ids["admin"] in plan["invited_member_ids"]
    elif endpoint == "update_rsvp":assert plan["rsvps"][str(ids["member"])]["status"] == "not_attending"
    elif endpoint == "delete_plan":assert plan is None
    elif endpoint == "find_plan_times":assert plan.get("matches_generated_at") is not None
    else:raise AssertionError("Authorized operation lacks a saved-record assertion")


@pytest.mark.parametrize("operation", OPERATIONS, ids=lambda operation: operation.endpoint + "-" + operation.method)
@pytest.mark.parametrize("state", STATES)
def test_group_capabilities_are_individual_and_rechecked_after_revocation(secured, operation, state):
    app, db, ids, password = secured
    actor = "owner" if state == "owner" else ("outsider" if state == "outsider" else "admin")
    required = operation.permission
    granted = [required] if required else sorted(ADMIN_PERMISSIONS)
    if state == "other_permission":
        granted = [next(value for value in sorted(ADMIN_PERMISSIONS) if value != required)] if required else sorted(ADMIN_PERMISSIONS)
    elif state == "ordinary_member":granted = []
    elif state == "unknown_permission":granted = ["*", "owner", "edit_group "]
    db.groups.update_one({"_id": ids["group"]}, {"$set": {f"account_roles.{ids['admin']}": "member" if state == "ordinary_member" else "admin", f"admin_permissions.{ids['admin']}": granted}})
    path, data, legacy = prepare_operation(db, ids, actor, operation)
    client = app.test_client()
    proof = login(client, actor, password)
    if state in {"revoked", "removed"}:
        owner = app.test_client()
        owner_proof = login(owner, "owner", password)
        if state == "revoked":
            change_path = f"/groups/{ids['group']}/members/{ids['admin']}/role"
            change_data = {"role": "admin", "permissions": []}
        else:
            change_path = f"/groups/{ids['group']}/members/{ids['admin']}/remove"
            change_data = {}
        assert owner.post(change_path, data=change_data, headers={"X-CSRF-Token": owner_proof}).status_code == 302
    if operation.endpoint == "reset_demo_group":
        # Reset exists only for demo groups; the same membership states apply.
        db.groups.update_one({"_id": ids["group"]}, {"$set": {"is_demo": True, "demo_expires_at": datetime(2099, 1, 1)}})
    before = copy.deepcopy(snapshot(db))
    accounts_before = list(db.accounts.find({}))
    response = client.open(path, method=operation.method, data=data if operation.method == "POST" else None, query_string={"q": "audit"} if operation.method == "GET" else None, headers={"X-CSRF-Token": proof})
    allowed = state == "owner" or (state == "granted" and required is not None)
    if allowed:
        assert_authorized_effect(db, ids, actor, operation, response, legacy)
        assert list(db.accounts.find({})) == accounts_before
    else:
        assert response.status_code == (404 if state in {"removed", "outsider"} else 403)
        assert snapshot(db) == before
        assert b"Private audit group" not in response.data and b"Private audit plan" not in response.data


def test_new_permission_guarded_routes_require_matrix_coverage():
    root = Path(__file__).resolve().parents[2]
    guarded = set()
    for node in ast.walk(ast.parse((root / "app.py").read_text(encoding="utf-8"))):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not any(isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and ast.unparse(item.func.value) == "app" for item in node.decorator_list):
            continue
        if any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in {"require_group_permission", "require_group_owner", "member_can_manage"} for call in ast.walk(node)):
            guarded.add(node.name)
    assert guarded <= {operation.endpoint for operation in OPERATIONS}
    assert {operation.permission for operation in OPERATIONS if operation.permission} == ADMIN_PERMISSIONS


@pytest.mark.parametrize("actor", ["owner", "admin"])
@pytest.mark.parametrize("target", ["owner", "peer_admin"])
def test_owner_and_peer_admin_removal_restrictions_are_enforced(secured, actor, target):
    app, db, ids, password = secured
    db.groups.update_one({"_id": ids["group"]}, {"$set": {f"admin_permissions.{ids['admin']}": sorted(ADMIN_PERMISSIONS), f"account_roles.{ids['member']}": "admin"}})
    client = app.test_client();proof = login(client, actor, password)
    target_id = ids["owner"] if target == "owner" else ids["member"]
    before = copy.deepcopy(snapshot(db))
    response = client.post(f"/groups/{ids['group']}/members/{target_id}/remove", headers={"X-CSRF-Token": proof})
    if actor == "owner" and target == "peer_admin":
        assert target_id not in db.groups.find_one({"_id": ids["group"]})["account_ids"]
    else:
        assert response.status_code == 302 and snapshot(db) == before


@pytest.mark.parametrize("operation", ["retract", "preview", "plan_invite", "rsvp", "role", "remove"])
def test_nested_identifiers_cannot_cross_group_or_participant_boundaries(secured, operation):
    app, db, ids, password = secured
    other_group, other_plan, other_invite = ObjectId(), ObjectId(), ObjectId()
    db.groups.insert_one({"_id": other_group, "owner_account_id": ids["outsider"], "account_ids": [ids["outsider"]], "member_ids": [ids["outsider"]], "name": "OTHER-PRIVATE-GROUP"})
    db.plans.insert_one({"_id": other_plan, "group_id": other_group, "title": "OTHER-PRIVATE-PLAN"})
    db.group_invites.insert_one({"_id": other_invite, "group_id": other_group, "invited_user_id": ids["member"], "status": "pending"})
    prepare_operation(db, ids, "owner", Operation("update_rsvp", "edit_member_rsvp"))
    client = app.test_client();proof = login(client, "owner", password)
    cases = {
        "retract": (f"/groups/{ids['group']}/invitations/{other_invite}/retract", {}),
        "preview": (f"/groups/{ids['group']}/schedule-preview", {"plan_id": str(other_plan)}),
        "plan_invite": (f"/plans/{ids['plan']}/invite", {"member_id": str(ids["outsider"])}),
        "rsvp": (f"/plans/{ids['plan']}/rsvp", {"member_id": str(ids["admin"]), "status": "attending"}),
        "role": (f"/groups/{ids['group']}/members/{ids['outsider']}/role", {"role": "admin", "permissions": sorted(ADMIN_PERMISSIONS)}),
        "remove": (f"/groups/{ids['group']}/members/{ids['outsider']}/remove", {}),
    }
    path, data = cases[operation]
    before = copy.deepcopy(snapshot(db))
    response = client.post(path, data=data, headers={"X-CSRF-Token": proof})
    assert response.status_code == 404 and snapshot(db) == before
    assert b"OTHER-PRIVATE" not in response.data


def test_owner_role_cannot_be_created_or_changed_by_mass_assignment(secured):
    app, db, ids, password = secured
    client = app.test_client();proof = login(client, "owner", password)
    before = copy.deepcopy(snapshot(db))
    response = client.post(f"/groups/{ids['group']}/members/{ids['member']}/role", data={"role": "owner", "owner_account_id": str(ids["member"])}, headers={"X-CSRF-Token": proof})
    assert response.status_code == 400 and snapshot(db) == before

