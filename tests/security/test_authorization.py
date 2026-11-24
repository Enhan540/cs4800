import copy
import json
import re
from pathlib import Path

import pytest

from tests.security.conftest import login, token

pytestmark = pytest.mark.security
ROUTES = json.loads((Path(__file__).resolve().parents[2] / ".security/routes.json").read_text())
PROTECTED = [row for row in ROUTES if row["access"] == "authenticated"]
OBJECT_ROUTES = [row for row in PROTECTED if "<" in row["path"]]


def path_for(row, ids):
    mapping = {"group_id": "group", "plan_id": "plan", "user_id": "owner", "connection_id": "connection", "notification_id": "notification", "invitation_id": "invite", "availability_id": "window"}
    return re.sub(r"<([^>]+)>", lambda match: str(ids[mapping[match[1]]]), row["path"])


def snapshot(db):
    result = {name: list(db[name].find({})) for name in db.list_collection_names() if name != "security_rate_limits"}
    for record in result.get("security_sessions", []):
        record.pop("last_seen_at", None)
    return result


@pytest.mark.parametrize("row,method", [(row, method) for row in PROTECTED for method in row["methods"]], ids=lambda value: value["endpoint"] if isinstance(value, dict) else value)
def test_every_protected_route_rejects_anonymous(secured, row, method):
    app, db, ids, password = secured
    c = app.test_client()
    csrf = token(c)
    response = c.open(path_for(row, ids), method=method, headers={"X-CSRF-Token": csrf}, json={"ids": []} if "/api/notifications/" in row["path"] and method == "POST" else None)
    assert response.status_code in {302, 401, 403}
    assert b"Private audit" not in response.data
    if response.status_code == 302:
        assert "/login" in response.headers["Location"]


@pytest.mark.parametrize("row,method", [(row, method) for row in OBJECT_ROUTES for method in row["methods"]], ids=lambda value: value["endpoint"] if isinstance(value, dict) else value)
def test_object_routes_reject_unrelated_user_without_writes(secured, row, method):
    app, db, ids, password = secured
    c = app.test_client()
    csrf = login(c, "outsider", password)
    before = copy.deepcopy(snapshot(db))
    response = c.open(path_for(row, ids), method=method, headers={"X-CSRF-Token": csrf})
    assert response.status_code in {403, 404}
    assert b"Private audit" not in response.data
    assert snapshot(db) == before


def test_authorized_availability_write_and_revoked_admin(secured):
    app, db, ids, password = secured
    c = app.test_client()
    csrf = login(c, "admin", password)
    path = f"/availability/{ids['window']}/edit"
    data = {"day": "Monday", "start_time": "10:00", "end_time": "11:00"}
    assert c.post(path, data=data, headers={"X-CSRF-Token": csrf}).status_code == 302
    assert db.availability.find_one({"_id": ids["window"]})["start_time"] == "10:00"
    db.groups.update_one({"_id": ids["group"]}, {"$set": {f"admin_permissions.{ids['admin']}": []}})
    assert c.post(path, data={**data, "start_time": "09:00"}, headers={"X-CSRF-Token": csrf}).status_code == 403
    db.groups.update_one({"_id": ids["group"]}, {"$pull": {"account_ids": ids["admin"], "member_ids": ids["admin"]}})
    assert c.get(f"/groups/{ids['group']}").status_code == 404
    assert c.get(f"/users/{ids['owner']}/availability").status_code == 404


