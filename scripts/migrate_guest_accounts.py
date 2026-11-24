"""Migrate legacy guest# member profiles into authenticated accounts.

This migration is explicit and idempotent. It is never imported or run by the
Flask application during startup.
"""

import argparse
import getpass
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.password_policy import password_error

GUEST_PATTERN = re.compile(r"^guest[1-9]\d*$", re.IGNORECASE)
MIGRATION_SOURCE = "legacy_guest"


def utc_now():
    return datetime.now(timezone.utc)


def legacy_guests(database):
    guests = []
    for member in database.users.find({}):
        name = str(member.get("display_name", "")).strip()
        if GUEST_PATTERN.fullmatch(name):
            guests.append(member)
    return sorted(guests, key=lambda item: item["display_name"].casefold())


def migration_account(database, member):
    account = database.accounts.find_one({"_id": member["linked_account_id"]}) if member.get("linked_account_id") else None
    account = account or database.accounts.find_one({"legacy_member_id": member["_id"], "migration_source": MIGRATION_SOURCE})
    if account:
        return account, False
    owner = database.accounts.find_one({"normalized_username": member["display_name"].strip().casefold()})
    conflict = bool(owner and (owner.get("legacy_member_id") != member["_id"] or owner.get("migration_source") != MIGRATION_SOURCE))
    return owner, conflict


def migrate_guest_accounts(database, *, dry_run=False, output=print, password_provider=None):
    guests = legacy_guests(database)
    # Collect and validate every new account's credential before any writes.
    # Existing accounts are never reset or assigned a replacement password.
    password_hashes, used_passwords = {}, set()
    if not dry_run:
        for member in guests:
            account, conflict = migration_account(database, member)
            if account or conflict:
                continue
            if password_provider is None:
                raise ValueError("New accounts require individually supplied temporary passwords")
            username = member["display_name"].strip()
            password = password_provider(username)
            error = password_error(password, username=username) if isinstance(password, str) else "A password is required."
            if error or password.casefold() in used_passwords:
                raise ValueError("Each new account requires a distinct password that passes the password policy")
            used_passwords.add(password.casefold())
            password_hashes[username.casefold()] = generate_password_hash(password)
        used_passwords.clear()
    legacy_groups = list(
        database.groups.find(
            {
                "$or": [
                    {"owner_account_id": {"$exists": False}},
                    {"legacy_guest_migrated": True},
                ]
            }
        )
    )
    report = {
        "found_guests": len(guests),
        "created_accounts": 0,
        "linked_members": 0,
        "membership_links": 0,
        "ownership_assignments": 0,
        "conflicts": [],
        "availability_records": database.availability.count_documents(
            {"user_id": {"$in": [guest["_id"] for guest in guests]}}
        ),
    }
    output(f"Found {len(guests)} legacy guest member(s).")
    if not guests:
        return report

    account_by_member = {}
    for member in guests:
        username = member["display_name"].strip()
        normalized_username = username.casefold()
        account, conflict = migration_account(database, member)
        if conflict:
            report["conflicts"].append(username)
            output(f"Skipped conflicting username: {username}")
            continue

        if not account:
            report["created_accounts"] += 1
            output(f"{'Would create' if dry_run else 'Created'} account: {username}")
            if not dry_run:
                now = utc_now()
                document = {
                    "username": username,
                    "normalized_username": normalized_username,
                    "password_hash": password_hashes[normalized_username],
                    "auth_methods": ["password"],
                    "must_change_password": True,
                    "temporary_password_expires_at": now + timedelta(hours=24),
                    "migration_source": MIGRATION_SOURCE,
                    "legacy_member_id": member["_id"],
                    "created_at": now,
                    "updated_at": now,
                }
                result = database.accounts.insert_one(document)
                document["_id"] = result.inserted_id
                account = document

        needs_link = (
            account
            and (
                member.get("linked_account_id") != account["_id"]
                or member.get("account_id") != account["_id"]
            )
        )
        if needs_link or (dry_run and not account):
            report["linked_members"] += 1
            output(f"{'Would link' if dry_run else 'Linked'} member: {username}")
            if not dry_run:
                database.users.update_one(
                    {"_id": member["_id"]},
                    {
                        "$set": {
                            "account_id": account["_id"],
                            "linked_account_id": account["_id"],
                            "updated_at": utc_now(),
                        }
                    },
                )
        if account:
            account_by_member[member["_id"]] = account

    for group in legacy_groups:
        for member in guests:
            if member["_id"] not in group.get("member_ids", []):
                continue
            account = account_by_member.get(member["_id"])
            if not account:
                if dry_run and member["display_name"].casefold() not in {
                    name.casefold() for name in report["conflicts"]
                }:
                    report["membership_links"] += 1
                continue
            desired_role = (
                "owner" if group.get("owner_account_id") == account["_id"] else "member"
            )
            if (
                account["_id"] not in group.get("account_ids", [])
                or group.get("account_roles", {}).get(str(account["_id"])) != desired_role
            ):
                report["membership_links"] += 1
                if not dry_run:
                    database.groups.update_one(
                        {"_id": group["_id"]},
                        {
                            "$addToSet": {"account_ids": account["_id"]},
                            "$set": {f"account_roles.{account['_id']}": desired_role},
                        },
                    )

    guest1 = next(
        (member for member in guests if member["display_name"].casefold() == "guest1"),
        None,
    )
    guest1_account = account_by_member.get(guest1["_id"]) if guest1 else None
    if guest1:
        for group in legacy_groups:
            if guest1_account and group.get("owner_account_id") == guest1_account["_id"]:
                continue
            report["ownership_assignments"] += 1
            if not dry_run and guest1_account:
                database.groups.update_one(
                    {"_id": group["_id"]},
                    {
                        "$set": {
                            "owner_account_id": guest1_account["_id"],
                            f"account_roles.{guest1_account['_id']}": "owner",
                            "legacy_guest_migrated": True,
                        },
                        "$addToSet": {
                            "account_ids": guest1_account["_id"],
                            "member_ids": guest1["_id"],
                        },
                    },
                )

    action = "Would preserve" if dry_run else "Preserved"
    output(f"{action} {report['availability_records']} availability record(s).")
    output(
        f"{'Would assign' if dry_run else 'Assigned'} guest1 ownership of "
        f"{report['ownership_assignments']} legacy group(s)."
    )
    if report["conflicts"]:
        output(f"Skipped {len(report['conflicts'])} unsafe username conflict(s).")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
    parser.add_argument("--database-name", help="override MONGO_DB_NAME")
    args = parser.parse_args(argv)
    load_dotenv()
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        print("MONGO_URI is not configured.", file=sys.stderr)
        return 2
    database_name = args.database_name or os.getenv("MONGO_DB_NAME", "cs4800_database")
    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=10000)
        client.admin.command("ping")
        def prompt_password(username):
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                raise ValueError("Use an interactive terminal for hidden password entry")
            password = getpass.getpass(f"Temporary password for {username}: ")
            if password != getpass.getpass("Confirm temporary password: "):
                raise ValueError("Temporary passwords did not match")
            return password
        migrate_guest_accounts(client[database_name], dry_run=args.dry_run, password_provider=prompt_password)
    except ValueError as exc:
        print(f"Migration stopped before creating new accounts: {exc}", file=sys.stderr)
        return 1
    except PyMongoError as exc:
        print(
            f"Migration could not connect to or update MongoDB ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
