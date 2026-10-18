"""CS4800 recurring group scheduling MVP."""

import os
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, url_for
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

    @app.get("/")
    def index():
        groups = []
        recent_plans = []
        stats = {"groups": 0, "members": 0, "plans": 0}
        if database is not None:
            try:
                stats = {
                    "groups": database.groups.count_documents({}),
                    "members": database.users.count_documents({}),
                    "plans": database.plans.count_documents({}),
                }
                groups = list(database.groups.find().sort("created_at", DESCENDING).limit(5))
                recent_plans = list(database.plans.find().sort("created_at", DESCENDING).limit(5))
                group_names = {
                    group["_id"]: group["name"]
                    for group in database.groups.find(
                        {"_id": {"$in": [plan["group_id"] for plan in recent_plans]}}
                    )
                }
                for plan in recent_plans:
                    plan["group_name"] = group_names.get(plan["group_id"], "Unknown group")
            except PyMongoError:
                app.logger.exception("Unable to load dashboard data")
                flash("Some dashboard data could not be loaded.", "error")
        return render_template("index.html", stats=stats, groups=groups, recent_plans=recent_plans)

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

    @app.get("/groups/<group_id>")
    def group_detail(group_id):
        db = require_database()
        group = get_group(group_id)
        member_ids = group.get("member_ids", [])
        members = list(db.users.find({"_id": {"$in": member_ids}}).sort("display_name", ASCENDING))
        member_map = {member["_id"]: member for member in members}
        entries = list(
            db.availability.find({"user_id": {"$in": member_ids}}).sort(
                [("day_order", ASCENDING), ("start_time", ASCENDING)]
            )
        )
        for entry in entries:
            entry["member"] = member_map.get(entry["user_id"])
        available_users = list(
            db.users.find({"_id": {"$nin": member_ids}}).sort("display_name", ASCENDING)
        )
        plans = list(db.plans.find({"group_id": group["_id"]}).sort("created_at", DESCENDING))
        return render_template(
            "group_detail.html",
            group=group,
            members=members,
            available_users=available_users,
            entries=entries,
            plans=plans,
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
            title = request.form.get("title", "").strip()
            start_date = request.form.get("start_date", "").strip()
            end_date = request.form.get("end_date", "").strip()
            earliest_time = request.form.get("earliest_time", "").strip() or None
            latest_time = request.form.get("latest_time", "").strip() or None
            errors = []
            try:
                duration_minutes = int(request.form.get("duration_minutes", "0"))
                minimum_attendance = int(request.form.get("minimum_attendance", "0"))
            except ValueError:
                duration_minutes = 0
                minimum_attendance = 0

            try:
                parsed_start = parse_date(start_date)
                parsed_end = parse_date(end_date)
                if parsed_end < parsed_start:
                    errors.append("End date must be on or after the start date.")
                if (parsed_end - parsed_start).days > 366:
                    errors.append("Date range must be one year or less.")
            except ValueError:
                errors.append("Please enter a valid date range.")
            if not title:
                errors.append("Plan title is required.")
            if duration_minutes <= 0:
                errors.append("Meeting duration must be greater than zero.")
            if minimum_attendance < 1:
                errors.append("Minimum attendance must be at least one.")
            if minimum_attendance > group_size:
                errors.append("Minimum attendance cannot exceed the group size.")
            try:
                if earliest_time:
                    parse_time(earliest_time)
                if latest_time:
                    parse_time(latest_time)
                if earliest_time and latest_time and parse_time(latest_time) <= parse_time(earliest_time):
                    errors.append("Latest allowed time must be later than earliest allowed time.")
            except ValueError:
                errors.append("Please enter valid allowed times.")

            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template("plan_create.html", group=group, form=request.form), 400

            result = db.plans.insert_one(
                {
                    "group_id": group["_id"],
                    "title": title,
                    "start_date": start_date,
                    "end_date": end_date,
                    "duration_minutes": duration_minutes,
                    "earliest_time": earliest_time,
                    "latest_time": latest_time,
                    "minimum_attendance": minimum_attendance,
                    "status": "planning",
                    "matches": [],
                    "created_at": utc_now(),
                }
            )
            flash("Plan created. Run the time finder to calculate matches.", "success")
            return redirect(url_for("plan_detail", plan_id=result.inserted_id))
        return render_template("plan_create.html", group=group)

    @app.get("/plans/<plan_id>")
    def plan_detail(plan_id):
        db = require_database()
        plan = get_plan(plan_id)
        group = db.groups.find_one({"_id": plan["group_id"]})
        if not group:
            abort(404)
        members = list(db.users.find({"_id": {"$in": group.get("member_ids", [])}}))
        member_map = {str(member["_id"]): member for member in members}
        return render_template("plan_detail.html", plan=plan, group=group, member_map=member_map)

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
        member_ids = group.get("member_ids", [])
        availability_records = list(db.availability.find({"user_id": {"$in": member_ids}}))
        try:
            matches = find_matching_times(
                member_ids=member_ids,
                availability_records=availability_records,
                start_date=plan["start_date"],
                end_date=plan["end_date"],
                duration_minutes=plan["duration_minutes"],
                earliest_time=plan.get("earliest_time"),
                latest_time=plan.get("latest_time"),
                minimum_attendance=plan["minimum_attendance"],
                max_results=10,
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("plan_detail", plan_id=plan_id))

        db.plans.update_one(
            {"_id": plan["_id"]},
            {"$set": {
                "matches": matches,
                "match_type": (
                    "exact" if matches and matches[0].get("is_exact")
                    else "alternative" if matches
                    else "none"
                ),
                "matches_generated_at": utc_now(),
            }},
        )
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
