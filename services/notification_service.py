"""Database-backed user notifications."""

from datetime import datetime, timezone

from pymongo import DESCENDING


GROUP_INVITE = "GROUP_INVITE"
PLAN_SCHEDULED = "PLAN_SCHEDULED"
PLAN_UPDATED = "PLAN_UPDATED"


def utc_now():
    return datetime.now(timezone.utc)


def create_notification(db, recipient_user_id, notification_type, *, group_id=None,
                        plan_id=None, invite_id=None, actor_user_id=None,
                        metadata=None):
    """Create one unresolved notification, avoiding duplicate actionable copies."""
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


def get_user_notifications(db, recipient_user_id, limit=20):
    return list(db.notifications.find({
        "recipient_user_id": recipient_user_id,
        "status": {"$ne": "resolved"},
    }).sort("created_at", DESCENDING).limit(limit))


def unread_count(db, recipient_user_id):
    return db.notifications.count_documents({
        "recipient_user_id": recipient_user_id,
        "status": {"$ne": "resolved"},
        "read_at": None,
    })


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
