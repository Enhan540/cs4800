"""Database-backed user notifications."""

from datetime import datetime, timezone

from pymongo import DESCENDING


GROUP_INVITE = "GROUP_INVITE"
PLAN_SCHEDULED = "PLAN_SCHEDULED"
PLAN_UPDATED = "PLAN_UPDATED"
INVITATION_TYPES = {GROUP_INVITE, "group_invite"}
PLAN_TYPES = {PLAN_SCHEDULED, PLAN_UPDATED, "plan_scheduled", "plan_updated"}


def utc_now():
    return datetime.now(timezone.utc)


def create_notification(db, recipient_user_id, notification_type, *, group_id=None,
                        plan_id=None, invite_id=None, actor_user_id=None,
                        metadata=None):
    """Create one unresolved notification, avoiding duplicate actionable copies."""
    if not recipient_user_id:
        return None
    if notification_type in PLAN_TYPES and (group_id is None or plan_id is None):
        return None
    if group_id is not None and notification_type not in INVITATION_TYPES:
        if not db.groups.find_one({"_id": group_id, "$or": [{"owner_account_id": recipient_user_id}, {"account_ids": recipient_user_id}]}, {"_id": 1}):
            return None
    if notification_type in PLAN_TYPES and not db.plans.find_one({"_id": plan_id, "group_id": group_id}, {"_id": 1}):
        return None
    query = {
        "recipient_user_id": recipient_user_id,
        "type": notification_type,
        "status": "unresolved",
    }
    for key, value in (("group_id", group_id), ("plan_id", plan_id),
                       ("invite_id", invite_id)):
        if value is not None:
            query[key] = value
    existing = db.notifications.find_one(query)
    if existing:
        return existing
    document = {
        **query,
        "created_at": utc_now(),
        "read_at": None,
        "actor_user_id": actor_user_id,
        "metadata": metadata or {},
    }
    try:
        result = db.notifications.insert_one(document)
    except Exception:
        existing = db.notifications.find_one(query)
        if existing:
            return existing
        raise
    document["_id"] = result.inserted_id
    return document


def visible_notification_query(db, recipient_user_id):
    """Own inbox records still require access to any private group context."""
    if not recipient_user_id:
        return {"_id": {"$in": []}}
    groups = [group["_id"] for group in db.groups.find({"$or": [{"owner_account_id": recipient_user_id}, {"account_ids": recipient_user_id}]}, {"_id": 1})]
    return {
        "recipient_user_id": recipient_user_id,
        "status": {"$ne": "resolved"},
        "$or": [
            {"type": {"$in": sorted(INVITATION_TYPES)}},
            {"group_id": {"$in": groups}},
            {"group_id": None, "type": {"$nin": sorted(PLAN_TYPES)}},
        ],
    }


def get_user_notifications(db, recipient_user_id, limit=20):
    return list(db.notifications.find(visible_notification_query(db, recipient_user_id)).sort("created_at", DESCENDING).limit(limit))


def latest_user_notification(db, recipient_user_id):
    return db.notifications.find_one(visible_notification_query(db, recipient_user_id), sort=[("created_at", DESCENDING)])


def unread_count(db, recipient_user_id):
    return db.notifications.count_documents({**visible_notification_query(db, recipient_user_id), "read_at": None})


def mark_read(db, notification_id, recipient_user_id):
    return db.notifications.update_one(
        {"_id": notification_id, "recipient_user_id": recipient_user_id},
        {"$set": {"read_at": utc_now()}},
    )


def resolve_notification(db, notification_id, recipient_user_id):
    return db.notifications.update_one(
        {"_id": notification_id, "recipient_user_id": recipient_user_id},
        {"$set": {"status": "resolved", "read_at": utc_now()}},
    )
