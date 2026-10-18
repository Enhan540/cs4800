import os
from datetime import datetime, timezone

import mongomock
import pytest
from bson import ObjectId

os.environ["MONGO_URI"] = ""
from app import create_app


@pytest.fixture
def app_and_db():
    database = mongomock.MongoClient().meetwise_test
    app = create_app(database_override=database)
    app.config.update(TESTING=True, SECRET_KEY="test-secret")
    return app, database


def make_group(database, name="Friday Friends", member_ids=None):
    return database.groups.insert_one({
        "name": name,
        "description": None,
        "member_ids": member_ids or [],
        "created_at": datetime.now(timezone.utc),
    }).inserted_id


def make_plan(database, group_id, **overrides):
    document = {
        "group_id": group_id,
        "title": "Movie Night",
        "start_date": "2026-10-23",
        "end_date": "2026-10-24",
        "duration_minutes": 120,
        "earliest_time": "17:00",
        "latest_time": "23:00",
        "minimum_attendance": 1,
        "status": "planning",
        "matches": [],
        "created_at": datetime.now(timezone.utc),
    }
    document.update(overrides)
    return database.plans.insert_one(document).inserted_id


def matching_time(member_ids, *, exact=True, duration=120):
    return {
        "date": "2026-10-23",
        "day": "Friday",
        "start_time": "19:00",
        "end_time": "21:00" if exact else "20:00",
        "attendance_count": len(member_ids),
        "total_members": len(member_ids),
        "available_member_ids": [str(member_id) for member_id in member_ids],
        "unavailable_member_ids": [],
        "is_exact": exact,
        "available_duration_minutes": duration,
    }


@pytest.mark.parametrize("duration", [45, 75, 125, 180])
def test_plan_creation_accepts_arbitrary_positive_duration(app_and_db, duration):
    app, database = app_and_db
    member_id = database.users.insert_one({"display_name": "Alex", "normalized_name": "alex"}).inserted_id
    group_id = make_group(database, member_ids=[member_id])

    response = app.test_client().post(f"/groups/{group_id}/plans/create", data={
        "title": f"Plan {duration}",
        "start_date": "2026-10-23",
        "end_date": "2026-10-23",
        "duration_minutes": str(duration),
        "earliest_time": "",
        "latest_time": "",
        "minimum_attendance": "1",
    })

    assert response.status_code == 302
    assert database.plans.find_one({"title": f"Plan {duration}"})["duration_minutes"] == duration


def test_delete_plan_removes_it(app_and_db):
    app, database = app_and_db
    group_id = make_group(database)
    plan_id = database.plans.insert_one({"group_id": group_id, "title": "Movie Night"}).inserted_id

    response = app.test_client().post(f"/plans/{plan_id}/delete")

    assert response.status_code == 302
    assert database.plans.find_one({"_id": plan_id}) is None
    assert response.headers["Location"].endswith(f"/groups/{group_id}")


def test_delete_group_cleans_plans_but_preserves_members_and_availability(app_and_db):
    app, database = app_and_db
    user_id = database.users.insert_one({"display_name": "Alex", "normalized_name": "alex"}).inserted_id
    deleted_group_id = make_group(database, "Delete Me", [user_id])
    other_group_id = make_group(database, "Keep Me", [user_id])
    database.plans.insert_one({"group_id": deleted_group_id, "title": "Delete Plan"})
    availability_id = database.availability.insert_one({
        "user_id": user_id, "day": "Friday", "start_time": "18:00", "end_time": "20:00"
    }).inserted_id

    response = app.test_client().post(f"/groups/{deleted_group_id}/delete")

    assert response.status_code == 302
    assert database.groups.find_one({"_id": deleted_group_id}) is None
    assert database.plans.count_documents({"group_id": deleted_group_id}) == 0
    assert database.users.find_one({"_id": user_id}) is not None
    assert database.availability.find_one({"_id": availability_id}) is not None
    assert user_id in database.groups.find_one({"_id": other_group_id})["member_ids"]


