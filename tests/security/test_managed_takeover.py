"""Someone who signs up can take over a member-without-account's place, only with their consent."""

import copy
from datetime import datetime

import pytest
from bson import ObjectId

from tests.security.conftest import login
from tests.security.test_authorization import snapshot

pytestmark = pytest.mark.security


def signed_in(app, db, name, password):
    db.calendar_connections.delete_many({})
    client = app.test_client()
    return client, {"X-CSRF-Token": login(client, name, password)}


def add_managed(db, group_id, name="Grandma", owner=None):
    managed = ObjectId()
    db.users.insert_one({"_id": managed, "display_name": name, "normalized_name": f"managed:{group_id}:{name.casefold()}",
                         "account_id": owner, "is_managed": True, "managed_group_id": group_id})
    db.groups.update_one({"_id": group_id}, {"$push": {"member_ids": managed}})
    db.availability.insert_one({"user_id": managed, "day": "Monday", "day_order": 0, "start_time": "09:00", "end_time": "10:00"})
    return managed


def invite(client, headers, group_id, managed=None, username="audit-outsider", json=False):
    extra = {"X-Requested-With": "fetch", "Accept": "application/json"} if json else {}
    data = {"username": username, **({"claims_member_id": str(managed)} if managed else {})}
    return client.post(f"/groups/{group_id}/invitations", headers={**headers, **extra}, data=data)


def pending_invite(db, ids):
    return db.group_invites.find_one({"group_id": ids["group"], "invited_user_id": ids["outsider"], "status": "pending"})


def test_accepting_a_take_over_moves_the_place_and_removes_the_placeholder(secured):
    app, db, ids, password = secured
    managed = add_managed(db, ids["group"], owner=ids["owner"])
    db.plans.update_one({"_id": ids["plan"]}, {"$set": {
        "invited_member_ids": [ids["owner"], managed], "status": "scheduled",
        "scheduled_start": datetime(2027, 1, 4, 9), "scheduled_end": datetime(2027, 1, 4, 9, 30),
        "scheduled_available_member_ids": [ids["owner"], managed], f"rsvps.{managed}": {"status": "attending"}}})
    owner, owner_headers = signed_in(app, db, "owner", password)
    assert invite(owner, owner_headers, ids["group"], managed).status_code == 302
    sent = pending_invite(db, ids)
    assert sent["claims_member_id"] == managed and sent["claims_member_name"] == "Grandma"
    note = db.notifications.find_one({"invite_id": sent["_id"]})
    assert note["metadata"]["claims_member_name"] == "Grandma"
    outsider, outsider_headers = signed_in(app, db, "outsider", password)
    dashboard = outsider.get("/dashboard").get_data(as_text=True)
    assert "to take over <strong>Grandma</strong>" in dashboard and "invited you to join this group as Grandma." in dashboard
    assert outsider.post(f"/invitations/{sent['_id']}/accept", headers=outsider_headers).status_code == 302
    group = db.groups.find_one({"_id": ids["group"]})
    plan = db.plans.find_one({"_id": ids["plan"]})
    assert ids["outsider"] in group["account_ids"] and ids["outsider"] in group["member_ids"] and managed not in group["member_ids"]
    assert plan["invited_member_ids"] == [ids["owner"], ids["outsider"]]
    assert plan["scheduled_available_member_ids"] == [ids["owner"], ids["outsider"]]
    assert plan["rsvps"][str(ids["outsider"])] == {"status": "attending"} and str(managed) not in plan["rsvps"]
    assert db.users.find_one({"_id": managed}) is None and db.availability.count_documents({"user_id": managed}) == 0
    # The person's own availability is untouched.
    assert db.users.find_one({"_id": ids["outsider"]})


def test_declining_leaves_the_member_without_an_account_alone(secured):
    app, db, ids, password = secured
    managed = add_managed(db, ids["group"], owner=ids["owner"])
    owner, owner_headers = signed_in(app, db, "owner", password)
    invite(owner, owner_headers, ids["group"], managed)
    sent = pending_invite(db, ids)
    outsider, outsider_headers = signed_in(app, db, "outsider", password)
    assert outsider.post(f"/invitations/{sent['_id']}/decline", headers=outsider_headers).status_code == 302
    assert db.users.find_one({"_id": managed}) and managed in db.groups.find_one({"_id": ids["group"]})["member_ids"]
    assert db.availability.count_documents({"user_id": managed}) == 1
    assert ids["outsider"] not in db.groups.find_one({"_id": ids["group"]})["account_ids"]


