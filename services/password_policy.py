"""Offline whole-password screening; accepted passwords are never rewritten."""

import hashlib
from datetime import datetime, timezone

from services.common_passwords import COMMON_PASSWORD_DIGESTS

# Rejection-only fingerprint of a retired migration bootstrap credential.
RETIRED_BOOTSTRAP_DIGEST = "3118c3f121b47857c2ecc177d07020d5e112363cea1d91c18e9a0132528c6ea9"
CONTEXT_WORDS = {"groupplanner", "group planner", "group-planner", "group_planner", "groupplanner123", "groupplanner123!"}


def retired_bootstrap_password(value):
    return hashlib.sha256(value.casefold().encode("utf-8")).hexdigest() == RETIRED_BOOTSTRAP_DIGEST


def legacy_password_setup_pending(document):
    return document.get("migration_source") == "legacy_guest" and document.get("must_change_password") is True


def legacy_setup_proof(document):
    return hashlib.sha256(document.get("password_hash", "").encode("utf-8")).hexdigest()


def legacy_setup_expired(document):
    if not legacy_password_setup_pending(document):
        return False
    expires = document.get("temporary_password_expires_at")
    if not isinstance(expires, datetime):
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)


def password_error(value, *, username="", email=""):
    username, email = str(username or ""), str(email or "")
    if len(value) < 8:
        return "Password must be at least 8 characters."
    if len(value) > 1024:
        return "Password must be at most 1024 characters."
    # Case folding is only for the denylist lookup. Hashing and login use the
    # original password, including spaces and Unicode, without modification.
    digest = hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()
    if digest in COMMON_PASSWORD_DIGESTS or digest == RETIRED_BOOTSTRAP_DIGEST:
        return "That password is too common. Choose a less predictable password or a longer passphrase."
    context = CONTEXT_WORDS | {username.casefold(), email.casefold(), email.split("@", 1)[0].casefold()}
    if value.casefold() in context:
        return "Choose a password different from your username, email, and the app name."
    return None