def test_member_created_from_group_is_added_and_remains_reusable(app_and_db):
    app, database = app_and_db
    first_group_id = make_group(database, "Friday Friends")
    second_group_id = make_group(database, "D&D Group")
    client = app.test_client()

    response = client.post("/users", data={
        "display_name": "Taylor", "email": "taylor@example.com", "group_id": str(first_group_id)
    })
    taylor = database.users.find_one({"normalized_name": "taylor"})

    assert response.status_code == 302
    assert taylor is not None
    assert taylor["_id"] in database.groups.find_one({"_id": first_group_id})["member_ids"]

    client.post(f"/groups/{second_group_id}/members", data={"user_id": str(taylor["_id"])})
    assert taylor["_id"] in database.groups.find_one({"_id": second_group_id})["member_ids"]

    client.post("/users", data={
        "display_name": "Taylor", "email": "taylor@example.com", "group_id": str(first_group_id)
    })
    assert database.groups.find_one({"_id": first_group_id})["member_ids"].count(taylor["_id"]) == 1


def test_standalone_member_creation_still_works(app_and_db):
    app, database = app_and_db
    group_id = make_group(database)

    response = app.test_client().post("/users", data={"display_name": "Jordan", "email": ""})

    jordan = database.users.find_one({"normalized_name": "jordan"})
    assert response.status_code == 302
    assert jordan is not None
    assert jordan["_id"] not in database.groups.find_one({"_id": group_id})["member_ids"]


def test_delete_missing_records_returns_404(app_and_db):
    app, _database = app_and_db
    client = app.test_client()
    assert client.post(f"/plans/{ObjectId()}/delete").status_code == 404
    assert client.post(f"/groups/{ObjectId()}/delete").status_code == 404


def test_schedule_exact_match_persists_selected_time_and_members(app_and_db):
    app, database = app_and_db
    member_ids = [
        database.users.insert_one({"display_name": name, "normalized_name": name.lower()}).inserted_id
        for name in ("Alex", "Sam")
    ]
    group_id = make_group(database, member_ids=member_ids)
    plan_id = make_plan(database, group_id, matches=[matching_time(member_ids)])

    response = app.test_client().post(
        f"/plans/{plan_id}/schedule", data={"match_index": "0"}, follow_redirects=True
    )
    plan = database.plans.find_one({"_id": plan_id})

    assert response.status_code == 200
    assert b"Plan scheduled for Friday, October 23, 7:00 PM - 9:00 PM." in response.data
    assert b"Scheduled" in response.data
    assert plan["status"] == "scheduled"
    assert plan["scheduled_start"] == datetime(2026, 10, 23, 19, 0)
    assert plan["scheduled_end"] == datetime(2026, 10, 23, 21, 0)
    assert plan["scheduled_available_member_ids"] == member_ids
    assert plan["duration_minutes"] == 120


