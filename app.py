"""CS4800 recurring group scheduling MVP."""

import json
import os
import re
import secrets
import base64
import hashlib
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from authlib.integrations.flask_client import OAuth
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash
from cryptography.fernet import Fernet, InvalidToken
import requests

from services.calendar_service import fetch_calendar_url, normalized_interval, parse_ical_busy_intervals
from services.availability_service import (
    availability_segments_for_date,
    group_availability_blocks,
)
from services.conflict_service import effective_plan_interval, intervals_overlap
from services.notification_service import (
    GROUP_INVITE,
    PLAN_SCHEDULED,
    create_notification,
    get_user_notifications,
    mark_read,
    resolve_notification,
    unread_count,
)
from services.scheduling_service import DAYS, find_matching_times, parse_date, parse_time

load_dotenv()

DAY_ORDER = {day: index for index, day in enumerate(DAYS)}
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,39}$")
ADMIN_PERMISSIONS = {
    "manage_members",
    "edit_group",
    "edit_member_availability",
    "edit_member_rsvp",
    "manage_plans",
}
CALENDAR_FRESHNESS_SECONDS = {
    "view": 5 * 60,
    "scheduling": 60,
}


class Account(UserMixin):
    def __init__(self, document):
        self.document = document
        self.id = str(document["_id"])
        self.username = document["username"]
        self.auth_methods = document.get("auth_methods", [])


def utc_now():
    return datetime.now(timezone.utc)


def object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        abort(404)