def test_mixed_owner_notification_batch_only_changes_owned_records(secured):
    app, db, ids, password = secured
    c = app.test_client()
    csrf = login(c, "outsider", password)
    response = c.post("/api/notifications/delete", json={"ids": [str(ids["notification"]), str(ids["outsider_notification"])]}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert db.notifications.find_one({"_id": ids["notification"]})
    assert not db.notifications.find_one({"_id": ids["outsider_notification"]})


def test_mixed_owner_availability_batch_is_atomic(secured):
    from bson import ObjectId
    app, db, ids, password = secured
    c = app.test_client()
    csrf = login(c, "owner", password)
    other = db.availability.insert_one({"user_id": ids["outsider"], "day": "Monday", "start_time": "09:00", "end_time": "10:00"}).inserted_id
    before = copy.deepcopy(list(db.availability.find({})))
    changes = [{"id": str(ids["window"]), "deleted": True}, {"id": str(other), "deleted": True}]
    c.post(f"/users/{ids['owner']}/availability/bulk-edit", data={"availability_payload": json.dumps(changes)}, headers={"X-CSRF-Token": csrf})
    assert list(db.availability.find({})) == before


@pytest.mark.parametrize("status", ["declined", "retracted", "revoked", "accepted"])
def test_inactive_invitation_cannot_be_accepted(secured, status):
    app, db, ids, password = secured
    db.groups.update_one({"_id": ids["group"]}, {"$pull": {"account_ids": ids["member"], "member_ids": ids["member"]}})
    db.group_invites.update_one({"_id": ids["invite"]}, {"$set": {"status": status}})
    c = app.test_client()
    csrf = login(c, "member", password)
    before = copy.deepcopy(db.groups.find_one({"_id": ids["group"]}))
    c.post(f"/invitations/{ids['invite']}/accept", headers={"X-CSRF-Token": csrf})
    assert db.groups.find_one({"_id": ids["group"]}) == before


@pytest.mark.parametrize("value", [[], {}, None, 42])
def test_bulk_ids_are_typed_before_lookup(secured, value):
    app, db, ids, password = secured
    c = app.test_client()
    csrf = login(c, "owner", password)
    before = copy.deepcopy(list(db.availability.find({})))
    result = c.post(f"/users/{ids['owner']}/availability/bulk-edit", data={"availability_payload": json.dumps([{"id": value, "deleted": True}])}, headers={"X-CSRF-Token": csrf, "Accept": "application/json"})
    assert result.status_code == 400
    assert list(db.availability.find({})) == before


def test_invitation_revocation_wins_before_membership_is_granted(secured, monkeypatch):
    app, db, ids, password = secured
    db.groups.update_one({"_id": ids["group"]}, {"$pull": {"account_ids": ids["member"], "member_ids": ids["member"]}, "$unset": {f"account_roles.{ids['member']}": ""}})
    c = app.test_client()
    csrf = login(c, "member", password)
    before = copy.deepcopy(db.groups.find_one({"_id": ids["group"]}))
    collection_type = type(db.groups)
    original_find = collection_type.find_one
    changed = {"value": False}

    def revoke_before_group_lookup(collection, query=None, *args, **kwargs):
        if collection.name == "groups" and query == {"_id": ids["group"]} and not changed["value"]:
            db.group_invites.update_one({"_id": ids["invite"], "status": "pending"}, {"$set": {"status": "revoked"}})
            changed["value"] = True
        return original_find(collection, query, *args, **kwargs)

    monkeypatch.setattr(collection_type, "find_one", revoke_before_group_lookup)
    response = c.post(f"/invitations/{ids['invite']}/accept", headers={"X-CSRF-Token": csrf, "Accept": "application/json"})
    assert changed["value"]
    assert response.status_code == 409
    assert db.groups.find_one({"_id": ids["group"]}) == before
    assert db.group_invites.find_one({"_id": ids["invite"]})["status"] == "revoked"


@pytest.mark.parametrize("linked", [True, False])
def test_distinct_legacy_member_ids_preserve_sharing_and_private_account_fields(secured, linked):
    from datetime import timedelta

    from bson import ObjectId
    from app import utc_now

    app, db, ids, password = secured
    legacy_id = ObjectId()
    member = db.users.find_one({"_id": ids["owner"]})
    db.users.delete_one({"_id": ids["owner"]})
    member["_id"] = legacy_id
    if not linked:
        member.pop("linked_account_id", None)
    db.users.insert_one(member)
    db.groups.update_one({"_id": ids["group"]}, {"$pull": {"member_ids": ids["owner"]}})
    db.groups.update_one({"_id": ids["group"]}, {"$addToSet": {"member_ids": legacy_id}})
    db.availability.update_one({"_id": ids["window"]}, {"$set": {"user_id": legacy_id}})
    db.calendar_connections.update_one({"_id": ids["connection"]}, {"$set": {"sync_status": "connected", "last_synced_at": utc_now(), "cache_start": utc_now() - timedelta(days=365), "cache_end": utc_now() + timedelta(days=365), "next_sync_attempt_at": utc_now() + timedelta(hours=1), "secret_ciphertext": "PRIVATE-CALENDAR-TOKEN-CANARY"}})
    observer, editor, outsider = app.test_client(), app.test_client(), app.test_client()
    login(observer, "member", password)
    csrf = login(editor, "admin", password)
    other_csrf = login(outsider, "outsider", password)
    assert observer.get(f"/users/{legacy_id}/availability").status_code == 403
    shared = observer.get(f"/groups/{ids['group']}")
    assert shared.status_code == 200
    response = editor.get(f"/users/{legacy_id}/availability")
    assert response.status_code == 200
    for value in ("PRIVATE-CALENDAR-TOKEN-CANARY", "PRIVATE-CALENDAR-CANARY", "owner@example.invalid", db.accounts.find_one({"_id": ids["owner"]})["password_hash"]):
        assert value.encode() not in response.data
        assert value.encode() not in shared.data
    data = {"day": "Monday", "start_time": "10:00", "end_time": "11:00"}
    path = f"/availability/{ids['window']}/edit"
    assert editor.post(path, data=data, headers={"X-CSRF-Token": csrf}).status_code == 302
    db.groups.update_one({"_id": ids["group"]}, {"$set": {f"admin_permissions.{ids['admin']}": []}})
    saved = db.availability.find_one({"_id": ids["window"]})
    assert editor.post(path, data={**data, "start_time": "09:00"}, headers={"X-CSRF-Token": csrf}).status_code == 403
    assert outsider.get(f"/users/{legacy_id}/availability").status_code == 404
    assert outsider.post(path, data=data, headers={"X-CSRF-Token": other_csrf}).status_code == 404
    assert db.availability.find_one({"_id": ids["window"]}) == saved