def test_shorter_alternative_requires_confirmation(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one({"display_name": "Alex", "normalized_name": "alex"}).inserted_id
    group_id = make_group(database, member_ids=[member_id])
    plan_id = make_plan(
        database,
        group_id,
        duration_minutes=120,
        matches=[matching_time([member_id], exact=False, duration=60)],
    )
    client = app.test_client()

    response = client.post(
        f"/plans/{plan_id}/schedule", data={"match_index": "0"}, follow_redirects=True
    )
    assert b"Confirm the shorter duration" in response.data
    assert database.plans.find_one({"_id": plan_id})["status"] == "planning"

    response = client.post(
        f"/plans/{plan_id}/schedule",
        data={"match_index": "0", "confirm_shorter": "yes"},
        follow_redirects=True,
    )
    plan = database.plans.find_one({"_id": plan_id})
    assert response.status_code == 200
    assert b"Shorter option selected" in response.data
    assert plan["status"] == "scheduled"
    assert plan["scheduled_is_shorter"] is True
    assert plan["scheduled_available_duration_minutes"] == 60


def test_edit_plan_persists_values_and_resets_scheduled_plan(app_and_db):
    app, database = app_and_db
    member_ids = [
        database.users.insert_one({"display_name": name, "normalized_name": name.lower()}).inserted_id
        for name in ("Alex", "Sam", "Chris")
    ]
    group_id = make_group(database, member_ids=member_ids)
    plan_id = make_plan(
        database,
        group_id,
        status="scheduled",
        matches=[matching_time(member_ids)],
        scheduled_start=datetime(2026, 10, 23, 19, 0),
        scheduled_end=datetime(2026, 10, 23, 21, 0),
        scheduled_available_member_ids=member_ids,
        scheduled_attendance_count=3,
        scheduled_total_members=3,
    )

    response = app.test_client().post(f"/plans/{plan_id}/edit", data={
        "title": "Updated Movie Night",
        "start_date": "2026-10-24",
        "end_date": "2026-10-26",
        "duration_minutes": "90",
        "earliest_time": "18:00",
        "latest_time": "22:00",
        "minimum_attendance": "2",
    })
    plan = database.plans.find_one({"_id": plan_id})

    assert response.status_code == 302
    assert plan["title"] == "Updated Movie Night"
    assert plan["start_date"] == "2026-10-24"
    assert plan["end_date"] == "2026-10-26"
    assert plan["duration_minutes"] == 90
    assert plan["minimum_attendance"] == 2
    assert plan["earliest_time"] == "18:00"
    assert plan["latest_time"] == "22:00"
    assert plan["status"] == "planning"
    assert plan["matches"] == []
    assert "scheduled_start" not in plan
    assert "scheduled_end" not in plan


def test_plan_edit_form_is_prepopulated_and_title_only_edit_keeps_schedule(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one({"display_name": "Alex", "normalized_name": "alex"}).inserted_id
    group_id = make_group(database, member_ids=[member_id])
    scheduled_start = datetime(2026, 10, 23, 19, 0)
    plan_id = make_plan(
        database, group_id, status="scheduled", scheduled_start=scheduled_start,
        scheduled_end=datetime(2026, 10, 23, 21, 0),
    )
    client = app.test_client()

    page = client.get(f"/plans/{plan_id}/edit")
    assert b'value="Movie Night"' in page.data
    assert b'value="120"' in page.data
    assert b'value="2026-10-23"' in page.data

    client.post(f"/plans/{plan_id}/edit", data={
        "title": "Renamed Movie Night",
        "start_date": "2026-10-23",
        "end_date": "2026-10-24",
        "duration_minutes": "120",
        "earliest_time": "17:00",
        "latest_time": "23:00",
        "minimum_attendance": "1",
    })
    plan = database.plans.find_one({"_id": plan_id})
    assert plan["status"] == "scheduled"
    assert plan["scheduled_start"] == scheduled_start


def test_back_navigation_targets_parent_pages(app_and_db):
    app, database = app_and_db
    group_id = make_group(database)
    plan_id = make_plan(database, group_id)
    client = app.test_client()

    group_page = client.get(f"/groups/{group_id}").get_data(as_text=True)
    plan_page = client.get(f"/plans/{plan_id}").get_data(as_text=True)

    assert 'href="/"' in group_page
    assert "Back to Dashboard" in group_page
    assert f'href="/groups/{group_id}"' in plan_page
    assert "Back to Friday Friends" in plan_page


def test_group_availability_matrix_renders_members_windows_and_empty_cells(app_and_db):
    app, database = app_and_db
    member_ids = {}
    for name in ("Alex", "Sam", "Chris"):
        member_ids[name] = database.users.insert_one(
            {"display_name": name, "normalized_name": name.lower()}
        ).inserted_id
    group_id = make_group(database, member_ids=list(member_ids.values()))
    database.availability.insert_many([
        {"user_id": member_ids["Alex"], "day": "Monday", "day_order": 0,
         "start_time": "17:00", "end_time": "21:00", "status": "available"},
        {"user_id": member_ids["Sam"], "day": "Monday", "day_order": 0,
         "start_time": "09:00", "end_time": "12:00", "status": "available"},
        {"user_id": member_ids["Sam"], "day": "Monday", "day_order": 0,
         "start_time": "16:00", "end_time": "20:00", "status": "available"},
    ])

    page = app.test_client().get(f"/groups/{group_id}").get_data(as_text=True)

    assert "Weekly Availability" in page
    assert "Recurring availability shown below" in page
    assert all(name in page for name in member_ids)
    assert "5:00 PM–9:00 PM" in page
    assert "9:00 AM–12:00 PM" in page
    assert "4:00 PM–8:00 PM" in page
    assert "—" in page
    assert page.count("Edit availability") >= 3


def test_plan_actions_are_ordered_for_planning_and_scheduled_states(app_and_db):
    app, database = app_and_db
    group_id = make_group(database)
    plan_id = make_plan(database, group_id)
    client = app.test_client()

    planning_page = client.get(f"/plans/{plan_id}").get_data(as_text=True)
    assert planning_page.index("Edit Plan") < planning_page.index("Find Matching Times")
    assert planning_page.index("Find Matching Times") < planning_page.index("Delete Plan")

    database.plans.update_one({"_id": plan_id}, {"$set": {
        "status": "scheduled",
        "scheduled_start": datetime(2026, 10, 23, 19, 0),
        "scheduled_end": datetime(2026, 10, 23, 21, 0),
        "scheduled_available_member_ids": [],
        "scheduled_attendance_count": 0,
        "scheduled_total_members": 0,
    }})
    scheduled_page = client.get(f"/plans/{plan_id}").get_data(as_text=True)
    assert scheduled_page.index("Edit Plan") < scheduled_page.index("Reschedule")
    assert scheduled_page.index("Reschedule") < scheduled_page.index("Delete Plan")


def test_group_page_separates_planning_and_scheduled_plans_without_duplicates(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one({"display_name": "Alex", "normalized_name": "alex"}).inserted_id
    group_id = make_group(database, member_ids=[member_id])
    make_plan(database, group_id, title="Needs A Time")
    make_plan(
        database,
        group_id,
        title="Confirmed Event",
        status="scheduled",
        scheduled_start=datetime(2026, 10, 24, 19, 0),
        scheduled_end=datetime(2026, 10, 24, 21, 0),
        rsvps={},
    )

    page = app.test_client().get(f"/groups/{group_id}").get_data(as_text=True)

    assert "Scheduled Plans" in page
    assert "Planning" in page
    assert page.count("Confirmed Event") == 1
    assert page.count("Needs A Time") == 1
    assert page.index("Confirmed Event") < page.index("Needs A Time")


def test_scheduled_plans_are_sorted_chronologically(app_and_db):
    app, database = app_and_db
    group_id = make_group(database)
    make_plan(
        database, group_id, title="Later Event", status="scheduled",
        scheduled_start=datetime(2026, 10, 26, 19, 0),
        scheduled_end=datetime(2026, 10, 26, 21, 0),
    )
    make_plan(
        database, group_id, title="Sooner Event", status="scheduled",
        scheduled_start=datetime(2026, 10, 24, 19, 0),
        scheduled_end=datetime(2026, 10, 24, 21, 0),
    )

    page = app.test_client().get(f"/groups/{group_id}").get_data(as_text=True)
    assert page.index("Sooner Event") < page.index("Later Event")


def test_edit_group_preserves_members_plans_id_and_created_at(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one({"display_name": "Alex", "normalized_name": "alex"}).inserted_id
    created_at = datetime.now(timezone.utc)
    group_id = database.groups.insert_one({
        "name": "Old Name", "description": "Old description",
        "member_ids": [member_id], "created_at": created_at,
    }).inserted_id
    persisted_created_at = database.groups.find_one({"_id": group_id})["created_at"]
    plan_id = make_plan(database, group_id)

    form_page = app.test_client().get(f"/groups/{group_id}/edit").get_data(as_text=True)
    assert 'value="Old Name"' in form_page
    assert "Old description" in form_page

    response = app.test_client().post(f"/groups/{group_id}/edit", data={
        "name": "Friday Crew", "description": "Updated description",
    })
    group = database.groups.find_one({"_id": group_id})

    assert response.status_code == 302
    assert group["name"] == "Friday Crew"
    assert group["description"] == "Updated description"
    assert group["member_ids"] == [member_id]
    assert group["created_at"] == persisted_created_at
    assert database.plans.find_one({"_id": plan_id})["group_id"] == group_id


def test_edit_group_rejects_blank_name(app_and_db):
    app, database = app_and_db
    group_id = make_group(database, name="Keep This Name")

    response = app.test_client().post(
        f"/groups/{group_id}/edit", data={"name": "  ", "description": "Changed"}
    )

    assert response.status_code == 400
    assert database.groups.find_one({"_id": group_id})["name"] == "Keep This Name"


def make_scheduled_plan_with_members(database):
    members = {}
    for name in ("Alex", "Jamie", "Chris", "Taylor"):
        members[name] = database.users.insert_one(
            {"display_name": name, "normalized_name": name.lower()}
        ).inserted_id
    group_id = make_group(database, member_ids=list(members.values()))
    plan_id = make_plan(
        database,
        group_id,
        status="scheduled",
        scheduled_start=datetime(2026, 10, 23, 19, 0),
        scheduled_end=datetime(2026, 10, 23, 22, 0),
        rsvps={},
    )
    return group_id, plan_id, members


@pytest.mark.parametrize("status", ["attending", "not_attending"])
def test_basic_rsvp_responses_persist(app_and_db, status):
    app, database = app_and_db
    _group_id, plan_id, members = make_scheduled_plan_with_members(database)

    response = app.test_client().post(
        f"/plans/{plan_id}/rsvp",
        data={"member_id": str(members["Alex"]), "status": status},
    )

    plan = database.plans.find_one({"_id": plan_id})
    assert response.status_code == 302
    assert plan["rsvps"][str(members["Alex"])]["status"] == status


@pytest.mark.parametrize(
    ("status", "response_time"),
    [("arriving_late", "20:00"), ("leaving_early", "21:30")],
)
def test_timed_rsvp_responses_persist(app_and_db, status, response_time):
    app, database = app_and_db
    _group_id, plan_id, members = make_scheduled_plan_with_members(database)

    response = app.test_client().post(
        f"/plans/{plan_id}/rsvp",
        data={
            "member_id": str(members["Jamie"]),
            "status": status,
            "response_time": response_time,
        },
    )

    saved = database.plans.find_one({"_id": plan_id})["rsvps"][str(members["Jamie"])]
    assert response.status_code == 302
    assert saved["status"] == status
    assert saved["time"] == response_time


@pytest.mark.parametrize(
    ("status", "response_time", "message"),
    [
        ("arriving_late", "18:30", b"Arrival time must be after the start"),
        ("leaving_early", "22:30", b"Departure time must be no earlier"),
    ],
)
def test_invalid_timed_rsvp_is_rejected(app_and_db, status, response_time, message):
    app, database = app_and_db
    _group_id, plan_id, members = make_scheduled_plan_with_members(database)

    response = app.test_client().post(
        f"/plans/{plan_id}/rsvp",
        data={
            "member_id": str(members["Chris"]),
            "status": status,
            "response_time": response_time,
        },
        follow_redirects=True,
    )

    assert message in response.data
    assert str(members["Chris"]) not in database.plans.find_one({"_id": plan_id})["rsvps"]


def test_rsvp_update_replaces_member_response_without_duplicate(app_and_db):
    app, database = app_and_db
    _group_id, plan_id, members = make_scheduled_plan_with_members(database)
    client = app.test_client()
    member_id = str(members["Taylor"])

    client.post(f"/plans/{plan_id}/rsvp", data={"member_id": member_id, "status": "attending"})
    client.post(
        f"/plans/{plan_id}/rsvp",
        data={"member_id": member_id, "status": "arriving_late", "response_time": "20:30"},
    )
    plan = database.plans.find_one({"_id": plan_id})

    assert len(plan["rsvps"]) == 1
    assert plan["rsvps"][member_id]["status"] == "arriving_late"
    assert plan["rsvps"][member_id]["time"] == "20:30"


def test_rsvp_attendance_display_and_counts(app_and_db):
    app, database = app_and_db
    _group_id, plan_id, members = make_scheduled_plan_with_members(database)
    database.plans.update_one({"_id": plan_id}, {"$set": {"rsvps": {
        str(members["Alex"]): {"status": "attending"},
        str(members["Jamie"]): {"status": "arriving_late", "time": "20:00"},
        str(members["Chris"]): {"status": "not_attending"},
    }}})

    page = app.test_client().get(f"/plans/{plan_id}").get_data(as_text=True)

    assert "2/4 attending" in page
    assert "✓ Attending" in page
    assert "◷ Arriving at 8:00 PM" in page
    assert "✕ Not Attending" in page
    assert "— No Response" in page


def add_preview_availability(database, member_id, start, end):
    database.availability.insert_one({
        "user_id": member_id,
        "day": "Friday",
        "day_order": 4,
        "start_time": start,
        "end_time": end,
        "status": "available",
    })


def test_schedule_preview_returns_matches_without_creating_plan(app_and_db):
    app, database = app_and_db
    member_ids = [
        database.users.insert_one({"display_name": name, "normalized_name": name.lower()}).inserted_id
        for name in ("Alex", "Sam", "Chris")
    ]
    group_id = make_group(database, member_ids=member_ids)
    for member_id, times in zip(member_ids, [("18:00", "22:00"), ("18:00", "20:00"), ("19:00", "22:00")]):
        add_preview_availability(database, member_id, *times)
    client = app.test_client()
    plan_count = database.plans.count_documents({})

    base_data = {
        "start_date": "2026-10-23", "end_date": "2026-10-23",
        "duration_minutes": "60", "minimum_attendance": "3",
        "earliest_time": "17:00", "latest_time": "23:00",
    }
    response = client.post(f"/groups/{group_id}/schedule-preview", data=base_data)
    first = response.get_json()

    assert response.status_code == 200
    assert first["success"] is True
    assert first["results"]
    assert first["results"][0]["attendance"] == 3
    assert database.plans.count_documents({}) == plan_count

    longer = client.post(
        f"/groups/{group_id}/schedule-preview",
        data={**base_data, "duration_minutes": "180"},
    ).get_json()
    lower_attendance = client.post(
        f"/groups/{group_id}/schedule-preview",
        data={**base_data, "duration_minutes": "120", "minimum_attendance": "2"},
    ).get_json()
    assert longer["results"] != first["results"]
    assert lower_attendance["results"] != first["results"]
    assert database.plans.count_documents({}) == plan_count


def test_schedule_preview_invalid_input_is_safe_and_creates_nothing(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one({"display_name": "Alex", "normalized_name": "alex"}).inserted_id
    group_id = make_group(database, member_ids=[member_id])

    response = app.test_client().post(
        f"/groups/{group_id}/schedule-preview",
        data={"start_date": "", "duration_minutes": "0", "minimum_attendance": "2"},
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False
    assert database.plans.count_documents({}) == 0


def test_plan_creation_and_opening_generate_matches_automatically(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one({"display_name": "Alex", "normalized_name": "alex"}).inserted_id
    group_id = make_group(database, member_ids=[member_id])
    add_preview_availability(database, member_id, "18:00", "22:00")
    client = app.test_client()

    client.post(f"/groups/{group_id}/plans/create", data={
        "title": "Automatic Match", "start_date": "2026-10-23", "end_date": "2026-10-23",
        "duration_minutes": "75", "minimum_attendance": "1",
        "earliest_time": "17:00", "latest_time": "23:00",
    })
    created = database.plans.find_one({"title": "Automatic Match"})
    assert created["matches"]
    assert created["matches"][0]["available_duration_minutes"] == 75

    database.plans.update_one(
        {"_id": created["_id"]},
        {"$set": {"matches": []}, "$unset": {"matches_generated_at": "", "match_type": ""}},
    )
    page = client.get(f"/plans/{created['_id']}").get_data(as_text=True)
    assert "Best Times" in page
    assert database.plans.find_one({"_id": created["_id"]})["matches"]


def test_dashboard_separates_upcoming_and_needs_planning_with_group_names(app_and_db):
    app, database = app_and_db
    group_id = make_group(database, name="College Friends")
    make_plan(database, group_id, title="Beach Trip")
    make_plan(
        database, group_id, title="Movie Night", status="scheduled",
        scheduled_start=datetime(2026, 10, 23, 19, 0),
        scheduled_end=datetime(2026, 10, 23, 21, 0), rsvps={},
    )

    page = app.test_client().get("/").get_data(as_text=True)

    assert "Upcoming" in page
    assert "Needs Planning" in page
    assert "Movie Night" in page
    assert "Beach Trip" in page
    assert page.count("College Friends") >= 2
