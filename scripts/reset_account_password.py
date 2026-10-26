"""Safely reset one existing Group Planner account password once.

Usage:
    python scripts/reset_account_password.py --username 2012MacBook

The password is entered interactively and is never written to disk, MongoDB,
or command output. This script updates only password_hash, plus an existing
must_change_password field when present.
"""

import argparse
import getpass
import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient
from werkzeug.security import check_password_hash, generate_password_hash


def main():
    parser = argparse.ArgumentParser(description="Reset an existing Group Planner password.")
    parser.add_argument("--username", default="2012MacBook")
    args = parser.parse_args()

    if args.username.casefold() != "2012macbook":
        parser.error("This one-time tool is restricted to username 2012MacBook.")

    load_dotenv()
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        print("MONGO_URI is not configured. Set it in your local environment or .env file.", file=sys.stderr)
        return 1

    database_name = os.getenv("MONGO_DB_NAME", "cs4800_database")
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        database = client[database_name]
        account = database.accounts.find_one({"normalized_username": args.username.casefold()})
        if not account:
            print("No account was found for that username.", file=sys.stderr)
            return 1

        password = getpass.getpass("New password: ")
        confirmation = getpass.getpass("Confirm new password: ")
        if len(password) < 8:
            print("Password must be at least 8 characters.", file=sys.stderr)
            return 1
        if password != confirmation:
            print("Passwords do not match.", file=sys.stderr)
            return 1

        update_fields = {"password_hash": generate_password_hash(password)}
        if "must_change_password" in account:
            update_fields["must_change_password"] = False

        result = database.accounts.update_one(
            {"_id": account["_id"]},
            {"$set": update_fields},
        )
        if result.modified_count != 1:
            print("The password was not updated.", file=sys.stderr)
            return 1

        updated = database.accounts.find_one({"_id": account["_id"]})
        if not updated or not check_password_hash(updated.get("password_hash", ""), password):
            print("Password update verification failed.", file=sys.stderr)
            return 1

        print("Password reset completed and verified for the existing account.")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
