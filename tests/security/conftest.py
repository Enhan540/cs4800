import os

import mongomock
import pytest
from bson import ObjectId
from werkzeug.security import generate_password_hash

os.environ["MONGO_URI"] = ""
from app import create_app


def seeded_app(database=None):
    db = database if database is not None else mongomock.MongoClient().security_tests
    app = create_app(database_override=db)
    app.config.update(TESTING=True, SECRET_KEY=os.urandom(32).hex(), LOGIN_DISABLED=False, EMAIL_DELIVERY_MODE="console")
    ids = {name: ObjectId() for name in ("owner", "outsider", "admin", "member", "group", "plan", "window", "connection", "invite", "notification", "outsider_notification")}
    password = os.environ.get("SECURITY_FIXTURE_PASSWORD") or os.urandom(24).hex()
    fixture_hash = generate_password_hash(password, method="pbkdf2:sha256:1000")
    for name in ("owner", "outsider", "admin", "member"):
        db.accounts.insert_one({"_id": ids[name], "username": f"audit-{name}", "normalized_username": f"audit-{name}", "password_hash": fixture_hash, "auth_methods": ["password"]})
        db.users.insert_one({"_id": ids[name], "account_id": ids[name], "linked_account_id": ids[name], "display_name": f"audit-{name}", "normalized_name": f"audit-{name}"})
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"email": "owner@example.invalid", "normalized_email": "owner@example.invalid", "email_verified": True}})
    db.groups.insert_one({"_id": ids["group"], "name": "Private audit group", "owner_account_id": ids["owner"], "account_ids": [ids[n] for n in ("owner", "admin", "member")], "member_ids": [ids[n] for n in ("owner", "admin", "member")], "account_roles": {str(ids["admin"]): "admin", str(ids["member"]): "member"}, "admin_permissions": {str(ids["admin"]): ["edit_member_availability"]}})
    db.availability.insert_one({"_id": ids["window"], "user_id": ids["owner"], "day": "Monday", "day_order": 0, "start_time": "09:00", "end_time": "10:00", "status": "available"})
    db.plans.insert_one({"_id": ids["plan"], "group_id": ids["group"], "title": "Private audit plan", "status": "planning", "start_date": "2027-01-04", "end_date": "2027-01-04", "duration_minutes": 30, "minimum_attendance": 1, "allowed_start_time": "09:00", "allowed_end_time": "17:00", "matches": [], "invited_member_ids": [ids["owner"], ids["member"]]})
    db.calendar_connections.insert_one({"_id": ids["connection"], "account_id": ids["owner"], "provider": "ical", "source_key": "synthetic", "display_name": "PRIVATE-CALENDAR-CANARY", "busy_intervals": [], "sync_status": "error"})
    db.group_invites.insert_one({"_id": ids["invite"], "group_id": ids["group"], "invited_user_id": ids["member"], "invited_by_user_id": ids["owner"], "status": "pending"})
    from app import utc_now
    for name, recipient in (("notification", "owner"), ("outsider_notification", "outsider")):
        db.notifications.insert_one({"_id": ids[name], "recipient_user_id": ids[recipient], "type": "group_invite", "status": "unresolved", "created_at": utc_now(), "metadata": {"group_name": f"PRIVATE-{recipient}-NOTIFICATION"}})
    app.config["SECURITY_FIXTURE_IDS"] = ids
    return app, db, ids, password


@pytest.fixture
def secured():
    return seeded_app()


def token(client):
    client.get("/login")
    with client.session_transaction() as state:
        return state["csrf_token"]


def login(client, name, password):
    result = client.post("/login", data={"username": f"audit-{name}", "password": password, "csrf_token": token(client)})
    assert result.status_code == 302
    with client.session_transaction() as state:
        return state["csrf_token"]
