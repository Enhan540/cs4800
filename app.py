"""CS4800 recurring group scheduling MVP."""

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

from services.scheduling_service import DAYS, find_matching_times, parse_date, parse_time

load_dotenv()

DAY_ORDER = {day: index for index, day in enumerate(DAYS)}


def utc_now():
    return datetime.now(timezone.utc)


def object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        abort(404)


def create_app(database_override=None):
    app = Flask(__name__)
    try:
        app_timezone = ZoneInfo(os.getenv("APP_TIMEZONE", "America/Los_Angeles"))
    except ZoneInfoNotFoundError:
        app_timezone = datetime.now().astimezone().tzinfo or timezone.utc
    app.config["SECRET_KEY"] = (
        os.getenv("SECRET_KEY")
        or os.getenv("FLASK_SECRET_KEY")
        or "dev-only-change-me"
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
            database.users.create_index("normalized_name", unique=True)
            database.users.create_index("email", unique=True, sparse=True)
            database.availability.create_index(
                [("user_id", ASCENDING), ("day_order", ASCENDING), ("start_time", ASCENDING)]
            )
            database.groups.create_index("created_at")
            database.plans.create_index([("group_id", ASCENDING), ("created_at", DESCENDING)])
        except PyMongoError:
            app.logger.exception("Unable to create MongoDB indexes during startup")

    def require_database():
        if database is None:
            flash("The database is not configured or is currently unavailable.", "error")
            abort(503)
        return database

    def get_user(user_id):
        user = require_database().users.find_one({"_id": object_id(user_id)})
        if not user:
            abort(404)
        return user

    def get_group(group_id):
        group = require_database().groups.find_one({"_id": object_id(group_id)})
        if not group:
            abort(404)
        return group

    def get_plan(plan_id):
        plan = require_database().plans.find_one({"_id": object_id(plan_id)})
        if not plan:
            abort(404)
        return plan

    def validate_availability(form):
        day = form.get("day", "").strip()
        start_time = form.get("start_time", "").strip()
        end_time = form.get("end_time", "").strip()

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

    def calculate_matches(group, values):
        member_ids = group.get("member_ids", [])
        availability_records = list(
            require_database().availability.find({"user_id": {"$in": member_ids}})
        )
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
        matches = calculate_matches(group, plan)
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

    def plan_members(plan, group, members_by_id):
        return [
            members_by_id[member_id]
            for member_id in plan_member_ids(plan, group)
            if member_id in members_by_id
        ]

    def event_time_options(plan, increment_minutes=15):
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
            "no_response": 0,
        }
        rows = []
        for member in members:
            response = responses.get(str(member["_id"]), {})
            status = response.get("status", "no_response")
            if status not in counts:
                status = "no_response"
            counts[status] += 1
            rows.append({
                "member": member,
                "status": status,
                "time": response.get("time"),
                "arrival_time": response.get("arrival_time") or (
                    response.get("time") if status == "arriving_late" else None
                ),
                "departure_time": response.get("departure_time") or (
                    response.get("time") if status == "leaving_early" else None
                ),
            })
        counts["overall_attending"] = (
            counts["attending"]
            + counts["arriving_late"]
            + counts["leaving_early"]
            + counts["arriving_late_leaving_early"]
        )
        counts["pending"] = counts["no_response"]
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
        groups = []
        upcoming_plans = []
        stats = {"groups": 0, "members": 0, "plans": 0}
        if database is not None:
            try:
                stats = {
                    "groups": database.groups.count_documents({}),
                    "members": database.users.count_documents({}),
                    "plans": database.plans.count_documents({}),
                }
                groups = list(database.groups.find().sort("created_at", DESCENDING).limit(5))
                all_plans = list(database.plans.find())
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
                    for member in database.users.find({"_id": {"$in": list(member_ids)}})
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
            except PyMongoError:
                app.logger.exception("Unable to load dashboard data")
                flash("Some dashboard data could not be loaded.", "error")
        return render_template(
            "index.html",
            stats=stats,
            groups=groups,
            upcoming_plans=upcoming_plans,
        )

    @app.route("/users", methods=["GET", "POST"])
    def users():
        db = require_database()
        group_id = request.values.get("group_id", "").strip()
        origin_group = get_group(group_id) if group_id else None
        if request.method == "POST":
            display_name = request.form.get("display_name", "").strip()
            email = request.form.get("email", "").strip().lower()
            if not display_name:
                flash("Display name is required.", "error")
                return redirect(url_for("users"))

            duplicate_queries = [{"normalized_name": display_name.casefold()}]
            if email:
                duplicate_queries.append({"email": email})
            existing_member = db.users.find_one({"$or": duplicate_queries})
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

        member_list = list(db.users.find().sort("display_name", ASCENDING))
        return render_template("users.html", users=member_list, origin_group=origin_group)

    @app.get("/groups")
    def groups():
        group_list = list(require_database().groups.find().sort("created_at", DESCENDING))
        return render_template("groups.html", groups=group_list)

    @app.route("/groups/create", methods=["GET", "POST"])
    def create_group():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            if not name:
                flash("Group name is required.", "error")
                return render_template("group_create.html", form=request.form), 400
            result = require_database().groups.insert_one(
                {
                    "name": name,
                    "description": description or None,
                    "member_ids": [],
                    "created_at": utc_now(),
                }
            )
            flash(f"{name} was created.", "success")
            return redirect(url_for("group_detail", group_id=result.inserted_id))
        return render_template("group_create.html")

    @app.route("/groups/<group_id>/edit", methods=["GET", "POST"])
    def edit_group(group_id):
        db = require_database()
        group = get_group(group_id)
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
    def group_detail(group_id):
        db = require_database()
        group = get_group(group_id)
        member_ids = group.get("member_ids", [])
        members = list(db.users.find({"_id": {"$in": member_ids}}).sort("display_name", ASCENDING))
        members_by_id = {member["_id"]: member for member in members}
        entries = list(
            db.availability.find({"user_id": {"$in": member_ids}}).sort(
                [("day_order", ASCENDING), ("start_time", ASCENDING)]
            )
        )
        availability_by_member = {
            member["_id"]: {day: [] for day in DAYS}
            for member in members
        }
        for entry in entries:
            member_days = availability_by_member.get(entry.get("user_id"))
            if member_days is not None and entry.get("day") in member_days:
                member_days[entry["day"]].append(entry)
        availability_rows = [
            {"member": member, "days": availability_by_member[member["_id"]]}
            for member in members
        ]
        available_users = list(
            db.users.find({"_id": {"$nin": member_ids}}).sort("display_name", ASCENDING)
        )
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
            available_users=available_users,
            availability_days=DAYS,
            availability_rows=availability_rows,
            scheduled_plans=scheduled_plans,
            planning_plans=planning_plans,
            upcoming_plan_count=len(scheduled_plans),
        )

    @app.post("/groups/<group_id>/members")
    def add_group_member(group_id):
        db = require_database()
        group = get_group(group_id)
        member_id = object_id(request.form.get("user_id"))
        member = db.users.find_one({"_id": member_id})
        if not member:
            abort(404)
        if member_id in group.get("member_ids", []):
            flash("That member is already in the group.", "error")
        else:
            db.groups.update_one({"_id": group["_id"]}, {"$addToSet": {"member_ids": member_id}})
            flash(f"{member['display_name']} was added to the group.", "success")
        return redirect(url_for("group_detail", group_id=group_id))

    @app.post("/groups/<group_id>/delete")
    def delete_group(group_id):
        db = require_database()
        group = get_group(group_id)
        db.plans.delete_many({"group_id": group["_id"]})
        db.groups.delete_one({"_id": group["_id"]})
        flash("Group deleted successfully.", "success")
        return redirect(url_for("groups"))

    @app.route("/users/<user_id>/availability", methods=["GET", "POST"])
    def member_availability(user_id):
        db = require_database()
        user = get_user(user_id)
        if request.method == "POST":
            values, error = validate_availability(request.form)
            if error:
                flash(error, "error")
            else:
                values.update({"user_id": user["_id"], "created_at": utc_now(), "updated_at": utc_now()})
                db.availability.insert_one(values)
                flash("Availability window added.", "success")
            return redirect(url_for("member_availability", user_id=user_id))

        entries = list(
            db.availability.find({"user_id": user["_id"]}).sort(
                [("day_order", ASCENDING), ("start_time", ASCENDING)]
            )
        )
        return render_template("member_availability.html", user=user, entries=entries, days=DAYS)

    @app.post("/availability/<availability_id>/edit")
    def edit_availability(availability_id):
        db = require_database()
        entry_id = object_id(availability_id)
        entry = db.availability.find_one({"_id": entry_id, "user_id": {"$exists": True}})
        if not entry:
            abort(404)
        values, error = validate_availability(request.form)
        if error:
            flash(error, "error")
        else:
            values["updated_at"] = utc_now()
            db.availability.update_one({"_id": entry_id}, {"$set": values})
            flash("Availability window updated.", "success")
        return redirect(url_for("member_availability", user_id=entry["user_id"]))

    @app.post("/availability/<availability_id>/delete")
    def delete_availability(availability_id):
        db = require_database()
        entry = db.availability.find_one({"_id": object_id(availability_id), "user_id": {"$exists": True}})
        if not entry:
            abort(404)
        db.availability.delete_one({"_id": entry["_id"]})
        flash("Availability window deleted.", "success")
        return redirect(url_for("member_availability", user_id=entry["user_id"]))

    @app.route("/groups/<group_id>/plans/create", methods=["GET", "POST"])
    def create_plan(group_id):
        db = require_database()
        group = get_group(group_id)
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
    def schedule_preview(group_id):
        group = get_group(group_id)
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
    def plan_detail(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
        group = db.groups.find_one({"_id": plan["group_id"]})
        if not group:
            abort(404)
        group_members = list(
            db.users.find({"_id": {"$in": group.get("member_ids", [])}}).sort(
                "display_name", ASCENDING
            )
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
        return render_template(
            "plan_detail.html",
            plan=plan,
            group=group,
            member_map=member_map,
            rsvp=rsvp_summary(plan, members),
            eligible_invitees=eligible_invitees,
            event_time_options=event_time_options(plan),
        )

    @app.route("/plans/<plan_id>/edit", methods=["GET", "POST"])
    def edit_plan(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
        group = db.groups.find_one({"_id": plan["group_id"]})
        if not group:
            abort(404)

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
    def schedule_plan(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
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
        date_text = scheduled_start.strftime("%A, %B %d").replace(" 0", " ")
        start_text = scheduled_start.strftime("%I:%M %p").lstrip("0")
        end_text = scheduled_end.strftime("%I:%M %p").lstrip("0")
        flash(f"Plan scheduled for {date_text}, {start_text} - {end_text}.", "success")
        return redirect(url_for("plan_detail", plan_id=plan["_id"]))

    @app.post("/plans/<plan_id>/invite")
    def invite_plan_member(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
        if not has_confirmed_schedule(plan):
            flash("Members can be invited after the plan is scheduled.", "error")
            return redirect(url_for("plan_detail", plan_id=plan["_id"]))
        group = db.groups.find_one({"_id": plan["group_id"]})
        if not group:
            abort(404)
        member_id = object_id(request.form.get("member_id"))
        member = db.users.find_one({"_id": member_id})
        if not member or member_id not in group.get("member_ids", []):
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

        status = request.form.get("status", "").strip()
        allowed_statuses = {
            "attending", "not_attending", "arriving_late", "leaving_early",
            "arriving_late_leaving_early",
        }
        if status not in allowed_statuses:
            flash("Please choose a valid RSVP response.", "error")
            return redirect(url_for("plan_detail", plan_id=plan["_id"]))

        scheduled_start = as_datetime(plan["scheduled_start"])
        scheduled_end = as_datetime(plan["scheduled_end"])

        def response_datetime(field_name, legacy=False):
            value = request.form.get(field_name, "").strip()
            if not value and legacy:
                value = request.form.get("response_time", "").strip()
            try:
                time_minutes = parse_time(value)
            except ValueError:
                return value, None
            value_datetime = datetime.combine(
                scheduled_start.date(), datetime.min.time()
            ).replace(hour=time_minutes // 60, minute=time_minutes % 60)
            return value, value_datetime

        response = {"status": status, "updated_at": utc_now()}
        if status == "arriving_late":
            arrival_time, arrival = response_datetime("arrival_time", legacy=True)
            if not arrival:
                flash("A valid arrival time is required.", "error")
                return redirect(url_for("plan_detail", plan_id=plan["_id"]))
            if not scheduled_start < arrival < scheduled_end:
                flash("Arrival time must be after the start and before the event end.", "error")
                return redirect(url_for("plan_detail", plan_id=plan["_id"]))
            response.update({"arrival_time": arrival_time, "time": arrival_time})
        elif status == "leaving_early":
            departure_time, departure = response_datetime("departure_time", legacy=True)
            if not departure:
                flash("A valid departure time is required.", "error")
                return redirect(url_for("plan_detail", plan_id=plan["_id"]))
            if not scheduled_start < departure < scheduled_end:
                flash(
                    "Departure time must be no earlier than the first valid option and before the event end.",
                    "error",
                )
                return redirect(url_for("plan_detail", plan_id=plan["_id"]))
            response.update({"departure_time": departure_time, "time": departure_time})
        elif status == "arriving_late_leaving_early":
            arrival_time, arrival = response_datetime("arrival_time")
            departure_time, departure = response_datetime("departure_time")
            if not arrival or not departure:
                flash("Valid arrival and departure times are required.", "error")
                return redirect(url_for("plan_detail", plan_id=plan["_id"]))
            if not scheduled_start < arrival < departure < scheduled_end:
                flash(
                    "Arrival and departure must be inside the event, with arrival before departure.",
                    "error",
                )
                return redirect(url_for("plan_detail", plan_id=plan["_id"]))
            response.update({"arrival_time": arrival_time, "departure_time": departure_time})

        db.plans.update_one(
            {"_id": plan["_id"]},
            {"$set": {f"rsvps.{member_id}": response}},
        )
        flash("RSVP updated successfully.", "success")
        return redirect(url_for("plan_detail", plan_id=plan["_id"]))

    @app.post("/plans/<plan_id>/delete")
    def delete_plan(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
        group_id = plan["group_id"]
        db.plans.delete_one({"_id": plan["_id"]})
        flash("Plan deleted successfully.", "success")
        if db.groups.find_one({"_id": group_id}):
            return redirect(url_for("group_detail", group_id=group_id))
        return redirect(url_for("groups"))

    @app.post("/plans/<plan_id>/find-times")
    def find_plan_times(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
        group = db.groups.find_one({"_id": plan["group_id"]})
        if not group:
            abort(404)
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
    def results():
        return redirect(url_for("groups"))

    @app.post("/submit")
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