@pytest.mark.parametrize("target", ["linked_member", "other_group", "unknown"])
def test_only_this_groups_members_without_accounts_can_be_claimed(secured, target):
    app, db, ids, password = secured
    other_group = ObjectId()
    db.groups.insert_one({"_id": other_group, "name": "Other", "owner_account_id": ids["owner"],
                          "account_ids": [ids["owner"]], "member_ids": [ids["owner"]]})
    claim = {"linked_member": ids["member"], "other_group": add_managed(db, other_group, owner=ids["owner"]),
             "unknown": ObjectId()}[target]
    owner, owner_headers = signed_in(app, db, "owner", password)
    before = copy.deepcopy(snapshot(db))
    assert invite(owner, owner_headers, ids["group"], claim).status_code == 404
    assert snapshot(db) == before


def test_one_pending_take_over_per_member(secured):
    app, db, ids, password = secured
    managed = add_managed(db, ids["group"], owner=ids["owner"])
    owner, owner_headers = signed_in(app, db, "owner", password)
    invite(owner, owner_headers, ids["group"], managed)
    db.accounts.insert_one({"_id": ObjectId(), "username": "audit-second", "normalized_username": "audit-second"})
    response = invite(owner, owner_headers, ids["group"], managed, username="audit-second", json=True)
    assert response.status_code == 409 and "already has a pending invitation" in response.get_json()["error"]
    assert db.group_invites.count_documents({"claims_member_id": managed}) == 1


def test_take_over_invitations_need_manage_members(secured):
    app, db, ids, password = secured
    managed = add_managed(db, ids["group"], owner=ids["owner"])
    admin, admin_headers = signed_in(app, db, "admin", password)  # fixture admin has only edit_member_availability
    before = copy.deepcopy(snapshot(db))
    assert invite(admin, admin_headers, ids["group"], managed).status_code == 403
    assert snapshot(db) == before


def test_another_account_cannot_accept_or_replay_a_take_over(secured):
    app, db, ids, password = secured
    managed = add_managed(db, ids["group"], owner=ids["owner"])
    owner, owner_headers = signed_in(app, db, "owner", password)
    invite(owner, owner_headers, ids["group"], managed)
    sent = pending_invite(db, ids)
    member, member_headers = signed_in(app, db, "member", password)
    assert member.post(f"/invitations/{sent['_id']}/accept", headers=member_headers).status_code == 404
    assert db.users.find_one({"_id": managed})
    outsider, outsider_headers = signed_in(app, db, "outsider", password)
    outsider.post(f"/invitations/{sent['_id']}/accept", headers=outsider_headers)
    after_first = copy.deepcopy(snapshot(db))
    outsider.post(f"/invitations/{sent['_id']}/accept", headers=outsider_headers)
    assert snapshot(db) == after_first


def test_removing_the_member_cancels_its_pending_take_over(secured):
    app, db, ids, password = secured
    managed = add_managed(db, ids["group"], owner=ids["owner"])
    owner, owner_headers = signed_in(app, db, "owner", password)
    invite(owner, owner_headers, ids["group"], managed)
    sent = pending_invite(db, ids)
    assert owner.post(f"/groups/{ids['group']}/members/{managed}/remove", headers=owner_headers).status_code == 302
    assert db.group_invites.find_one({"_id": sent["_id"]})["status"] == "revoked"
    assert db.notifications.count_documents({"invite_id": sent["_id"]}) == 0


def test_plain_invitations_are_unchanged(secured):
    app, db, ids, password = secured
    managed = add_managed(db, ids["group"], owner=ids["owner"])
    owner, owner_headers = signed_in(app, db, "owner", password)
    invite(owner, owner_headers, ids["group"])
    sent = pending_invite(db, ids)
    assert "claims_member_id" not in sent
    outsider, outsider_headers = signed_in(app, db, "outsider", password)
    outsider.post(f"/invitations/{sent['_id']}/accept", headers=outsider_headers)
    assert db.users.find_one({"_id": managed}) and managed in db.groups.find_one({"_id": ids["group"]})["member_ids"]


def test_invite_dialog_offers_members_without_accounts(secured):
    app, db, ids, password = secured
    owner, _headers = signed_in(app, db, "owner", password)
    assert 'name="claims_member_id"' not in owner.get(f"/groups/{ids['group']}").get_data(as_text=True)
    managed = add_managed(db, ids["group"], owner=ids["owner"])
    page = owner.get(f"/groups/{ids['group']}").get_data(as_text=True)
    assert f'<option value="{managed}">Grandma (no account)</option>' in page
    script = owner.get("/static/js/app.js").get_data(as_text=True)
    assert "claims_member_id: input.closest(\"form\").querySelector" in script
