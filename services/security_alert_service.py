"""Durable security-change alerts without passwords, bearer links, or MFA data."""

from datetime import datetime, timedelta, timezone
import re

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError
import requests

from services.email_service import send_security_change_email

EVENTS = {
    "password_changed": "Your password was changed",
    "password_reset": "Your password was reset",
    "password_added": "A password was added to your account",
    "email_changed": "Your recovery email was changed",
    "username_changed": "Your sign-in username was changed",
    "google_connected": "Google sign-in was connected",
    "google_disconnected": "Google sign-in was disconnected",
    "mfa_enabled": "Authenticator verification was enabled",
    "mfa_disabled": "Authenticator verification was disabled",
    "mfa_recovery_regenerated": "Your authenticator recovery codes were replaced",
    "mfa_recovery_used": "An authenticator recovery code was used",
}


def now():
    return datetime.now(timezone.utc)


def verified_recipient(account):
    for address, verified in ((account.get("email"), account.get("email_verified")), (account.get("google_email"), account.get("google_email_verified") if account.get("google_sub") else False)):
        address = str(address or "").strip().casefold()
        if verified is True and len(address) <= 254 and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address):
            return address
    return None


def commit_change(app, db, account, query, update, event):
    """The marker is atomic with the change; prepared jobs reconcile on retry."""
    if event not in EVENTS:
        raise ValueError("Unsupported security event")
    at = now()
    event_id = ObjectId()
    db.security_alerts.insert_one({"_id": event_id, "account_id": account["_id"], "event": event, "recipient": verified_recipient(account), "created_at": at, "state": "prepared", "next_attempt_at": at, "attempts": 0, "expires_at": at + timedelta(days=30)})
    mutation = {operator: dict(fields) for operator, fields in update.items()}
    mutation.setdefault("$addToSet", {})["security_alert_markers"] = event_id
    updated = db.accounts.find_one_and_update({"$and": [{"_id": account["_id"], "auth_version": account.get("auth_version")}, query]}, mutation, return_document=ReturnDocument.AFTER)
    if updated is None:
        db.security_alerts.delete_one({"_id": event_id, "state": "prepared"})
        return None
    try:
        db.security_alerts.update_one({"_id": event_id, "state": "prepared"}, {"$set": {"state": "pending", "next_attempt_at": now()}})
        db.accounts.update_one({"_id": account["_id"]}, {"$pull": {"security_alert_markers": event_id}})
        deliver_one(app, db, event_id)
    except PyMongoError:
        app.logger.error("Security alert remains queued for retry")
    return updated


def deliver_one(app, db, event_id):
    at = now()
    lease = str(ObjectId())
    job = db.security_alerts.find_one_and_update({"_id": event_id, "state": {"$in": ["prepared", "pending"]}, "next_attempt_at": {"$lte": at}, "$or": [{"lease_until": {"$exists": False}}, {"lease_until": {"$lt": at}}]}, {"$set": {"lease_until": at + timedelta(minutes=2), "lease_token": lease, "next_attempt_at": at + timedelta(minutes=15)}, "$inc": {"attempts": 1}}, return_document=ReturnDocument.AFTER)
    if not job:
        return "deferred"
    claim = {"_id": event_id, "lease_token": lease}
    if job["state"] == "prepared" and not db.accounts.find_one({"_id": job["account_id"], "security_alert_markers": event_id}):
        db.security_alerts.update_one(claim, {"$unset": {"lease_until": "", "lease_token": ""}})
        return "deferred"
    db.security_alerts.update_one(claim, {"$set": {"state": "pending"}})
    db.accounts.update_one({"_id": job["account_id"]}, {"$pull": {"security_alert_markers": event_id}})
    db.notifications.update_one({"_id": event_id}, {"$setOnInsert": {"recipient_user_id": job["account_id"], "type": "SECURITY_CHANGE", "status": "unresolved", "created_at": job["created_at"], "read_at": None, "metadata": {"message": EVENTS[job["event"]] + "."}}}, upsert=True)
    sent = not job.get("recipient")
    try:
        if job.get("recipient"):
            created = job["created_at"]
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            sent = send_security_change_email(app, recipient=job["recipient"], message=EVENTS[job["event"]], changed_at=created.isoformat(), idempotency_key="security-change/" + str(event_id))
    except requests.RequestException:
        app.logger.warning("Security email delivery deferred")
    mutation = {"$unset": {"lease_until": "", "lease_token": ""}}
    if sent:
        mutation["$set"] = {"state": "sent", "sent_at": now()}
    db.security_alerts.update_one(claim, mutation)
    return "sent" if sent else "deferred"


def deliver_due(app, db, *, limit=50):
    jobs = list(db.security_alerts.find({"state": {"$in": ["prepared", "pending"]}, "next_attempt_at": {"$lte": now()}}).sort("next_attempt_at", 1).limit(min(max(int(limit), 1), 50)))
    counts = {"sent": 0, "deferred": 0}
    for job in jobs:
        counts[deliver_one(app, db, job["_id"])] += 1
    return counts