def create_app(database_override=None):
    app = Flask(__name__)
    timezone_name = os.getenv("APP_TIMEZONE", "America/Los_Angeles")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError("APP_TIMEZONE must be a valid IANA timezone") from exc
    configured_secret = os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY")
    if os.getenv("VERCEL") and not configured_secret:
        raise RuntimeError("SECRET_KEY must be configured in Vercel")
    app.config["SECRET_KEY"] = configured_secret or "dev-only-change-me"
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "").casefold()
        in {"1", "true", "yes"},
        GOOGLE_CLIENT_ID=os.getenv("GOOGLE_CLIENT_ID", ""),
        GOOGLE_CLIENT_SECRET=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        GOOGLE_REDIRECT_URI=os.getenv("GOOGLE_REDIRECT_URI", ""),
        GOOGLE_CALENDAR_REDIRECT_URI=os.getenv("GOOGLE_CALENDAR_REDIRECT_URI", ""),
        APP_TIMEZONE=timezone_name,
    )

    mongo_uri = os.getenv("MONGO_URI")
    database_name = os.getenv("MONGO_DB_NAME", "cs4800_database")
    database = database_override

    if database is None and mongo_uri:
        try:
            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
            client.admin.command("ping")
            database = client[database_name]
        except PyMongoError:
            app.logger.exception("Unable to connect to MongoDB Atlas during startup")

    if database is not None:
        try:
            user_indexes = database.users.index_information()
            if "normalized_name_1" in user_indexes:
                database.users.drop_index("normalized_name_1")
            if "email_1" in user_indexes:
                database.users.drop_index("email_1")
            scoped_email_index = user_indexes.get("account_id_1_email_1")
            if scoped_email_index and not scoped_email_index.get("partialFilterExpression"):
                database.users.drop_index("account_id_1_email_1")
            database.users.create_index(
                [("account_id", ASCENDING), ("normalized_name", ASCENDING)], unique=True
            )
            database.users.create_index(
                [("account_id", ASCENDING), ("email", ASCENDING)],
                unique=True,
                partialFilterExpression={"email": {"$type": "string"}},
            )
            database.accounts.create_index("normalized_username", unique=True)
            database.accounts.create_index("google_sub", unique=True, sparse=True)
            database.accounts.create_index("google_email", unique=True, sparse=True)
            database.google_oauth_states.create_index("state_hash", unique=True)
            database.google_oauth_states.create_index("expires_at", expireAfterSeconds=0)
            database.google_link_states.create_index("token_hash", unique=True)
            database.google_link_states.create_index("expires_at", expireAfterSeconds=0)
            database.calendar_connections.create_index(
                [("account_id", ASCENDING), ("provider", ASCENDING)], unique=True
            )
            database.availability.create_index(
                [("user_id", ASCENDING), ("day_order", ASCENDING), ("start_time", ASCENDING)]
            )
            database.groups.create_index("created_at")
            database.plans.create_index([("group_id", ASCENDING), ("created_at", DESCENDING)])
            database.group_invites.create_index(
                [("group_id", ASCENDING), ("invited_user_id", ASCENDING)],
                unique=True,
                partialFilterExpression={"status": "pending"},
            )
            database.notifications.create_index(
                [("recipient_user_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)]
            )
        except PyMongoError:
            app.logger.exception("Unable to create MongoDB indexes during startup")

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.login_message = "Please log in to continue."
    login_manager.login_message_category = "error"
    login_manager.init_app(app)

    @app.context_processor
    def notification_context():
        if not current_user.is_authenticated or database is None:
            return {"notifications": [], "notification_unread_count": 0, "sidebar_groups": []}
        try:
            notifications = get_user_notifications(database, current_account_id())
            sidebar_groups = list(database.groups.find(
                account_filter("account_ids")
            ).sort("created_at", DESCENDING).limit(6))
            return {
                "notifications": notifications,
                "notification_unread_count": unread_count(database, current_account_id()),
                "sidebar_groups": sidebar_groups,
            }
        except PyMongoError:
            app.logger.exception("Unable to load notifications")
            return {"notifications": [], "notification_unread_count": 0, "sidebar_groups": []}

    oauth = OAuth(app)
    google = oauth.register(
        name="google",
        client_id=app.config["GOOGLE_CLIENT_ID"],
        client_secret=app.config["GOOGLE_CLIENT_SECRET"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    app.extensions["google_oauth_client"] = google
    google_calendar = oauth.register(
        name="google_calendar",
        client_id=app.config["GOOGLE_CLIENT_ID"],
        client_secret=app.config["GOOGLE_CLIENT_SECRET"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "https://www.googleapis.com/auth/calendar.events.freebusy"},
    )
    app.extensions["google_calendar_oauth_client"] = google_calendar

    @login_manager.user_loader
    def load_account(account_id):
        if database is None:
            return None
        try:
            document = database.accounts.find_one({"_id": ObjectId(account_id)})
        except (InvalidId, TypeError):
            return None
        return Account(document) if document else None

    def require_database():
        if database is None:
            flash("The database is not configured or is currently unavailable.", "error")
            abort(503)
        return database

    def wants_json_response():
        return request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accept_mimetypes.best == "application/json"

    def auth_bypass():
        return bool(app.config.get("LOGIN_DISABLED"))

    def current_account_id():
        if not current_user.is_authenticated:
            return None
        try:
            return ObjectId(current_user.id)
        except (InvalidId, TypeError):
            return None

    def account_filter(field="account_id"):
        return {} if auth_bypass() else {field: current_account_id()}

    def group_is_accessible(group):
        if auth_bypass():
            return True
        account_id = current_account_id()
        return bool(
            account_id
            and (
                group.get("owner_account_id") == account_id
                or account_id in group.get("account_ids", [])
            )
        )

    def group_is_owner(group):
        return auth_bypass() or group.get("owner_account_id") == current_account_id()

    def group_role(group, account_id=None):
        if auth_bypass():
            return "owner"
        account_id = account_id or current_account_id()
        if not account_id:
            return None
        if group.get("owner_account_id") == account_id:
            return "owner"
        return group.get("account_roles", {}).get(str(account_id), "member")

    def has_group_permission(group, permission, account_id=None):
        account_id = account_id or current_account_id()
        if auth_bypass() or group.get("owner_account_id") == account_id:
            return True
        if permission not in ADMIN_PERMISSIONS:
            return False
        return (
            group_role(group, account_id) == "admin"
            and permission
            in group.get("admin_permissions", {}).get(str(account_id), [])
        )

    def require_group_permission(group, permission):
        if not has_group_permission(group, permission):
            abort(403)

    def require_group_owner(group):
        if not group_is_owner(group):
            abort(403)

    def member_is_accessible(member):
        if auth_bypass() or member.get("account_id") == current_account_id():
            return True
        return bool(
            require_database().groups.find_one(
                {
                    "$or": [
                        {"owner_account_id": current_account_id()},
                        {"account_ids": current_account_id()},
                    ],
                    "member_ids": member["_id"],
                },
                {"_id": 1},
            )
        )

    def member_can_manage(member):
        if auth_bypass() or member.get("account_id") == current_account_id():
            return True
        groups = require_database().groups.find({
            "$or": [
                {"owner_account_id": current_account_id()},
                {"account_ids": current_account_id()},
            ],
            "member_ids": member["_id"],
        })
        return any(
            group.get("owner_account_id") == current_account_id()
            or has_group_permission(group, "edit_member_availability")
            for group in groups
        )

    def get_user(user_id):
        user = require_database().users.find_one({"_id": object_id(user_id)})
        if not user or not member_is_accessible(user):
            abort(404)
        return user

    def ensure_account_member():
        db = require_database()
        account_id = current_account_id()
        member = db.users.find_one({"linked_account_id": account_id})
        if member:
            return member
        account = db.accounts.find_one({"_id": account_id})
        if not account:
            abort(404)
        now = utc_now()
        document = {
            "display_name": account["username"],
            "normalized_name": account["username"].casefold(),
            "account_id": account_id,
            "linked_account_id": account_id,
            "created_at": now,
            "updated_at": now,
        }
        try:
            result = db.users.insert_one(document)
        except DuplicateKeyError:
            member = db.users.find_one(
                {"account_id": account_id, "normalized_name": document["normalized_name"]}
            )
            if not member:
                raise
            db.users.update_one(
                {"_id": member["_id"]},
                {"$set": {"linked_account_id": account_id, "updated_at": now}},
            )
            member["linked_account_id"] = account_id
            return member
        document["_id"] = result.inserted_id
        return document

    def get_group(group_id):
        group = require_database().groups.find_one({"_id": object_id(group_id)})
        if not group or not group_is_accessible(group):
            abort(404)
        return group

    def get_plan(plan_id):
        plan = require_database().plans.find_one({"_id": object_id(plan_id)})
        if not plan:
            abort(404)
        group = require_database().groups.find_one({"_id": plan.get("group_id")})
        if not group or not group_is_accessible(group):
            abort(404)
        return plan

    def normalize_selector_time(form, prefix):
        """Turn the three-part availability picker into a stored HH:MM value."""
        hour = form.get(f"{prefix}_hour", "").strip()
        minute = form.get(f"{prefix}_minute", "").strip()
        period = form.get(f"{prefix}_period", "").strip().upper()
        typed_time = form.get(f"{prefix}_time", "").strip()

        if typed_time:
            return normalize_time_text(typed_time)
        if not any((hour, minute, period)):
            return ""
        if not all((hour, minute, period)):
            raise ValueError("Choose an hour, minute, and AM or PM.")
        if hour not in {str(value) for value in range(1, 13)}:
            raise ValueError("Please choose a valid hour.")
        if minute not in {f"{value:02d}" for value in range(0, 60, 5)}:
            raise ValueError("Please choose a valid five-minute interval.")
        if period not in {"AM", "PM"}:
            raise ValueError("Please choose AM or PM.")

        hour_value = int(hour) % 12
        if period == "PM":
            hour_value += 12
        return f"{hour_value:02d}:{int(minute):02d}"

    def normalize_time_text(value):
        """Accept common 12-hour text forms while storing a normalized HH:MM."""
        value = (value or "").strip().upper().replace(" ", "")
        if not value:
            return ""
        match = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(AM|PM)", value)
        if match:
            hour, minute, period = int(match.group(1)), int(match.group(2) or 0), match.group(3)
            if not 1 <= hour <= 12 or minute > 59:
                raise ValueError("Please enter a valid time.")
            if minute % 5:
                raise ValueError("Please enter a time in 5-minute increments.")
            hour = hour % 12 + (12 if period == "PM" else 0)
            return f"{hour:02d}:{minute:02d}"
        try:
            hour, minute = map(int, value.split(":"))
        except ValueError:
            raise ValueError("Please enter a valid time.") from None
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Please enter a valid time.")
        if minute % 5:
            raise ValueError("Please enter a time in 5-minute increments.")
        return f"{hour:02d}:{minute:02d}"

    def validate_availability_values(day, start_time, end_time):
        if not all([day, start_time, end_time]):
            return None, "Day, start time, and end time are required."
        if day not in DAY_ORDER:
            return None, "Please choose a valid day of the week."
        try:
            start_minutes = parse_time(start_time)
            end_minutes = parse_time(end_time)
        except ValueError:
            return None, "Please enter valid start and end times."
        if end_minutes <= start_minutes:
            return None, "End time must be later than start time."
        return {
            "day": day,
            "day_order": DAY_ORDER[day],
            "start_time": start_time,
            "end_time": end_time,
            "status": "available",
        }, None

    def validate_availability(form):
        day = form.get("day", "").strip()
        try:
            start_time = normalize_selector_time(form, "start")
            end_time = normalize_selector_time(form, "end")
        except ValueError as exc:
            return None, str(exc)

        return validate_availability_values(day, start_time, end_time)

    def calendar_busy_by_member(members):
        """Return cached busy intervals strictly scoped to each member account."""
        db = require_database()
        account_by_member = {
            str(member["_id"]): member.get("linked_account_id") or member.get("account_id")
            for member in members
        }
        account_ids = [account_id for account_id in account_by_member.values() if account_id]
        busy_by_account = {}
        if account_ids:
            for connection in db.calendar_connections.find(
                {"account_id": {"$in": account_ids}, "sync_status": "connected"},
                {"account_id": 1, "busy_intervals": 1},
            ):
                busy_by_account.setdefault(connection["account_id"], []).extend(
                    connection.get("busy_intervals", [])
                )
        # A member without a linked account is never assigned another account's
        # cache, even if other members in the same group have calendars.
        return {
            member_id: list(busy_by_account.get(account_id, [])) if account_id else []
            for member_id, account_id in account_by_member.items()
        }

    def availability_return_context():
        """Return only to known in-app parents, never to an external URL."""
        requested = request.values.get("return_to", "").strip()
        parsed = urlsplit(requested)
        if not requested or parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
            return url_for("dashboard"), "Dashboard"
        if parsed.path == url_for("account"):
            return url_for("account"), "Account"
        if parsed.path in {url_for("users"), url_for("members")}:
            return url_for("dashboard"), "Dashboard"

        group_prefix = "/groups/"
        if parsed.path.startswith(group_prefix):
            group_value = parsed.path[len(group_prefix):].split("/", 1)[0]
            try:
                group = require_database().groups.find_one({"_id": ObjectId(group_value)})
            except InvalidId:
                group = None
            if (
                group
                and group_is_accessible(group)
                and parsed.path == url_for("group_detail", group_id=group["_id"])
            ):
                return url_for("group_detail", group_id=group["_id"]), group["name"]
        return url_for("dashboard"), "Dashboard"

    def availability_editor_state():
        selected_day = request.values.get("selected_day", DAYS[0]).strip()
        if selected_day not in DAY_ORDER:
            selected_day = DAYS[0]
        values = {"selected_day": selected_day, "start_time": "09:00", "end_time": "10:00"}
        for field in ("start_time", "end_time"):
            try:
                candidate = normalize_time_text(request.values.get(field, ""))
                if candidate:
                    values[field] = candidate
            except ValueError:
                pass
        return values

    def validate_plan_form(form, group_size, require_title=True):
        values = {
            "title": form.get("title", "").strip(),
            "start_date": form.get("start_date", "").strip(),
            "end_date": form.get("end_date", "").strip(),
            "earliest_time": form.get("earliest_time", "").strip() or None,
            "latest_time": form.get("latest_time", "").strip() or None,
        }
        errors = []
        try:
            values["duration_minutes"] = int(form.get("duration_minutes", "0"))
            values["minimum_attendance"] = int(form.get("minimum_attendance", "0"))
        except (TypeError, ValueError):
            values["duration_minutes"] = 0
            values["minimum_attendance"] = 0

        try:
            parsed_start = parse_date(values["start_date"])
            parsed_end = parse_date(values["end_date"])
            if parsed_end < parsed_start:
                errors.append("End date must be on or after the start date.")
            if (parsed_end - parsed_start).days > 366:
                errors.append("Date range must be one year or less.")
        except (TypeError, ValueError):
            errors.append("Please enter a valid date range.")
        if require_title and not values["title"]:
            errors.append("Plan title is required.")
        if values["duration_minutes"] <= 0:
            errors.append("Meeting duration must be greater than zero.")
        if values["minimum_attendance"] < 1:
            errors.append("Minimum attendance must be at least one.")
        if values["minimum_attendance"] > group_size:
            errors.append("Minimum attendance cannot exceed the group size.")
        try:
            if values["earliest_time"]:
                parse_time(values["earliest_time"])
            if values["latest_time"]:
                parse_time(values["latest_time"])
            if (
                values["earliest_time"]
                and values["latest_time"]
                and parse_time(values["latest_time"]) <= parse_time(values["earliest_time"])
            ):
                errors.append("Latest allowed time must be later than earliest allowed time.")
        except ValueError:
            errors.append("Please enter valid allowed times.")
        return values, errors

    def calculate_matches(group, values, exclude_plan_id=None):
        ensure_group_calendars_fresh(group, "scheduling")
        member_ids = group.get("member_ids", [])
        availability_records = list(
            require_database().availability.find({"user_id": {"$in": member_ids}})
        )
        member_documents = list(
            require_database().users.find({"_id": {"$in": member_ids}}, {"linked_account_id": 1, "account_id": 1})
        )
        account_by_member = {
            str(member["_id"]): member.get("linked_account_id") or member.get("account_id")
            for member in member_documents
        }
        busy_intervals_by_member = calendar_busy_by_member(member_documents)
        account_ids = [account_id for account_id in account_by_member.values() if account_id]
        # Treat existing RSVP-aware Group Planner events like calendar busy time.
        # Pending events are included conservatively; Not Attending is excluded.
        if account_ids:
            account_member_documents = list(
                require_database().users.find(
                    {"$or": [{"linked_account_id": {"$in": account_ids}}, {"account_id": {"$in": account_ids}}]},
                    {"_id": 1, "linked_account_id": 1, "account_id": 1},
                )
            )
            member_ids_by_account = {}
            for document in account_member_documents:
                linked = document.get("linked_account_id") or document.get("account_id")
                if linked:
                    member_ids_by_account.setdefault(linked, []).append(document["_id"])
            all_account_member_ids = [
                member_id for ids in member_ids_by_account.values() for member_id in ids
            ]
            committed_group_ids = [
                item["_id"] for item in require_database().groups.find(
                    {"member_ids": {"$in": all_account_member_ids}}, {"_id": 1}
                )
            ]
            for scheduled_plan in require_database().plans.find({
                "group_id": {"$in": committed_group_ids},
                "status": "scheduled",
                **({"_id": {"$ne": exclude_plan_id}} if exclude_plan_id else {}),
            }):
                scheduled_group = require_database().groups.find_one(
                    {"_id": scheduled_plan["group_id"]}, {"member_ids": 1}
                ) or {}
                scheduled_member_ids = plan_member_ids(scheduled_plan, scheduled_group)
                for account_id, member_ids in member_ids_by_account.items():
                    scheduled_member_id = next(
                        (member_id for member_id in scheduled_member_ids if member_id in member_ids),
                        None,
                    )
                    if not scheduled_member_id:
                        continue
                    interval = effective_plan_interval(scheduled_plan, scheduled_member_id)
                    if interval:
                        for target_member_id in account_by_member:
                            if account_by_member[target_member_id] == account_id:
                                busy_intervals_by_member.setdefault(target_member_id, []).append({
                                    "start": interval["start"],
                                    "end": interval["end"],
                                })
        return find_matching_times(
            member_ids=member_ids,
            availability_records=availability_records,
            start_date=values["start_date"],
            end_date=values["end_date"],
            duration_minutes=values["duration_minutes"],
            earliest_time=values.get("earliest_time"),
            latest_time=values.get("latest_time"),
            minimum_attendance=values["minimum_attendance"],
            max_results=10,
            busy_intervals_by_member=busy_intervals_by_member,
            timezone_name=app.config["APP_TIMEZONE"],
        )

    def match_metadata(matches):
        return {
            "matches": matches,
            "match_type": (
                "exact" if matches and matches[0].get("is_exact")
                else "alternative" if matches
                else "none"
            ),
            "matches_generated_at": utc_now(),
        }

    def refresh_plan_matches(plan, group):
        matches = calculate_matches(group, plan, exclude_plan_id=plan["_id"])
        metadata = match_metadata(matches)
        require_database().plans.update_one(
            {"_id": plan["_id"]}, {"$set": metadata}
        )
        plan.update(metadata)
        return matches

    def has_confirmed_schedule(plan):
        return bool(
            plan.get("status") == "scheduled"
            and plan.get("scheduled_start")
            and plan.get("scheduled_end")
        )

    def as_datetime(value):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return value if isinstance(value, datetime) else None

    def is_upcoming_schedule(plan):
        if not has_confirmed_schedule(plan):
            return False
        scheduled_end = as_datetime(plan.get("scheduled_end"))
        if not scheduled_end:
            return False
        now = utc_now()
        if scheduled_end.tzinfo is None:
            # PyMongo returns BSON datetimes as naive UTC values by default.
            # Normalize them before comparing with the app's aware UTC clock.
            scheduled_end = scheduled_end.replace(tzinfo=timezone.utc)
        else:
            scheduled_end = scheduled_end.astimezone(timezone.utc)
        return scheduled_end >= now

    def plan_member_ids(plan, group):
        invited = plan.get("invited_member_ids")
        return invited if invited is not None else group.get("member_ids", [])

    def get_user_plan_conflicts(plan, member_id):
        """Return authorized, RSVP-aware conflicts for one member on a plan."""
        current_interval = effective_plan_interval(plan, member_id)
        if not current_interval:
            return []
        db = require_database()
        member = db.users.find_one({"_id": member_id})
        if not member:
            return []
        account_id = member.get("linked_account_id") or member.get("account_id")
        if not account_id and not auth_bypass():
            return []
        account_members = list(
            db.users.find(
                {"$or": [{"linked_account_id": account_id}, {"account_id": account_id}]},
                {"_id": 1},
            )
        ) if account_id else []
        account_member_ids = [item["_id"] for item in account_members]
        group_query = {"member_ids": {"$in": account_member_ids}}
        if not auth_bypass():
            group_query = {
                "$and": [
                    group_query,
                    {"$or": [{"owner_account_id": account_id}, {"account_ids": account_id}]},
                ]
            }
        group_ids = [group["_id"] for group in db.groups.find(group_query, {"_id": 1})]
        conflicts = []
        for other in db.plans.find({
            "_id": {"$ne": plan["_id"]},
            "group_id": {"$in": group_ids},
            "status": "scheduled",
        }):
            other_group = db.groups.find_one({"_id": other["group_id"]}, {"member_ids": 1}) or {}
            other_member_id = next(
                (candidate for candidate in plan_member_ids(other, other_group) if candidate in account_member_ids),
                None,
            )
            if not other_member_id:
                continue
            other_interval = effective_plan_interval(other, other_member_id)
            if not other_interval or not intervals_overlap(
                current_interval["start"], current_interval["end"],
                other_interval["start"], other_interval["end"],
            ):
                continue
            conflicts.append({
                "plan": other,
                "interval": other_interval,
                "overlap_start": max(current_interval["start"], other_interval["start"]),
                "overlap_end": min(current_interval["end"], other_interval["end"]),
            })
        conflicts.sort(key=lambda item: item["interval"]["start"])
        return conflicts

    def plan_members(plan, group, members_by_id):
        return [
            members_by_id[member_id]
            for member_id in plan_member_ids(plan, group)
            if member_id in members_by_id
        ]

    def event_time_options(plan, increment_minutes=5):
        start = as_datetime(plan.get("scheduled_start"))
        end = as_datetime(plan.get("scheduled_end"))
        if not start or not end or end <= start:
            return []
        options = []
        current = start + timedelta(minutes=increment_minutes)
        while current < end:
            options.append(current.strftime("%H:%M"))
            current += timedelta(minutes=increment_minutes)
        return options

    def rsvp_time_defaults(plan):
        start = as_datetime(plan.get("scheduled_start"))
        end = as_datetime(plan.get("scheduled_end"))
        if not start or not end or end - start <= timedelta(minutes=10):
            return {"arrival": "", "departure": ""}
        return {
            "arrival": (start + timedelta(minutes=5)).strftime("%H:%M"),
            "departure": (end - timedelta(minutes=5)).strftime("%H:%M"),
        }

    def datetime_sort_value(value):
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                return 0
        if not isinstance(value, datetime):
            return 0
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()

    def rsvp_summary(plan, members):
        responses = plan.get("rsvps", {})
        counts = {
            "attending": 0,
            "arriving_late": 0,
            "leaving_early": 0,
            "arriving_late_leaving_early": 0,
            "not_attending": 0,
            "pending": 0,
            "no_response": 0,
        }
        rows = []
        for member in members:
            response = responses.get(str(member["_id"]), {})
            status = response.get("status", "pending")
            # Read legacy top-level partial statuses as the new Attending +
            # modifier shape so existing saved RSVP documents stay usable.
            arriving_late = response.get("arriving_late", False) or status in {
                "arriving_late", "arriving_late_leaving_early"
            }
            leaving_early = response.get("leaving_early", False) or status in {
                "leaving_early", "arriving_late_leaving_early"
            }
            if status in {"arriving_late", "leaving_early", "arriving_late_leaving_early"}:
                status = "attending"
            if status == "no_response":
                status = "pending"
            if status not in {"attending", "not_attending", "pending"}:
                status = "pending"
            counts[status] += 1
            if status == "attending" and arriving_late and leaving_early:
                counts["arriving_late_leaving_early"] += 1
            elif status == "attending" and arriving_late:
                counts["arriving_late"] += 1
            elif status == "attending" and leaving_early:
                counts["leaving_early"] += 1
            rows.append({
                "member": member,
                "status": status,
                "arriving_late": bool(arriving_late and status == "attending"),
                "leaving_early": bool(leaving_early and status == "attending"),
                "arrival_time": response.get("arrival_time") or (
                    response.get("time") if arriving_late else None
                ),
                "departure_time": response.get("departure_time") or (
                    response.get("time") if leaving_early else None
                ),
            })
        counts["overall_attending"] = counts["attending"]
        counts["pending"] = counts["pending"] + counts["no_response"]
        return {"counts": counts, "rows": rows}

    def clear_scheduled_fields():
        return {
            "scheduled_start": "",
            "scheduled_end": "",
            "scheduled_available_member_ids": "",
            "scheduled_attendance_count": "",
            "scheduled_total_members": "",
            "scheduled_is_shorter": "",
            "scheduled_available_duration_minutes": "",
            "rsvps": "",
        }

    @app.template_filter("time12")
    def time12(value):
        if not value:
            return "—"
        try:
            return datetime.strptime(value, "%H:%M").strftime("%I:%M %p").lstrip("0")
        except ValueError:
            return value

    @app.template_filter("time_input")
    def time_input(value):
        try:
            return datetime.strptime(value, "%H:%M").strftime("%I:%M %p")
        except (TypeError, ValueError):
            return "09:00 AM"

    @app.template_filter("time_parts")
    def time_parts(value):
        """Convert a normalized time into values for the 12-hour editor."""
        try:
            hour_24, minute = map(int, str(value).split(":"))
            if not 0 <= hour_24 <= 23 or minute not in range(60):
                raise ValueError
        except (TypeError, ValueError):
            hour_24, minute = 9, 0
        return {
            "hour": hour_24 % 12 or 12,
            "minute": f"{minute:02d}",
            "period": "PM" if hour_24 >= 12 else "AM",
        }

    @app.template_filter("time_minutes")
    def time_minutes(value):
        try:
            return parse_time(value)
        except ValueError:
            return 0

    @app.template_filter("duration_label")
    def duration_label(minutes):
        minutes = int(minutes or 0)
        hours, remaining = divmod(minutes, 60)
        parts = []
        if hours:
            parts.append(f"{hours} hr")
        if remaining or not parts:
            parts.append(f"{remaining} min")
        return " ".join(parts)

    @app.template_filter("event_duration")
    def event_duration(plan):
        start = as_datetime(plan.get("scheduled_start"))
        end = as_datetime(plan.get("scheduled_end"))
        if not start or not end or end <= start:
            return "—"
        minutes = int((end - start).total_seconds() // 60)
        if minutes % 60 == 0:
            hours = minutes // 60
            return f"{hours} hour" if hours == 1 else f"{hours} hours"
        return duration_label(minutes)

    @app.template_filter("date_long")
    def date_long(value):
        if not value:
            return "—"
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return value.strftime("%A, %B %-d, %Y") if os.name != "nt" else value.strftime("%A, %B %#d, %Y")

    @app.template_filter("datetime_time12")
    def datetime_time12(value):
        if not value:
            return "—"
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return value.strftime("%I:%M %p").lstrip("0")

    @app.template_filter("date_short")
    def date_short(value):
        if not value:
            return "—"
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return value.strftime("%a, %b %d").replace(" 0", " ")

    @app.template_filter("plan_date")
    def plan_date(value):
        if not value:
            return "—"
        return datetime.strptime(value, "%Y-%m-%d").strftime("%b %d").replace(" 0", " ")

    @app.get("/")
    def index():
        if auth_bypass():
            return dashboard()
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return render_template("landing.html")

    def safe_next_url(value):
        parsed = urlsplit(value or "")
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
            return None
        return value

    def calendar_cipher():
        key = base64.urlsafe_b64encode(
            hashlib.sha256(app.config["SECRET_KEY"].encode("utf-8")).digest()
        )
        return Fernet(key)

    def encrypt_calendar_secret(value):
        return calendar_cipher().encrypt(json.dumps(value).encode("utf-8")).decode("ascii")

    def decrypt_calendar_secret(value):
        try:
            return json.loads(calendar_cipher().decrypt(value.encode("ascii")).decode("utf-8"))
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
            raise ValueError("Saved calendar credentials are no longer available.")

    def sync_calendar_connection(connection):
        db = require_database()
        try:
            secret = decrypt_calendar_secret(connection["secret_ciphertext"])
            if connection["provider"] == "ical":
                intervals = parse_ical_busy_intervals(
                    fetch_calendar_url(secret["url"]),
                    default_timezone=ZoneInfo(app.config["APP_TIMEZONE"]),
                )
            elif connection["provider"] == "google":
                token = secret.get("access_token")
                if not token:
                    raise ValueError("Google Calendar authorization needs to be reconnected.")
                now = utc_now()
                response = requests.post(
                    "https://www.googleapis.com/calendar/v3/freeBusy",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "timeMin": now.isoformat(),
                        "timeMax": (now + timedelta(days=366)).isoformat(),
                        "items": [{"id": "primary"}],
                    },
                    timeout=(3.05, 15),
                )
                if response.status_code == 401 and secret.get("refresh_token"):
                    refresh = requests.post(
                        "https://oauth2.googleapis.com/token",
                        data={
                            "client_id": app.config["GOOGLE_CLIENT_ID"],
                            "client_secret": app.config["GOOGLE_CLIENT_SECRET"],
                            "refresh_token": secret["refresh_token"],
                            "grant_type": "refresh_token",
                        },
                        timeout=(3.05, 15),
                    )
                    refresh.raise_for_status()
                    token = refresh.json().get("access_token")
                    if not token:
                        raise ValueError("Google Calendar authorization needs to be reconnected.")
                    secret["access_token"] = token
                    db.calendar_connections.update_one(
                        {"_id": connection["_id"]},
                        {"$set": {"secret_ciphertext": encrypt_calendar_secret(secret)}},
                    )
                    response = requests.post(
                        "https://www.googleapis.com/calendar/v3/freeBusy",
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "timeMin": now.isoformat(),
                            "timeMax": (now + timedelta(days=366)).isoformat(),
                            "items": [{"id": "primary"}],
                        },
                        timeout=(3.05, 15),
                    )
                response.raise_for_status()
                intervals = [
                    normalized_interval(item["start"], item["end"])
                    for item in response.json().get("calendars", {}).get("primary", {}).get("busy", [])
                ]
            else:
                raise ValueError("Unsupported calendar provider.")
            db.calendar_connections.update_one(
                {"_id": connection["_id"]},
                {"$set": {
                    "busy_intervals": intervals,
                    "last_synced_at": utc_now(),
                    "sync_status": "connected",
                    "sync_error": None,
                }},
            )
            return True
        except Exception:
            app.logger.exception("Calendar sync failed")
            db.calendar_connections.update_one(
                {"_id": connection["_id"]},
                {"$set": {"sync_status": "error", "sync_error": "Calendar could not be synced."}},
            )
            return False

    def ensure_calendar_fresh(account_id, mode="view", force=False):
        """Refresh an account calendar only when its cached data is stale."""
        if not account_id:
            return True
        db = require_database()
        connection = db.calendar_connections.find_one({
            "account_id": account_id,
            "provider": {"$in": ["ical", "google"]},
        })
        if not connection:
            return True
        threshold = timedelta(seconds=CALENDAR_FRESHNESS_SECONDS.get(
            mode, CALENDAR_FRESHNESS_SECONDS["view"]
        ))
        last_synced = as_datetime(connection.get("last_synced_at"))
        if (
            not force
            and last_synced
            and utc_now() - last_synced <= threshold
            and connection.get("sync_status") == "connected"
        ):
            return True

        # A MongoDB lease avoids duplicate fetches across serverless instances.
        lease = db.calendar_connections.update_one(
            {"_id": connection["_id"], "sync_in_progress": {"$ne": True}},
            {"$set": {"sync_in_progress": True, "sync_started_at": utc_now()}},
        )
        if not lease.modified_count:
            return False
        try:
            return sync_calendar_connection(connection)
        finally:
            db.calendar_connections.update_one(
                {"_id": connection["_id"]},
                {"$unset": {"sync_in_progress": "", "sync_started_at": ""}},
            )

    def ensure_group_calendars_fresh(group, mode="view"):
        db = require_database()
        failures = []
        for member in db.users.find(
            {"_id": {"$in": group.get("member_ids", [])}},
            {"linked_account_id": 1, "account_id": 1},
        ):
            account_id = member.get("linked_account_id") or member.get("account_id")
            if account_id and not ensure_calendar_fresh(account_id, mode):
                failures.append(account_id)
        return failures

    def available_google_username(profile, db):
        # Use the email local-part as a starting suggestion.  The provider's
        # display name is not an identity and must not silently become a
        # Group Planner username.
        raw_name = str(profile.get("email", "")).split("@", 1)[0]
        base = re.sub(r"[^A-Za-z0-9_-]+", "-", raw_name).strip("-_")[:40]
        if not base or not base[0].isalnum():
            base = "user"
        if len(base) < 3:
            base = f"{base}-user"[:40]
        candidate = base
        suffix = 2
        while db.accounts.find_one({"normalized_username": candidate.casefold()}):
            suffix_text = f"-{suffix}"
            candidate = f"{base[:40 - len(suffix_text)]}{suffix_text}"
            suffix += 1
        return candidate

    def google_claims(profile):
        """Return the minimal validated Google identity used by this app."""
        if not isinstance(profile, dict):
            return None
        issuer = profile.get("iss")
        if issuer and issuer not in {"https://accounts.google.com", "accounts.google.com"}:
            return None
        subject = str(profile.get("sub", "")).strip()
        email = str(profile.get("email", "")).strip().casefold()
        if not subject or not email or profile.get("email_verified") is not True:
            return None
        return {
            "sub": subject,
            "email": email,
            "email_verified": True,
            "name": str(profile.get("name", "")).strip(),
        }

    def google_claims_match_client(profile):
        audience = profile.get("aud") if isinstance(profile, dict) else None
        if audience and app.config["GOOGLE_CLIENT_ID"]:
            audiences = audience if isinstance(audience, list) else [audience]
            if app.config["GOOGLE_CLIENT_ID"] not in audiences:
                return False
        return True

    def google_state(flow, account_id=None):
        state = secrets.token_urlsafe(32)
        require_database().google_oauth_states.insert_one({
            "state_hash": hashlib.sha256(state.encode()).hexdigest(),
            "flow": flow,
            "account_id": account_id,
            "created_at": utc_now(),
            "expires_at": utc_now() + timedelta(minutes=10),
        })
        session["google_oauth_state"] = state
        return state

    def pop_google_state(state):
        if not state:
            return None
        db = require_database()
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        record = db.google_oauth_states.find_one({"state_hash": state_hash})
        db.google_oauth_states.delete_one({"state_hash": state_hash})
        expires_at = record.get("expires_at") if record else None
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not record or (expires_at and expires_at < utc_now()):
            return None
        return record

    def account_email_conflict(db, claims, exclude_id=None):
        query = {"$or": [
            {"google_sub": claims["sub"]},
            {"google_email": claims["email"]},
            {"email": claims["email"]},
            {"normalized_email": claims["email"]},
        ]}
        if exclude_id is not None:
            query = {"$and": [query, {"_id": {"$ne": exclude_id}}]}
        return db.accounts.find_one(query)

    def link_google_identity(db, account_id, claims):
        conflict = account_email_conflict(db, claims, exclude_id=account_id)
        if conflict:
            return False
        account = db.accounts.find_one({"_id": account_id})
        if not account:
            return False
        existing_sub = account.get("google_sub")
        if existing_sub and existing_sub != claims["sub"]:
            return False
        result = db.accounts.update_one(
            {"_id": account_id, "$or": [
                {"google_sub": {"$exists": False}},
                {"google_sub": None},
                {"google_sub": claims["sub"]},
            ]},
            {"$set": {
                "google_sub": claims["sub"],
                "google_email": claims["email"],
                "google_email_verified": True,
                "updated_at": utc_now(),
            }, "$addToSet": {"auth_methods": "google"}},
        )
        return result.modified_count == 1 or existing_sub == claims["sub"]

    def create_pending_google_link(db, claims, account_id):
        token = secrets.token_urlsafe(32)
        db.google_link_states.insert_one({
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "account_id": account_id,
            "claims": claims,
            "created_at": utc_now(),
            "expires_at": utc_now() + timedelta(minutes=10),
        })
        session["pending_google_link_token"] = token

    def pending_google_link(db):
        token = session.get("pending_google_link_token")
        if not token:
            return None
        record = db.google_link_states.find_one({
            "token_hash": hashlib.sha256(token.encode()).hexdigest()
        })
        expires_at = record.get("expires_at") if record else None
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not record or (expires_at and expires_at < utc_now()):
            session.pop("pending_google_link_token", None)
            if record:
                db.google_link_states.delete_one({"_id": record["_id"]})
            return None
        return record

    def finish_pending_google_link(db, account):
        record = pending_google_link(db)
        if not record:
            return False
        if record["account_id"] != account["_id"]:
            return None
        linked = link_google_identity(db, account["_id"], record["claims"])
        if linked:
            db.google_link_states.delete_one({"_id": record["_id"]})
            session.pop("pending_google_link_token", None)
        return linked

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirmation = request.form.get("confirm_password", "")
            errors = []
            if not username:
                errors.append("Username is required.")
            elif not USERNAME_PATTERN.fullmatch(username):
                errors.append(
                    "Username must be 3–40 characters and use only letters, numbers, underscores, or hyphens."
                )
            if len(password) < 8:
                errors.append("Password must be at least 8 characters.")
            if password != confirmation:
                errors.append("Passwords do not match.")
            db = require_database()
            if not errors and db.accounts.find_one({"normalized_username": username.casefold()}):
                errors.append("Username is already taken.")
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template("register.html", username=username), 400
            now = utc_now()
            document = {
                "username": username,
                "normalized_username": username.casefold(),
                "password_hash": generate_password_hash(password),
                "auth_methods": ["password"],
                "must_change_password": False,
                "created_at": now,
                "updated_at": now,
            }
            try:
                result = db.accounts.insert_one(document)
            except DuplicateKeyError:
                flash("Username is already taken.", "error")
                return render_template("register.html", username=username), 400
            document["_id"] = result.inserted_id
            login_user(Account(document))
            flash("Your account was created.", "success")
            return redirect(url_for("dashboard"))
        return render_template("register.html", username="")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        next_url = safe_next_url(request.values.get("next"))
        db = require_database()
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            account = db.accounts.find_one(
                {"normalized_username": username.casefold()}
            )
            if not account or not account.get("password_hash") or not check_password_hash(
                account["password_hash"], password
            ):
                flash("Invalid username or password.", "error")
                return render_template(
                    "login.html", username=username, next_url=next_url,
                    pending_google_link=bool(pending_google_link(db)),
                ), 401
            link_result = finish_pending_google_link(db, account)
            if link_result is None:
                flash("Sign in with the account that owns the matching email to connect Google.", "error")
                return render_template(
                    "login.html", username=username, next_url=next_url,
                    pending_google_link=True,
                ), 403
            if link_result is False and session.get("pending_google_link_token"):
                flash("Google could not be connected to this account. Please try again.", "error")
                return render_template(
                    "login.html", username=username, next_url=next_url,
                    pending_google_link=True,
                ), 400
            login_user(Account(account))
            return redirect(next_url or url_for("dashboard"))
        return render_template(
            "login.html", username="", next_url=next_url,
            pending_google_link=bool(pending_google_link(db)),
        )

    @app.post("/logout")
    @login_required
    def logout():
        logout_user()
        session.pop("_flashes", None)
        session.pop("google_oauth_state", None)
        flash("You have been logged out.", "success")
        return redirect(url_for("index"))

    @app.get("/auth/google")
    def google_auth():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if not app.config["GOOGLE_CLIENT_ID"] or not app.config["GOOGLE_CLIENT_SECRET"]:
            flash("Google sign-in is not configured for this deployment.", "error")
            return redirect(url_for("login"))
        state = google_state("login")
        redirect_uri = app.config["GOOGLE_REDIRECT_URI"] or url_for(
            "google_callback", _external=True
        )
        return app.extensions["google_oauth_client"].authorize_redirect(
            redirect_uri, state=state
        )

    @app.get("/auth/google/callback")
    def google_callback():
        expected_state = session.pop("google_oauth_state", None)
        received_state = request.args.get("state", "")
        if (
            not expected_state
            or not received_state
            or not secrets.compare_digest(expected_state, received_state)
        ):
            flash("Google sign-in could not be verified. Please try again.", "error")
            return redirect(url_for("login"))
        state_record = pop_google_state(expected_state)
        if not state_record:
            flash("Google sign-in has expired. Please try again.", "error")
            return redirect(url_for("account" if current_user.is_authenticated else "login"))
        if request.args.get("error"):
            flash("Google sign-in was cancelled or denied.", "error")
            return redirect(url_for("login"))
        try:
            token = app.extensions["google_oauth_client"].authorize_access_token()
            profile = token.get("userinfo") or {}
            if not profile:
                profile = app.extensions["google_oauth_client"].get("userinfo").json()
        except Exception:
            app.logger.exception("Google sign-in failed during the OAuth callback")
            flash("Google sign-in failed. Please try again.", "error")
            return redirect(url_for("login"))

        claims = google_claims(profile)
        if not claims or not google_claims_match_client(profile):
            flash("Google did not provide a verified email address.", "error")
            return redirect(url_for("account" if current_user.is_authenticated else "login"))

        db = require_database()
        flow = state_record.get("flow")
        if flow == "connect":
            if not current_user.is_authenticated or str(current_account_id()) != str(state_record.get("account_id")):
                flash("Please remain signed in while connecting Google.", "error")
                return redirect(url_for("login"))
            if account_email_conflict(db, claims, exclude_id=current_account_id()):
                flash("That Google identity is already linked to another account.", "error")
                return redirect(url_for("account"))
            if not link_google_identity(db, current_account_id(), claims):
                flash("Google could not be connected to this account.", "error")
                return redirect(url_for("account"))
            flash("Google connected to your Group Planner account.", "success")
            return redirect(url_for("account"))

        account_document = db.accounts.find_one({"google_sub": claims["sub"]})
        if account_document:
            login_user(Account(account_document))
            return redirect(url_for("dashboard"))

        email_owner = account_email_conflict(db, claims)
        if email_owner:
            if email_owner.get("google_sub"):
                flash("That Google email is already connected to another account.", "error")
                return redirect(url_for("login"))
            create_pending_google_link(db, claims, email_owner["_id"])
            flash("A Group Planner account already uses this email. Sign in to connect Google.", "error")
            return redirect(url_for("login", link_google="1"))

        if current_user.is_authenticated:
            flash("Google could not be connected to this account.", "error")
            return redirect(url_for("account"))
        now = utc_now()
        username = available_google_username(profile, db)
        account_document = {
            "username": username,
            "normalized_username": username.casefold(),
            "google_sub": claims["sub"],
            "google_email": claims["email"],
            "google_email_verified": True,
            "email": claims["email"],
            "email_verified": True,
            "auth_methods": ["google"],
            "must_change_password": False,
            "created_at": now,
            "updated_at": now,
        }
        try:
            result = db.accounts.insert_one(account_document)
        except DuplicateKeyError:
            flash("That Google identity is already connected. Please try again.", "error")
            return redirect(url_for("login"))
        account_document["_id"] = result.inserted_id
        login_user(Account(account_document))
        return redirect(url_for("dashboard"))

    @app.get("/auth/google/connect")
    @login_required
    def google_connect():
        if not app.config["GOOGLE_CLIENT_ID"] or not app.config["GOOGLE_CLIENT_SECRET"]:
            flash("Google sign-in is not configured for this deployment.", "error")
            return redirect(url_for("account"))
        state = google_state("connect", current_account_id())
        redirect_uri = app.config["GOOGLE_REDIRECT_URI"] or url_for(
            "google_callback", _external=True
        )
        return app.extensions["google_oauth_client"].authorize_redirect(
            redirect_uri, state=state
        )

    @app.get("/auth/google-calendar")
    @login_required
    def google_calendar_auth():
        if not app.config["GOOGLE_CLIENT_ID"] or not app.config["GOOGLE_CLIENT_SECRET"]:
            flash("Google Calendar is not configured for this deployment.", "error")
            return redirect(url_for("account"))
        state = secrets.token_urlsafe(32)
        session["google_calendar_oauth_state"] = state
        redirect_uri = app.config["GOOGLE_CALENDAR_REDIRECT_URI"] or url_for(
            "google_calendar_callback", _external=True
        )
        return app.extensions["google_calendar_oauth_client"].authorize_redirect(
            redirect_uri,
            state=state,
            access_type="offline",
            prompt="consent",
        )

    @app.get("/auth/google-calendar/callback")
    @login_required
    def google_calendar_callback():
        expected_state = session.pop("google_calendar_oauth_state", None)
        received_state = request.args.get("state", "")
        if not expected_state or not received_state or not secrets.compare_digest(expected_state, received_state):
            flash("Google Calendar connection could not be verified.", "error")
            return redirect(url_for("account"))
        if request.args.get("error"):
            flash("Google Calendar connection was cancelled or denied.", "error")
            return redirect(url_for("account"))
        try:
            token = app.extensions["google_calendar_oauth_client"].authorize_access_token()
        except Exception:
            app.logger.exception("Google Calendar authorization failed")
            flash("Google Calendar could not be connected.", "error")
            return redirect(url_for("account"))
        secret = {
            key: token[key]
            for key in ("access_token", "refresh_token", "expires_at", "token_type")
            if token.get(key)
        }
        if not secret.get("access_token"):
            flash("Google Calendar did not return usable authorization.", "error")
            return redirect(url_for("account"))
        now = utc_now()
        db = require_database()
        db.calendar_connections.update_one(
            {"account_id": current_account_id(), "provider": "google"},
            {"$set": {
                "account_id": current_account_id(),
                "provider": "google",
                "secret_ciphertext": encrypt_calendar_secret(secret),
                "sync_status": "pending",
                "busy_intervals": [],
                "updated_at": now,
            }, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        connection = db.calendar_connections.find_one(
            {"account_id": current_account_id(), "provider": "google"}
        )
        if sync_calendar_connection(connection):
            flash("Google Calendar connected and synced.", "success")
        else:
            flash("Google Calendar was connected, but could not be synced yet.", "error")
        return redirect(url_for("account"))

    @app.post("/account/calendars/ical")
    @login_required
    def connect_ical_calendar():
        raw_url = request.form.get("calendar_url", "")
        try:
            payload = fetch_calendar_url(raw_url)
            intervals = parse_ical_busy_intervals(
                payload, default_timezone=ZoneInfo(app.config["APP_TIMEZONE"])
            )
        except (ValueError, requests.RequestException):
            flash("That calendar URL could not be safely connected.", "error")
            return redirect(url_for("account"))
        now = utc_now()
        db = require_database()
        db.calendar_connections.update_one(
            {"account_id": current_account_id(), "provider": "ical"},
            {"$set": {
                "account_id": current_account_id(),
                "provider": "ical",
                "secret_ciphertext": encrypt_calendar_secret({"url": raw_url}),
                "busy_intervals": intervals,
                "sync_status": "connected",
                "last_synced_at": now,
                "sync_error": None,
                "updated_at": now,
            }, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        flash("Personal calendar connected and synced.", "success")
        return redirect(url_for("account"))

    @app.post("/account/calendars/<connection_id>/sync")
    @login_required
    def sync_calendar(connection_id):
        connection = require_database().calendar_connections.find_one(
            {"_id": object_id(connection_id), "account_id": current_account_id()}
        )
        if not connection:
            abort(404)
        if sync_calendar_connection(connection):
            flash("Calendar synced.", "success")
        else:
            flash("Calendar could not be synced. Your weekly availability is still being used.", "error")
        return redirect(url_for("account"))

    @app.post("/account/calendars/<connection_id>/disconnect")
    @login_required
    def disconnect_calendar(connection_id):
        result = require_database().calendar_connections.delete_one(
            {"_id": object_id(connection_id), "account_id": current_account_id()}
        )
        if not result.deleted_count:
            abort(404)
        flash("Calendar disconnected. Your weekly availability is unchanged.", "success")
        return redirect(url_for("account"))

    @app.get("/account")
    @login_required
    def account():
        db = require_database()
        document = db.accounts.find_one({"_id": current_account_id()})
        if not document:
            abort(404)
        connections = list(db.calendar_connections.find({"account_id": current_account_id()}))
        return render_template(
            "account.html",
            account=document,
            connections=connections,
        )

    @app.post("/account/google/disconnect")
    @login_required
    def disconnect_google_identity():
        db = require_database()
        account = db.accounts.find_one({"_id": current_account_id()})
        if not account or not account.get("google_sub"):
            flash("Google is not connected to this account.", "error")
            return redirect(url_for("account"))
        if not account.get("password_hash"):
            flash("Set a password before disconnecting your only sign-in method.", "error")
            return redirect(url_for("account"))
        db.accounts.update_one(
            {"_id": account["_id"]},
            {"$unset": {
                "google_sub": "",
                "google_email": "",
                "google_email_verified": "",
            }, "$pull": {"auth_methods": "google"}, "$set": {"updated_at": utc_now()}},
        )
        flash("Google was disconnected. Your password remains active.", "success")
        return redirect(url_for("account"))

    @app.post("/account/password")
    @login_required
    def change_password():
        db = require_database()
        account = db.accounts.find_one({"_id": current_account_id()})
        if not account:
            abort(404)
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirmation = request.form.get("confirm_password", "")
        if not account.get("password_hash"):
            flash("This account currently uses Google Sign-In and does not have a password.", "error")
            return redirect(url_for("account"))
        if not check_password_hash(account["password_hash"], current_password):
            flash("Current password is incorrect.", "error")
            return redirect(url_for("account"))
        if len(new_password) < 8:
            flash("New password must be at least 8 characters.", "error")
            return redirect(url_for("account"))
        if new_password != confirmation:
            flash("New passwords do not match.", "error")
            return redirect(url_for("account"))
        db.accounts.update_one(
            {"_id": account["_id"]},
            {"$set": {
                "password_hash": generate_password_hash(new_password),
                "must_change_password": False,
                "updated_at": utc_now(),
            }, "$addToSet": {"auth_methods": "password"}},
        )
        flash("Password updated successfully.", "success")
        return redirect(url_for("account"))

    @app.get("/account/availability")
    @login_required
    def account_availability():
        member = ensure_account_member()
        return redirect(url_for("member_availability", user_id=member["_id"], return_to=url_for("account")))

    @app.get("/dashboard")
    @login_required
    def dashboard():
        groups = []
        upcoming_plans = []
        pending_invitations = []
        stats = {"groups": 0, "members": 0, "plans": 0}
        if database is not None:
            try:
                group_query = account_filter("account_ids")
                member_query = account_filter()
                group_ids = [
                    group["_id"] for group in database.groups.find(group_query, {"_id": 1})
                ]
                plan_query = {"group_id": {"$in": group_ids}}
                stats = {
                    "groups": database.groups.count_documents(group_query),
                    "members": database.users.count_documents(member_query),
                    "plans": database.plans.count_documents(plan_query),
                }
                groups = list(
                    database.groups.find(group_query).sort("created_at", DESCENDING).limit(5)
                )
                all_plans = list(database.plans.find(plan_query))
                group_documents = {
                    group["_id"]: group
                    for group in database.groups.find(
                        {"_id": {"$in": list({
                            *[plan["group_id"] for plan in all_plans],
                            *[group["_id"] for group in groups],
                        })}}
                    )
                }
                group_names = {
                    group_id: group["name"] for group_id, group in group_documents.items()
                }
                member_ids = {
                    member_id
                    for group in group_documents.values()
                    for member_id in group.get("member_ids", [])
                }
                members_by_id = {
                    member["_id"]: member
                    for member in database.users.find(
                        {"_id": {"$in": list(member_ids)}}
                    )
                }
                for plan in all_plans:
                    plan["group_name"] = group_names.get(plan["group_id"], "Unknown group")
                    group = group_documents.get(plan["group_id"], {})
                    plan["rsvp_summary"] = rsvp_summary(
                        plan, plan_members(plan, group, members_by_id)
                    )
                    if is_upcoming_schedule(plan):
                        upcoming_plans.append(plan)
                upcoming_plans.sort(key=lambda item: datetime_sort_value(item["scheduled_start"]))
                upcoming_counts = {}
                for plan in upcoming_plans:
                    upcoming_counts[plan["group_id"]] = upcoming_counts.get(plan["group_id"], 0) + 1
                for group in groups:
                    group["upcoming_plan_count"] = upcoming_counts.get(group["_id"], 0)
                upcoming_plans = upcoming_plans[:6]
                pending_invitations = list(
                    database.group_invites.find(
                        {"invited_user_id": current_account_id(), "status": "pending"}
                    ).sort("created_at", DESCENDING)
                )
                invitation_group_ids = [invite["group_id"] for invite in pending_invitations]
                invitation_groups = {
                    group["_id"]: group
                    for group in database.groups.find({"_id": {"$in": invitation_group_ids}})
                }
                inviter_ids = [invite["invited_by_user_id"] for invite in pending_invitations]
                inviters = {
                    account["_id"]: account
                    for account in database.accounts.find({"_id": {"$in": inviter_ids}})
                }
                pending_invitations = [
                    {
                        **invite,
                        "group": invitation_groups.get(invite["group_id"]),
                        "inviter": inviters.get(invite["invited_by_user_id"]),
                    }
                    for invite in pending_invitations
                    if invitation_groups.get(invite["group_id"])
                ]
            except PyMongoError:
                app.logger.exception("Unable to load dashboard data")
                flash("Some dashboard data could not be loaded.", "error")
        return render_template(
            "index.html",
            stats=stats,
            groups=groups,
            upcoming_plans=upcoming_plans,
            pending_invitations=pending_invitations,
        )

    @app.route("/users", methods=["GET", "POST"])
    @login_required
    def users():
        db = require_database()
        group_id = request.values.get("group_id", "").strip()
        origin_group = get_group(group_id) if group_id else None
        if request.method == "GET" and not origin_group:
            return redirect(url_for("account"))
        if origin_group:
            # The UI no longer links here. Keep this POST-only compatibility path
            # for old unlinked migration records; authenticated accounts use invites.
            if request.method == "GET":
                return redirect(url_for("group_detail", group_id=origin_group["_id"]))
            require_group_permission(origin_group, "manage_members")
        if request.method == "POST":
            display_name = request.form.get("display_name", "").strip()
            email = request.form.get("email", "").strip().lower()
            if not display_name:
                flash("Display name is required.", "error")
                return redirect(url_for("users"))

            duplicate_queries = [{"normalized_name": display_name.casefold()}]
            if email:
                duplicate_queries.append({"email": email})
            existing_member = db.users.find_one(
                {**account_filter(), "$or": duplicate_queries}
            )
            if existing_member:
                if origin_group:
                    db.groups.update_one(
                        {"_id": origin_group["_id"]},
                        {"$addToSet": {"member_ids": existing_member["_id"]}},
                    )
                    flash(f"{existing_member['display_name']} already existed and was added to the group.", "success")
                    return redirect(url_for("group_detail", group_id=origin_group["_id"]))
                flash("That member already exists. Members can be reused across groups.", "error")
                return redirect(url_for("users"))

            member_document = {
                "display_name": display_name,
                "normalized_name": display_name.casefold(),
                "created_at": utc_now(),
            }
            if not auth_bypass():
                member_document["account_id"] = current_account_id()
            if email:
                member_document["email"] = email
            try:
                result = db.users.insert_one(member_document)
            except DuplicateKeyError:
                flash("That member already exists.", "error")
            else:
                if origin_group:
                    db.groups.update_one(
                        {"_id": origin_group["_id"]},
                        {"$addToSet": {"member_ids": result.inserted_id}},
                    )
                    flash(f"{display_name} was created and added to {origin_group['name']}.", "success")
                    return redirect(url_for("group_detail", group_id=origin_group["_id"]))
                flash(f"{display_name} was added.", "success")
            return redirect(url_for("users"))

        member_list = list(db.users.find(account_filter()).sort("display_name", ASCENDING))
        return render_template("users.html", users=member_list, origin_group=origin_group)

    @app.get("/groups")
    @login_required
    def groups():
        group_list = list(
            require_database().groups.find(account_filter("account_ids")).sort(
                "created_at", DESCENDING
            )
        )
        return render_template("groups.html", groups=group_list)

    @app.get("/members")
    @login_required
    def members():
        return redirect(url_for("users"))

    @app.route("/groups/create", methods=["GET", "POST"])
    @login_required
    def create_group():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            if not name:
                flash("Group name is required.", "error")
                return render_template("group_create.html", form=request.form), 400
            group_document = {
                "name": name,
                "description": description or None,
                "member_ids": [],
                "created_at": utc_now(),
            }
            if not auth_bypass():
                account_id = current_account_id()
                owner_member = ensure_account_member()
                group_document.update({
                    "owner_account_id": account_id,
                    "account_ids": [account_id],
                    "account_roles": {str(account_id): "owner"},
                    "member_ids": [owner_member["_id"]],
                })
            result = require_database().groups.insert_one(group_document)
            flash(f"{name} was created.", "success")
            return redirect(url_for("group_detail", group_id=result.inserted_id))
        return render_template("group_create.html")

    @app.route("/groups/<group_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_group(group_id):
        db = require_database()
        group = get_group(group_id)
        require_group_permission(group, "edit_group")
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            if not name:
                flash("Group name is required.", "error")
                return render_template("group_edit.html", group=group, form=request.form), 400
            db.groups.update_one(
                {"_id": group["_id"]},
                {"$set": {"name": name, "description": description or None}},
            )
            flash("Group updated successfully.", "success")
            return redirect(url_for("group_detail", group_id=group["_id"]))
        return render_template("group_edit.html", group=group, form=group)

    @app.get("/groups/<group_id>")
    @login_required
    def group_detail(group_id):
        db = require_database()
        group = get_group(group_id)
        member_ids = group.get("member_ids", [])
        members = list(
            db.users.find({"_id": {"$in": member_ids}}).sort(
                "display_name", ASCENDING
            )
        )
        pending_invites = list(db.group_invites.find({
            "group_id": group["_id"], "status": "pending"
        }).sort("created_at", ASCENDING))
        pending_account_ids = [invite["invited_user_id"] for invite in pending_invites]
        pending_accounts = {
            account["_id"]: account
            for account in db.accounts.find({"_id": {"$in": pending_account_ids}})
        }
        pending_invitees = [
            {"invite": invite, "account": pending_accounts.get(invite["invited_user_id"])}
            for invite in pending_invites
            if pending_accounts.get(invite["invited_user_id"])
        ]
        members_by_id = {member["_id"]: member for member in members}
        entries = list(
            db.availability.find({"user_id": {"$in": member_ids}}).sort(
                [("day_order", ASCENDING), ("start_time", ASCENDING)]
            )
        )
        try:
            week_start = date.fromisoformat(request.args.get("week_start", ""))
        except ValueError:
            week_start = date.today()
        week_start -= timedelta(days=week_start.weekday())
        week_dates = [week_start + timedelta(days=index) for index in range(7)]
        account_by_member = {
            str(member["_id"]): member.get("linked_account_id") or member.get("account_id")
            for member in members
        }
        busy_by_member = calendar_busy_by_member(members)
        plan_conflicts = {str(member["_id"]): [] for member in members}
        member_group_ids = [
            item["_id"] for item in db.groups.find(
                {"member_ids": {"$in": member_ids}}, {"_id": 1}
            )
        ]
        for scheduled_plan in db.plans.find({"group_id": {"$in": member_group_ids}, "status": "scheduled"}):
            scheduled_group = db.groups.find_one({"_id": scheduled_plan["group_id"]}) or {}
            for member in members:
                if member["_id"] in plan_member_ids(scheduled_plan, scheduled_group):
                    interval = effective_plan_interval(scheduled_plan, member["_id"])
                    if interval:
                        plan_conflicts[str(member["_id"])].append(interval)
        availability_rows = []
        for member in members:
            member_days = {}
            for current in week_dates:
                segments = availability_segments_for_date(
                    entries, member["_id"], current,
                    calendar_busy_intervals=busy_by_member[str(member["_id"])],
                    plan_intervals=plan_conflicts[str(member["_id"])],
                    timezone_name=app.config["APP_TIMEZONE"],
                )
                member_days[current.isoformat()] = [
                    {
                        "start_time": segment["start"].strftime("%H:%M"),
                        "end_time": segment["end"].strftime("%H:%M"),
                        "start_minutes": int((segment["start"] - datetime.combine(current, datetime.min.time())).total_seconds() // 60),
                        "duration_minutes": int((segment["end"] - segment["start"]).total_seconds() // 60),
                        "conflicted": segment["conflicted"],
                        "conflict_sources": segment.get("conflict_sources", []),
                    }
                    for segment in segments
                ]
            availability_rows.append({"member": member, "days": member_days})
        baseline_starts = []
        baseline_ends = []
        member_keys = {str(member["_id"]) for member in members}
        displayed_day_names = {current.strftime("%A") for current in week_dates}
        for entry in entries:
            if str(entry.get("user_id")) not in member_keys or entry.get("day") not in displayed_day_names:
                continue
            try:
                baseline_starts.append(parse_time(entry.get("start_time")))
                baseline_ends.append(parse_time(entry.get("end_time")))
            except (TypeError, ValueError):
                continue
        earliest_baseline = min(baseline_starts, default=10 * 60)
        heatmap_start_minutes = max(0, earliest_baseline - 60)
        heatmap_start_minutes -= heatmap_start_minutes % 30
        latest_baseline = max(baseline_ends, default=23 * 60)
        heatmap_end_minutes = min(24 * 60, ((latest_baseline + 60 + 29) // 30) * 30)
        heatmap_end_minutes = max(heatmap_end_minutes, heatmap_start_minutes + 30)
        heatmap_days = group_availability_blocks(
            entries,
            [member["_id"] for member in members],
            week_dates,
            calendar_busy_by_member=busy_by_member,
            plan_conflicts_by_member=plan_conflicts,
            start_minute=heatmap_start_minutes,
            end_minute=heatmap_end_minutes,
            timezone_name=app.config["APP_TIMEZONE"],
        )
        best_availability = []
        for day in week_dates:
            for block in next(item for item in heatmap_days if item["date"] == day.isoformat())["blocks"]:
                best_availability.append({
                    "date": day,
                    "start_minutes": block["start_minutes"],
                    "end_minutes": block["end_minutes"],
                    "available_count": block["available_count"],
                    "total_count": block["total_members"],
                })
        best_availability.sort(key=lambda item: (
            -item["available_count"],
            -(item["end_minutes"] - item["start_minutes"]),
            item["date"],
            item["start_minutes"],
        ))
        plans = list(db.plans.find({"group_id": group["_id"]}))
        scheduled_plans = []
        planning_plans = []
        for plan in plans:
            if is_upcoming_schedule(plan):
                plan["rsvp_summary"] = rsvp_summary(
                    plan, plan_members(plan, group, members_by_id)
                )
                scheduled_plans.append(plan)
            elif not has_confirmed_schedule(plan):
                planning_plans.append(plan)
        scheduled_plans.sort(key=lambda item: datetime_sort_value(item["scheduled_start"]))
        planning_plans.sort(
            key=lambda item: datetime_sort_value(item.get("created_at")), reverse=True
        )
        return render_template(
            "group_detail.html",
            group=group,
            members=members,
            availability_days=week_dates,
            week_start=week_start,
            week_end=week_dates[-1],
            previous_week=(week_start - timedelta(days=7)).isoformat(),
            next_week=(week_start + timedelta(days=7)).isoformat(),
            availability_rows=availability_rows,
            heatmap_days=heatmap_days,
            has_heatmap_blocks=any(item["blocks"] for item in heatmap_days),
            best_availability=best_availability[:3],
            heatmap_start_minutes=heatmap_start_minutes,
            heatmap_end_minutes=heatmap_end_minutes,
            scheduled_plans=scheduled_plans,
            planning_plans=planning_plans,
            upcoming_plan_count=len(scheduled_plans),
            can_manage_group=group_is_owner(group),
            can_invite=group_is_owner(group) or has_group_permission(group, "manage_members"),
            pending_invitees=pending_invitees,
            can_assign_roles=group_is_owner(group),
            permissions={
                permission: has_group_permission(group, permission)
                for permission in ADMIN_PERMISSIONS
            },
            current_group_role=group_role(group),
            current_member_id=(
                (db.users.find_one({"linked_account_id": current_account_id()}) or {}).get("_id")
                if not auth_bypass()
                else None
            ),
        )

    @app.post("/groups/<group_id>/members")
    @login_required
    def add_group_member(group_id):
        db = require_database()
        group = get_group(group_id)
        require_group_permission(group, "manage_members")
        member_id = object_id(request.form.get("user_id"))
        member = get_user(member_id)
        if member_id in group.get("member_ids", []):
            flash("That member is already in the group.", "error")
        elif member.get("linked_account_id"):
            flash("Use an account invitation to add this user to the group.", "error")
        else:
            # Preserve the ability to keep old, unlinked migration records usable.
            update = {"$addToSet": {"member_ids": member_id}}
            db.groups.update_one({"_id": group["_id"]}, update)
            flash(f"Legacy member {member['display_name']} was added to the group.", "success")
        return redirect(url_for("group_detail", group_id=group_id))

    @app.get("/groups/<group_id>/invitations/search")
    @login_required
    def search_group_invitation_accounts(group_id):
        db = require_database()
        group = get_group(group_id)
        require_group_permission(group, "manage_members")
        query = request.args.get("q", "").strip()
        if len(query) < 2:
            return jsonify({"results": []})
        accounts = list(
            db.accounts.find(
                {
                    "normalized_username": {"$regex": re.escape(query.casefold())},
                    "_id": {"$ne": current_account_id()},
                },
                {"username": 1},
            ).sort("normalized_username", ASCENDING).limit(10)
        )
        member_account_ids = set(group.get("account_ids", []))
        pending_account_ids = {
            invite["invited_user_id"]
            for invite in db.group_invites.find(
                {"group_id": group["_id"], "status": "pending"},
                {"invited_user_id": 1},
            )
        }
        return jsonify({
            "results": [
                {
                    "id": str(account["_id"]),
                    "username": account["username"],
                    "state": (
                        "member" if account["_id"] in member_account_ids
                        else "pending" if account["_id"] in pending_account_ids
                        else "available"
                    ),
                }
                for account in accounts
            ]
        })

    @app.post("/groups/<group_id>/invitations")
    @login_required
    def invite_group_account(group_id):
        db = require_database()
        group = get_group(group_id)
        require_group_permission(group, "manage_members")
        username = request.form.get("username", "").strip()
        account = db.accounts.find_one({"normalized_username": username.casefold()})
        if not account:
            if wants_json_response():
                return jsonify({"ok": False, "error": "No account was found for that username."}), 404
            flash("No account was found for that username.", "error")
            return redirect(url_for("group_detail", group_id=group_id))
        account_id = account["_id"]
        response_state = "available"
        if account_id == current_account_id():
            response_state = "member"
            flash("You are already in this group.", "error")
        elif account_id in group.get("account_ids", []):
            response_state = "member"
            flash("This user is already a member of the group.", "error")
        elif db.group_invites.find_one(
            {"group_id": group["_id"], "invited_user_id": account_id, "status": "pending"}
        ):
            response_state = "pending"
            flash("This user has already been invited.", "error")
        else:
            invite = {
                "group_id": group["_id"],
                "invited_user_id": account_id,
                "invited_by_user_id": current_account_id(),
                "status": "pending",
                "created_at": utc_now(),
                "responded_at": None,
                "token": None,
            }
            try:
                result = db.group_invites.insert_one(invite)
            except DuplicateKeyError:
                flash("This user has already been invited.", "error")
            else:
                response_state = "pending"
                create_notification(
                    db,
                    account_id,
                    GROUP_INVITE,
                    group_id=group["_id"],
                    invite_id=result.inserted_id,
                    actor_user_id=current_account_id(),
                    metadata={
                        "group_name": group.get("name", "Group"),
                        "actor_name": current_user.username,
                    },
                )
                flash(f"Invitation sent to {account['username']}.", "success")
        if wants_json_response():
            return jsonify({"ok": response_state == "pending", "id": str(account_id), "username": account["username"], "state": response_state})
        return redirect(url_for("group_detail", group_id=group_id))

    def respond_to_group_invitation(invitation_id, status):
        db = require_database()
        invite = db.group_invites.find_one({"_id": object_id(invitation_id)})
        if not invite or invite.get("invited_user_id") != current_account_id():
            abort(404)
        if invite.get("status") != "pending":
            flash("This invitation has already been answered.", "error")
            return redirect(url_for("dashboard"))
        group = db.groups.find_one({"_id": invite.get("group_id")})
        if not group:
            db.group_invites.update_one(
                {"_id": invite["_id"], "status": "pending"},
                {"$set": {"status": "declined", "responded_at": utc_now()}},
            )
            flash("That group no longer exists.", "error")
            return redirect(url_for("dashboard"))
        if status == "accepted":
            member = ensure_account_member()
            db.groups.update_one(
                {"_id": group["_id"], "account_ids": {"$ne": current_account_id()}},
                {"$addToSet": {"member_ids": member["_id"], "account_ids": current_account_id()},
                 "$set": {f"account_roles.{current_account_id()}": "member"}},
            )
        result = db.group_invites.update_one(
            {"_id": invite["_id"], "invited_user_id": current_account_id(), "status": "pending"},
            {"$set": {"status": status, "responded_at": utc_now()}},
        )
        notification = db.notifications.find_one({
            "invite_id": invite["_id"],
            "recipient_user_id": current_account_id(),
            "status": {"$ne": "resolved"},
        })
        if notification:
            resolve_notification(db, notification["_id"], current_account_id())
        if result.modified_count != 1:
            flash("This invitation has already been answered.", "error")
        elif status == "accepted":
            flash(f"You joined {group['name']}.", "success")
        else:
            flash("Invitation declined.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/invitations/<invitation_id>/accept")
    @login_required
    def accept_group_invitation(invitation_id):
        return respond_to_group_invitation(invitation_id, "accepted")

    @app.post("/invitations/<invitation_id>/decline")
    @login_required
    def decline_group_invitation(invitation_id):
        return respond_to_group_invitation(invitation_id, "declined")

    @app.post("/groups/<group_id>/invitations/<invitation_id>/retract")
    @login_required
    def retract_group_invitation(group_id, invitation_id):
        db = require_database()
        group = get_group(group_id)
        require_group_permission(group, "manage_members")
        invite = db.group_invites.find_one({
            "_id": object_id(invitation_id), "group_id": group["_id"]
        })
        if not invite:
            abort(404)
        result = db.group_invites.update_one(
            {"_id": invite["_id"], "group_id": group["_id"], "status": "pending"},
            {"$set": {"status": "revoked", "responded_at": utc_now()}},
        )
        if result.modified_count:
            notification = db.notifications.find_one({
                "invite_id": invite["_id"],
                "status": {"$ne": "resolved"},
            })
            if notification:
                resolve_notification(db, notification["_id"], notification["recipient_user_id"])
            flash("Invitation retracted.", "success")
        else:
            flash("That invitation is no longer pending.", "error")
        return redirect(url_for("group_detail", group_id=group["_id"]))

    @app.post("/notifications/read")
    @login_required
    def mark_notifications_read():
        db = require_database()
        db.notifications.update_many(
            {"recipient_user_id": current_account_id(), "read_at": None},
            {"$set": {"read_at": utc_now()}},
        )
        return jsonify({"ok": True, "unread_count": unread_count(db, current_account_id())})

    def notification_payload(notification):
        metadata = notification.get("metadata", {})
        payload = {
            "id": str(notification["_id"]),
            "type": notification.get("type"),
            "read": bool(notification.get("read_at")),
            "status": notification.get("status", "unresolved"),
            "created_at": notification.get("created_at").isoformat() if notification.get("created_at") else "",
            "group_name": metadata.get("group_name", "Group invitation"),
            "actor_name": metadata.get("actor_name", "Someone"),
            "plan_title": metadata.get("plan_title", "Scheduled plan"),
            "scheduled_start": metadata.get("scheduled_start", ""),
            "scheduled_end": metadata.get("scheduled_end", ""),
            "invite_id": str(notification["invite_id"]) if notification.get("invite_id") else None,
            "plan_id": str(notification["plan_id"]) if notification.get("plan_id") else None,
        }
        return payload

    @app.get("/api/notifications")
    @login_required
    def api_notifications():
        db = require_database()
        items = get_user_notifications(db, current_account_id())
        return jsonify({
            "notifications": [notification_payload(item) for item in items],
            "unread_count": unread_count(db, current_account_id()),
        })

    @app.get("/api/notifications/summary")
    @login_required
    def api_notification_summary():
        db = require_database()
        latest = db.notifications.find_one(
            {"recipient_user_id": current_account_id()},
            sort=[("created_at", DESCENDING)],
        )
        return jsonify({
            "unread_count": unread_count(db, current_account_id()),
            "latest_created_at": latest.get("created_at").isoformat() if latest and latest.get("created_at") else None,
        })

    @app.post("/notifications/<notification_id>/rsvp")
    @login_required
    def notification_rsvp(notification_id):
        db = require_database()
        notification = db.notifications.find_one({
            "_id": object_id(notification_id),
            "recipient_user_id": current_account_id(),
            "type": PLAN_SCHEDULED,
            "status": {"$ne": "resolved"},
        })
        if not notification or not notification.get("plan_id"):
            abort(404)
        plan = get_plan(notification["plan_id"])
        group = db.groups.find_one({"_id": plan["group_id"]})
        member = db.users.find_one({
            "$or": [
                {"linked_account_id": current_account_id()},
                {"account_id": current_account_id()},
            ],
            "_id": {"$in": plan_member_ids(plan, group)},
        })
        if not member:
            abort(403)
        status = request.form.get("status", "").strip()
        if status not in {"attending", "not_attending"}:
            abort(400)
        response = {"status": status, "updated_at": utc_now()}
        db.plans.update_one(
            {"_id": plan["_id"]},
            {"$set": {f"rsvps.{member['_id']}": response}},
        )
        resolve_notification(db, notification["_id"], current_account_id())
        if status == "attending" and get_user_plan_conflicts(plan, member["_id"]):
            flash("Your RSVP was saved, but it conflicts with another scheduled plan.", "error")
        else:
            flash("RSVP updated successfully.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/groups/<group_id>/members/<user_id>/role")
    @login_required
    def update_group_member_role(group_id, user_id):
        db = require_database()
        group = get_group(group_id)
        require_group_owner(group)
        member = db.users.find_one({"_id": object_id(user_id)})
        if not member or member["_id"] not in group.get("member_ids", []):
            abort(404)
        target_account_id = member.get("linked_account_id")
        if not target_account_id or target_account_id == group.get("owner_account_id"):
            abort(403)
        role = request.form.get("role", "member").strip().lower()
        if role not in {"member", "admin"}:
            abort(400)
        permissions = [
            permission
            for permission in request.form.getlist("permissions")
            if permission in ADMIN_PERMISSIONS
        ]
        update = {"$set": {f"account_roles.{target_account_id}": role}}
        if role == "admin":
            update["$set"][f"admin_permissions.{target_account_id}"] = permissions
        else:
            update["$unset"] = {f"admin_permissions.{target_account_id}": ""}
        db.groups.update_one({"_id": group["_id"]}, update)
        flash(f"{member['display_name']} is now a {role}.", "success")
        return redirect(url_for("group_detail", group_id=group_id))

    @app.post("/groups/<group_id>/members/<user_id>/remove")
    @login_required
    def remove_group_member(group_id, user_id):
        db = require_database()
        group = get_group(group_id)
        require_group_permission(group, "manage_members")
        member = db.users.find_one({"_id": object_id(user_id)})
        if not member or member["_id"] not in group.get("member_ids", []):
            abort(404)
        if member.get("linked_account_id") == group.get("owner_account_id"):
            flash("The group owner cannot be removed.", "error")
        elif (
            not group_is_owner(group)
            and group_role(group, member.get("linked_account_id")) == "admin"
        ):
            flash("Admins cannot remove another admin.", "error")
        else:
            update = {"$pull": {"member_ids": member["_id"]}}
            linked_account_id = member.get("linked_account_id")
            if linked_account_id:
                update["$pull"]["account_ids"] = linked_account_id
                update["$unset"] = {f"account_roles.{linked_account_id}": ""}
            db.groups.update_one({"_id": group["_id"]}, update)
            flash(f"{member['display_name']} was removed from the group.", "success")
        return redirect(url_for("group_detail", group_id=group_id))

    @app.post("/groups/<group_id>/delete")
    @login_required
    def delete_group(group_id):
        db = require_database()
        group = get_group(group_id)
        require_group_owner(group)
        db.plans.delete_many({"group_id": group["_id"]})
        db.groups.delete_one({"_id": group["_id"]})
        flash("Group deleted successfully.", "success")
        return redirect(url_for("groups"))

    @app.route("/users/<user_id>/availability", methods=["GET", "POST"])
    @login_required
    def member_availability(user_id):
        db = require_database()
        user = get_user(user_id)
        if not member_can_manage(user):
            abort(403)
        return_to, return_label = availability_return_context()
        editor_state = availability_editor_state()
        if request.method == "POST":
            values, error = validate_availability(request.form)
            if error:
                if wants_json_response():
                    return jsonify({"error": error}), 400
                flash(error, "error")
            else:
                values.update({"user_id": user["_id"], "created_at": utc_now(), "updated_at": utc_now()})
                result = db.availability.insert_one(values)
                if wants_json_response():
                    return jsonify({
                        "entry": {
                            "id": str(result.inserted_id),
                            "day": values["day"],
                            "start_time": values["start_time"],
                            "end_time": values["end_time"],
                        }
                    })
                flash("Availability window added.", "success")
            return redirect(url_for(
                "member_availability",
                user_id=user_id,
                return_to=return_to,
                selected_day=editor_state["selected_day"],
                start_time=values["start_time"] if not error else editor_state["start_time"],
                end_time=values["end_time"] if not error else editor_state["end_time"],
            ))

        entries = list(
            db.availability.find({"user_id": user["_id"]}).sort(
                [("day_order", ASCENDING), ("start_time", ASCENDING)]
            )
        )
        return render_template(
            "member_availability.html",
            user=user,
            entries=entries,
            days=DAYS,
            return_to=return_to,
            return_label=return_label,
            editor_state=editor_state,
            can_edit_availability=member_can_manage(user),
        )

    @app.post("/users/<user_id>/availability/bulk-edit")
    @login_required
    def bulk_edit_availability(user_id):
        db = require_database()
        user = get_user(user_id)
        if not member_can_manage(user):
            abort(403)
        return_to, _return_label = availability_return_context()
        try:
            changes = json.loads(request.form.get("availability_payload", "[]"))
        except json.JSONDecodeError:
            changes = None
        if not isinstance(changes, list):
            if wants_json_response():
                return jsonify({"error": "Availability changes could not be read."}), 400
            flash("Availability changes could not be read.", "error")
            return redirect(url_for("member_availability", user_id=user_id, return_to=return_to))

        existing = {
            str(entry["_id"]): entry
            for entry in db.availability.find({"user_id": user["_id"]})
        }
        changes_to_apply = []
        for change in changes:
            if not isinstance(change, dict) or change.get("id") not in existing:
                if wants_json_response():
                    return jsonify({"error": "One of the availability windows could not be found."}), 400
                flash("One of the availability windows could not be found.", "error")
                return redirect(url_for("member_availability", user_id=user_id, return_to=return_to))
            entry = existing[change["id"]]
            if change.get("deleted"):
                changes_to_apply.append(("delete", entry["_id"], None))
                continue
            values, error = validate_availability_values(
                entry["day"],
                change.get("start_time", ""),
                change.get("end_time", ""),
            )
            if error:
                if wants_json_response():
                    return jsonify({"error": error}), 400
                flash(error, "error")
                return redirect(url_for("member_availability", user_id=user_id, return_to=return_to))
            values["updated_at"] = utc_now()
            changes_to_apply.append(("update", entry["_id"], values))
        # Validate the entire session before changing any document so invalid
        # input never leaves a partially edited weekly schedule.
        for operation, entry_id, values in changes_to_apply:
            if operation == "delete":
                db.availability.delete_one({"_id": entry_id})
            else:
                db.availability.update_one({"_id": entry_id}, {"$set": values})
        if changes_to_apply:
            if wants_json_response():
                return jsonify({"ok": True})
            flash("Availability changes saved.", "success")
        elif wants_json_response():
            return jsonify({"ok": True})
        return redirect(url_for("member_availability", user_id=user_id, return_to=return_to))

    @app.post("/availability/<availability_id>/edit")
    @login_required
    def edit_availability(availability_id):
        db = require_database()
        entry_id = object_id(availability_id)
        entry = db.availability.find_one({"_id": entry_id, "user_id": {"$exists": True}})
        if not entry:
            abort(404)
        user = get_user(entry["user_id"])
        if not member_can_manage(user):
            abort(403)
        values, error = validate_availability(request.form)
        if error:
            if wants_json_response():
                return jsonify({"error": error}), 400
            flash(error, "error")
        else:
            values["updated_at"] = utc_now()
            db.availability.update_one({"_id": entry_id}, {"$set": values})
            if wants_json_response():
                return jsonify({
                    "entry": {
                        "id": str(entry_id),
                        "day": values["day"],
                        "start_time": values["start_time"],
                        "end_time": values["end_time"],
                    }
                })
            flash("Availability window updated.", "success")
        return_to, _return_label = availability_return_context()
        return redirect(url_for("member_availability", user_id=entry["user_id"], return_to=return_to))

    @app.post("/availability/<availability_id>/delete")
    @login_required
    def delete_availability(availability_id):
        db = require_database()
        entry = db.availability.find_one({"_id": object_id(availability_id), "user_id": {"$exists": True}})
        if not entry:
            abort(404)
        user = get_user(entry["user_id"])
        if not member_can_manage(user):
            abort(403)
        db.availability.delete_one({"_id": entry["_id"]})
        if wants_json_response():
            return jsonify({"ok": True, "id": str(entry["_id"])})
        flash("Availability window deleted.", "success")
        return_to, _return_label = availability_return_context()
        return redirect(url_for("member_availability", user_id=entry["user_id"], return_to=return_to))

    @app.route("/groups/<group_id>/plans/create", methods=["GET", "POST"])
    @login_required
    def create_plan(group_id):
        db = require_database()
        group = get_group(group_id)
        require_group_permission(group, "manage_plans")
        group_size = len(group.get("member_ids", []))
        if request.method == "POST":
            values, errors = validate_plan_form(request.form, group_size)
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template("plan_create.html", group=group, form=request.form), 400

            values.update({
                "group_id": group["_id"],
                "invited_member_ids": list(group.get("member_ids", [])),
                "status": "planning",
                "matches": [],
                "created_at": utc_now(),
            })
            result = db.plans.insert_one(values)
            values["_id"] = result.inserted_id
            try:
                refresh_plan_matches(values, group)
            except ValueError as exc:
                app.logger.warning("Unable to generate initial matches: %s", exc)
            flash("Plan created. The best available times are ready to review.", "success")
            return redirect(url_for("plan_detail", plan_id=result.inserted_id))
        return render_template("plan_create.html", group=group)

    @app.post("/groups/<group_id>/schedule-preview")
    @login_required
    def schedule_preview(group_id):
        group = get_group(group_id)
        require_group_permission(group, "manage_plans")
        values, errors = validate_plan_form(
            request.form, len(group.get("member_ids", [])), require_title=False
        )
        if errors:
            return jsonify({"success": False, "errors": errors, "results": []}), 400
        try:
            matches = calculate_matches(group, values)
        except ValueError as exc:
            return jsonify({"success": False, "errors": [str(exc)], "results": []}), 400
        results = [
            {
                "date": match["date"],
                "day": match["day"],
                "start": match["start_time"],
                "end": match["end_time"],
                "attendance": match["attendance_count"],
                "group_size": match["total_members"],
                "exact_match": match["is_exact"],
                "available_duration_minutes": match["available_duration_minutes"],
            }
            for match in matches
        ]
        return jsonify({"success": True, "results": results})

    @app.get("/plans/<plan_id>")
    @login_required
    def plan_detail(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
        group = db.groups.find_one({"_id": plan["group_id"]})
        if not group:
            abort(404)
        group_members = list(
            db.users.find({
                "_id": {"$in": group.get("member_ids", [])},
            }).sort("display_name", ASCENDING)
        )
        members_by_id = {member["_id"]: member for member in group_members}
        members = plan_members(plan, group, members_by_id)
        participant_ids = set(plan_member_ids(plan, group))
        eligible_invitees = [
            member for member in group_members if member["_id"] not in participant_ids
        ]
        if not has_confirmed_schedule(plan) and not plan.get("matches"):
            try:
                refresh_plan_matches(plan, group)
            except ValueError as exc:
                flash(f"Scheduling options could not be generated: {exc}", "error")
        member_map = {str(member["_id"]): member for member in members}
        current_member_id = (
            (db.users.find_one({"linked_account_id": current_account_id()}) or {}).get("_id")
            if not auth_bypass()
            else None
        )
        rsvp = rsvp_summary(plan, members)
        conflicts = (
            get_user_plan_conflicts(plan, current_member_id)
            if current_member_id and has_confirmed_schedule(plan)
            else []
        )
        return render_template(
            "plan_detail.html",
            plan=plan,
            group=group,
            member_map=member_map,
            rsvp=rsvp,
            current_rsvp_row=next(
                (row for row in rsvp["rows"] if row["member"]["_id"] == current_member_id), None
            ),
            eligible_invitees=eligible_invitees,
            event_time_options=event_time_options(plan),
            rsvp_time_defaults=rsvp_time_defaults(plan),
            can_manage_group=has_group_permission(group, "manage_plans"),
            can_edit_member_rsvp=has_group_permission(group, "edit_member_rsvp"),
            current_member_id=current_member_id,
            conflicts=conflicts,
        )

    @app.route("/plans/<plan_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_plan(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
        group = db.groups.find_one({"_id": plan["group_id"]})
        if not group:
            abort(404)
        require_group_permission(group, "manage_plans")

        if request.method == "POST":
            values, errors = validate_plan_form(request.form, len(group.get("member_ids", [])))
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template("plan_edit.html", plan=plan, group=group, form=request.form), 400

            scheduling_fields = (
                "start_date", "end_date", "duration_minutes", "minimum_attendance",
                "earliest_time", "latest_time",
            )
            constraints_changed = any(plan.get(field) != values[field] for field in scheduling_fields)
            update = {"$set": values}
            if constraints_changed:
                update["$set"].update({"status": "planning", "matches": []})
                update["$unset"] = {"match_type": "", "matches_generated_at": ""}
                update["$unset"].update(clear_scheduled_fields())
            db.plans.update_one({"_id": plan["_id"]}, update)
            if constraints_changed:
                flash("Plan updated. Scheduling requirements changed, so select a new time.", "success")
            else:
                flash("Plan updated successfully.", "success")
            return redirect(url_for("plan_detail", plan_id=plan["_id"]))

        return render_template("plan_edit.html", plan=plan, group=group, form=plan)

    @app.post("/plans/<plan_id>/schedule")
    @login_required
    def schedule_plan(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
        group = db.groups.find_one({"_id": plan["group_id"]})
        if not group:
            abort(404)
        require_group_permission(group, "manage_plans")
        ensure_group_calendars_fresh(group, "scheduling")
        try:
            match_index = int(request.form.get("match_index", ""))
            if match_index < 0:
                raise IndexError
            match = plan.get("matches", [])[match_index]
        except (TypeError, ValueError, IndexError):
            abort(404)

        is_exact = match.get("is_exact", True)
        if not is_exact and request.form.get("confirm_shorter") != "yes":
            flash("Confirm the shorter duration before scheduling this option.", "error")
            return redirect(url_for("plan_detail", plan_id=plan["_id"]))

        try:
            scheduled_start = datetime.fromisoformat(f"{match['date']}T{match['start_time']}")
            scheduled_end = datetime.fromisoformat(f"{match['date']}T{match['end_time']}")
        except (KeyError, ValueError):
            abort(404)

        relevant_member_ids = plan_member_ids(plan, group)

        available_member_ids = []
        for member_id in match.get("available_member_ids", []):
            try:
                available_member_ids.append(ObjectId(member_id))
            except (InvalidId, TypeError):
                continue

        db.plans.update_one(
            {"_id": plan["_id"]},
            {"$set": {
                "status": "scheduled",
                "scheduled_start": scheduled_start,
                "scheduled_end": scheduled_end,
                "scheduled_available_member_ids": available_member_ids,
                "scheduled_attendance_count": match.get("attendance_count", len(available_member_ids)),
                "scheduled_total_members": match.get("total_members", len(available_member_ids)),
                "scheduled_is_shorter": not is_exact,
                "scheduled_available_duration_minutes": match.get(
                    "available_duration_minutes", plan["duration_minutes"]
                ),
                "rsvps": {},
            }},
        )
        for member_id in relevant_member_ids:
            member = db.users.find_one({"_id": member_id}, {"linked_account_id": 1, "account_id": 1})
            recipient_id = (member or {}).get("linked_account_id") or (member or {}).get("account_id")
            if recipient_id:
                create_notification(
                    db,
                    recipient_id,
                    PLAN_SCHEDULED,
                    group_id=group["_id"],
                    plan_id=plan["_id"],
                    actor_user_id=current_account_id(),
                    metadata={
                        "plan_title": plan.get("title", "Scheduled plan"),
                        "scheduled_start": scheduled_start.isoformat(),
                        "scheduled_end": scheduled_end.isoformat(),
                    },
                )
        date_text = scheduled_start.strftime("%A, %B %d").replace(" 0", " ")
        start_text = scheduled_start.strftime("%I:%M %p").lstrip("0")
        end_text = scheduled_end.strftime("%I:%M %p").lstrip("0")
        flash(f"Plan scheduled for {date_text}, {start_text} - {end_text}.", "success")
        return redirect(url_for("plan_detail", plan_id=plan["_id"]))

    @app.post("/plans/<plan_id>/invite")
    @login_required
    def invite_plan_member(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
        if not has_confirmed_schedule(plan):
            flash("Members can be invited after the plan is scheduled.", "error")
            return redirect(url_for("plan_detail", plan_id=plan["_id"]))
        group = db.groups.find_one({"_id": plan["group_id"]})
        if not group:
            abort(404)
        require_group_permission(group, "manage_plans")
        member_id = object_id(request.form.get("member_id"))
        member = get_user(member_id)
        if member_id not in group.get("member_ids", []):
            abort(404)
        if member_id in plan_member_ids(plan, group):
            flash("That member is already invited to this plan.", "error")
        else:
            db.plans.update_one(
                {"_id": plan["_id"]}, {"$addToSet": {"invited_member_ids": member_id}}
            )
            flash(f"{member['display_name']} was invited to the plan.", "success")
        return redirect(url_for("plan_detail", plan_id=plan["_id"]))

    @app.post("/plans/<plan_id>/rsvp")
    @login_required
    def update_rsvp(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
        if not has_confirmed_schedule(plan):
            flash("RSVPs are available after a plan has been scheduled.", "error")
            return redirect(url_for("plan_detail", plan_id=plan["_id"]))
        group = db.groups.find_one({"_id": plan["group_id"]})
        if not group:
            abort(404)
        member_id = object_id(request.form.get("member_id"))
        if member_id not in plan_member_ids(plan, group):
            abort(404)
        member = db.users.find_one({"_id": member_id})
        if not member:
            abort(404)
        if (
            not has_group_permission(group, "edit_member_rsvp")
            and member.get("linked_account_id") != current_account_id()
        ):
            abort(403)

        status = request.form.get("status", "").strip()
        allowed_statuses = {"pending", "attending", "not_attending"}
        if status not in allowed_statuses:
            flash("Please choose a valid RSVP response.", "error")
            return redirect(url_for("plan_detail", plan_id=plan["_id"]))

        scheduled_start = as_datetime(plan["scheduled_start"])
        scheduled_end = as_datetime(plan["scheduled_end"])

        def response_datetime(field_name):
            value = request.form.get(field_name, "").strip()
            if not value:
                return "", None
            try:
                normalized = normalize_time_text(value)
                time_minutes = parse_time(normalized)
            except ValueError:
                return value, None
            value_datetime = datetime.combine(
                scheduled_start.date(), datetime.min.time()
            ).replace(hour=time_minutes // 60, minute=time_minutes % 60)
            return normalized, value_datetime

        response = {"status": status, "updated_at": utc_now()}
        if status == "attending":
            arriving_late = request.form.get("arriving_late") == "on"
            leaving_early = request.form.get("leaving_early") == "on"
            arrival_time, arrival = response_datetime("arrival_time")
            departure_time, departure = response_datetime("departure_time")
            if arrival_time and not arrival:
                flash("Please enter a valid arrival time.", "error")
                return redirect(url_for("plan_detail", plan_id=plan["_id"]))
            if departure_time and not departure:
                flash("Please enter a valid leaving time.", "error")
                return redirect(url_for("plan_detail", plan_id=plan["_id"]))
            if arrival and not scheduled_start < arrival < scheduled_end:
                flash("Arrival time must be after the start and before the event end.", "error")
                return redirect(url_for("plan_detail", plan_id=plan["_id"]))
            if departure and not scheduled_start < departure < scheduled_end:
                flash("Leaving time must be after the start and before the event end.", "error")
                return redirect(url_for("plan_detail", plan_id=plan["_id"]))
            if arrival and departure and not arrival < departure:
                flash(
                    "Arrival and departure must be inside the event, with arrival before departure.",
                    "error",
                )
                return redirect(url_for("plan_detail", plan_id=plan["_id"]))
            response.update({
                "arriving_late": arriving_late,
                "leaving_early": leaving_early,
            })
            if arriving_late and arrival:
                response["arrival_time"] = arrival_time
            if leaving_early and departure:
                response["departure_time"] = departure_time
        else:
            # Pending and Not Attending never retain partial-attendance fields.
            response.pop("arriving_late", None)
            response.pop("leaving_early", None)

        db.plans.update_one(
            {"_id": plan["_id"]},
            {"$set": {f"rsvps.{member_id}": response}},
        )
        flash("RSVP updated successfully.", "success")
        return redirect(url_for("plan_detail", plan_id=plan["_id"]))

    @app.post("/plans/<plan_id>/delete")
    @login_required
    def delete_plan(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
        group_id = plan["group_id"]
        group = db.groups.find_one({"_id": group_id})
        if not group:
            abort(404)
        require_group_permission(group, "manage_plans")
        db.plans.delete_one({"_id": plan["_id"]})
        flash("Plan deleted successfully.", "success")
        if db.groups.find_one({"_id": group_id}):
            return redirect(url_for("group_detail", group_id=group_id))
        return redirect(url_for("groups"))

    @app.post("/plans/<plan_id>/find-times")
    @login_required
    def find_plan_times(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
        group = db.groups.find_one({"_id": plan["group_id"]})
        if not group:
            abort(404)
        require_group_permission(group, "manage_plans")
        was_scheduled = has_confirmed_schedule(plan)
        if was_scheduled:
            db.plans.update_one(
                {"_id": plan["_id"]},
                {"$set": {"status": "planning"}, "$unset": clear_scheduled_fields()},
            )
            plan["status"] = "planning"
            for field in clear_scheduled_fields():
                plan.pop(field, None)
        try:
            matches = refresh_plan_matches(plan, group)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("plan_detail", plan_id=plan_id))
        if matches and matches[0].get("is_exact"):
            flash(f"Found {len(matches)} matching time options.", "success")
        elif matches:
            flash(
                "No matching times were found for the requested duration. "
                "Showing the closest available options.",
                "error",
            )
        else:
            flash("No matching times were found.", "error")
        return redirect(url_for("plan_detail", plan_id=plan_id))

    @app.get("/results")
    @login_required
    def results():
        return redirect(url_for("groups"))

    @app.post("/submit")
    @login_required
    def submit():
        flash("Create or choose a member before adding recurring availability.", "error")
        return redirect(url_for("users"))

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="That page or record was not found."), 404

    @app.errorhandler(500)
    def server_error(_error):
        return render_template("error.html", code=500, message="Something went wrong on the server."), 500

    @app.errorhandler(503)
    def unavailable(_error):
        return render_template("error.html", code=503, message="The database is currently unavailable."), 503

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
