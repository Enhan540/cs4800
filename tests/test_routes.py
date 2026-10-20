import json
import os
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from bson import ObjectId

os.environ["MONGO_URI"] = ""
from app import create_app


@pytest.fixture
def app_and_db():
    database = mongomock.MongoClient().meetwise_test
    app = create_app(database_override=database)
    app.config.update(TESTING=True, SECRET_KEY="test-secret", LOGIN_DISABLED=True)
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
    assert scheduled_page.index("Edit Plan") < scheduled_page.index("Delete Plan")
    assert "Reschedule" not in scheduled_page
    assert "Schedule this time" not in scheduled_page


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

    assert "Upcoming Plans" in page
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
    ("modifier", "field_name", "response_time"),
    [("arriving_late", "arrival_time", "20:00"), ("leaving_early", "departure_time", "21:30")],
)
def test_timed_rsvp_responses_persist(app_and_db, modifier, field_name, response_time):
    app, database = app_and_db
    _group_id, plan_id, members = make_scheduled_plan_with_members(database)

    response = app.test_client().post(
        f"/plans/{plan_id}/rsvp",
        data={
            "member_id": str(members["Jamie"]),
            "status": "attending", modifier: "on", field_name: response_time,
        },
    )

    saved = database.plans.find_one({"_id": plan_id})["rsvps"][str(members["Jamie"])]
    assert response.status_code == 302
    assert saved["status"] == "attending"
    assert saved[modifier] is True
    assert saved[field_name] == response_time


@pytest.mark.parametrize(
    ("modifier", "field_name", "response_time", "message"),
    [
        ("arriving_late", "arrival_time", "18:30", b"Arrival time must be after the start"),
        ("leaving_early", "departure_time", "22:30", b"Leaving time must be after the start"),
    ],
)
def test_invalid_timed_rsvp_is_rejected(app_and_db, modifier, field_name, response_time, message):
    app, database = app_and_db
    _group_id, plan_id, members = make_scheduled_plan_with_members(database)

    response = app.test_client().post(
        f"/plans/{plan_id}/rsvp",
        data={
            "member_id": str(members["Chris"]),
                "status": "attending", modifier: "on", field_name: response_time,
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
        data={"member_id": member_id, "status": "attending", "arriving_late": "on", "arrival_time": "20:30"},
    )
    plan = database.plans.find_one({"_id": plan_id})

    assert len(plan["rsvps"]) == 1
    assert plan["rsvps"][member_id]["status"] == "attending"
    assert plan["rsvps"][member_id]["arriving_late"] is True
    assert plan["rsvps"][member_id]["arrival_time"] == "20:30"


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
    assert "Attending" in page
    assert "Arriving at 8:00 PM" in page
    assert "Not attending" in page
    assert "Awaiting response" in page


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


def test_dashboard_shows_upcoming_but_not_global_planning_with_group_names(app_and_db):
    app, database = app_and_db
    group_id = make_group(database, name="College Friends")
    make_plan(database, group_id, title="Beach Trip")
    make_plan(
        database, group_id, title="Movie Night", status="scheduled",
        scheduled_start=datetime(2026, 10, 23, 19, 0),
        scheduled_end=datetime(2026, 10, 23, 21, 0), rsvps={},
    )

    page = app.test_client().get("/").get_data(as_text=True)

    assert "Upcoming Plans" in page
    assert "Needs Planning" not in page
    assert "Movie Night" in page
    assert "Beach Trip" not in page
    assert page.count("College Friends") >= 2


def make_six_to_ten_plan(database):
    member_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id
    group_id = make_group(database, member_ids=[member_id])
    plan_id = make_plan(
        database,
        group_id,
        status="scheduled",
        scheduled_start=datetime(2026, 10, 23, 18, 0),
        scheduled_end=datetime(2026, 10, 23, 22, 0),
        invited_member_ids=[member_id],
        rsvps={},
    )
    return group_id, plan_id, member_id


@pytest.mark.parametrize(
    ("response_time", "accepted"),
    [("17:45", False), ("18:30", True), ("21:30", True), ("22:15", False)],
)
def test_arrival_time_must_be_strictly_inside_event(app_and_db, response_time, accepted):
    app, database = app_and_db
    _group_id, plan_id, member_id = make_six_to_ten_plan(database)

    app.test_client().post(f"/plans/{plan_id}/rsvp", data={
        "member_id": str(member_id),
        "status": "attending", "arriving_late": "on",
        "arrival_time": response_time,
    })
    response = database.plans.find_one({"_id": plan_id})["rsvps"].get(str(member_id))

    assert (response is not None) is accepted
    if accepted:
        assert response["arrival_time"] == response_time


@pytest.mark.parametrize(
    ("response_time", "accepted"),
    [("17:45", False), ("19:00", True), ("22:15", False)],
)
def test_departure_time_must_be_strictly_inside_event(app_and_db, response_time, accepted):
    app, database = app_and_db
    _group_id, plan_id, member_id = make_six_to_ten_plan(database)

    app.test_client().post(f"/plans/{plan_id}/rsvp", data={
        "member_id": str(member_id),
        "status": "attending", "leaving_early": "on",
        "departure_time": response_time,
    })
    response = database.plans.find_one({"_id": plan_id})["rsvps"].get(str(member_id))

    assert (response is not None) is accepted
    if accepted:
        assert response["departure_time"] == response_time


def test_combined_late_and_early_rsvp_validates_order_and_counts_as_attending(app_and_db):
    app, database = app_and_db
    _group_id, plan_id, member_id = make_six_to_ten_plan(database)
    client = app.test_client()

    response = client.post(f"/plans/{plan_id}/rsvp", data={
        "member_id": str(member_id),
        "status": "attending", "arriving_late": "on", "leaving_early": "on",
        "arrival_time": "19:00",
        "departure_time": "21:00",
    })
    saved = database.plans.find_one({"_id": plan_id})["rsvps"][str(member_id)]
    assert response.status_code == 302
    assert saved["arrival_time"] == "19:00"
    assert saved["departure_time"] == "21:00"

    page = client.get(f"/plans/{plan_id}").get_data(as_text=True)
    assert "1/1 attending" in page
    assert "7:00 PM – 9:00 PM" in page
    assert "Attending" in page

    invalid_plan_id = make_plan(
        database,
        database.plans.find_one({"_id": plan_id})["group_id"],
        status="scheduled",
        scheduled_start=datetime(2026, 10, 23, 18, 0),
        scheduled_end=datetime(2026, 10, 23, 22, 0),
        invited_member_ids=[member_id],
        rsvps={},
    )
    invalid = client.post(
        f"/plans/{invalid_plan_id}/rsvp",
        data={
            "member_id": str(member_id),
            "status": "attending", "arriving_late": "on", "leaving_early": "on",
            "arrival_time": "21:00",
            "departure_time": "20:00",
        },
        follow_redirects=True,
    )
    assert b"arrival before departure" in invalid.data
    assert database.plans.find_one({"_id": invalid_plan_id})["rsvps"] == {}


def test_rsvp_page_uses_unified_optional_five_minute_time_inputs(app_and_db):
    app, database = app_and_db
    _group_id, plan_id, _member_id = make_six_to_ten_plan(database)

    page = app.test_client().get(f"/plans/{plan_id}").get_data(as_text=True)

    assert 'name="arrival_time"' in page
    assert 'name="departure_time"' in page
    assert 'data-unified-time' in page
    assert 'data-picker-minute' in page
    assert '<option value="05">05</option>' in page
    assert 'type="time"' not in page


def test_scheduled_page_prioritizes_confirmed_event_and_hides_candidate_results(app_and_db):
    app, database = app_and_db
    group_id, plan_id, member_id = make_six_to_ten_plan(database)
    database.plans.update_one({"_id": plan_id}, {"$set": {
        "title": "Movie Night",
        "duration_minutes": 300,
        "matches": [matching_time([member_id])],
        "match_type": "exact",
    }})

    page = app.test_client().get(f"/plans/{plan_id}").get_data(as_text=True)

    assert page.index("Movie Night") < page.index("Friday, October 23, 2026")
    assert "6:00 PM – 10:00 PM" in page
    assert "· 4 hours" in page
    assert "Scheduling Requirements" in page
    assert "Best Times" not in page
    assert "Closest Available Options" not in page
    assert "Schedule this time" not in page
    assert f'href="/groups/{group_id}"' in page


def test_invite_existing_group_member_once(app_and_db):
    app, database = app_and_db
    invited_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id
    eligible_id = database.users.insert_one(
        {"display_name": "Sam", "normalized_name": "sam"}
    ).inserted_id
    group_id = make_group(database, member_ids=[invited_id, eligible_id])
    plan_id = make_plan(
        database,
        group_id,
        status="scheduled",
        scheduled_start=datetime(2026, 10, 23, 18, 0),
        scheduled_end=datetime(2026, 10, 23, 22, 0),
        invited_member_ids=[invited_id],
        rsvps={},
    )
    client = app.test_client()

    page = client.get(f"/plans/{plan_id}").get_data(as_text=True)
    assert "+ Invite Members" in page
    assert f'value="{eligible_id}"' in page

    client.post(f"/plans/{plan_id}/invite", data={"member_id": str(eligible_id)})
    client.post(f"/plans/{plan_id}/invite", data={"member_id": str(eligible_id)})
    plan = database.plans.find_one({"_id": plan_id})

    assert plan["invited_member_ids"].count(eligible_id) == 1
    refreshed = client.get(f"/plans/{plan_id}").get_data(as_text=True)
    assert "Sam" in refreshed
    assert "0/2 attending" in refreshed


def test_group_hierarchy_and_upcoming_count_exclude_past_and_planning(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id
    group_id = make_group(database, member_ids=[member_id])
    now = datetime.now()
    make_plan(database, group_id, title="Future Event", status="scheduled",
              scheduled_start=now + timedelta(days=2), scheduled_end=now + timedelta(days=2, hours=2))
    make_plan(database, group_id, title="Past Event", status="scheduled",
              scheduled_start=now - timedelta(days=2), scheduled_end=now - timedelta(days=2, hours=-2))
    make_plan(database, group_id, title="Still Planning")

    page = app.test_client().get(f"/groups/{group_id}").get_data(as_text=True)

    assert page.index("<h2>Upcoming Plans") < page.index("<h2>Planning")
    assert page.index("<h2>Planning") < page.index("<h2>Weekly Availability")
    assert page.index("<h2>Weekly Availability") < page.index("<h2>Members")
    assert "Future Event" in page
    assert "Past Event" not in page
    assert "1 upcoming plan" in page
    assert "Still Planning" in page
    assert f'href="/plans/' in page


def test_dashboard_has_clickable_groups_and_only_future_scheduled_plans(app_and_db):
    app, database = app_and_db
    group_id = make_group(database, name="Friday Friends")
    now = datetime.now()
    future_id = make_plan(
        database, group_id, title="Future Movie", status="scheduled",
        scheduled_start=now + timedelta(days=1), scheduled_end=now + timedelta(days=1, hours=2),
    )
    make_plan(
        database, group_id, title="Past Movie", status="scheduled",
        scheduled_start=now - timedelta(days=1), scheduled_end=now - timedelta(hours=1),
    )
    make_plan(database, group_id, title="Unconfirmed Movie")

    page = app.test_client().get("/").get_data(as_text=True)

    assert f'href="/groups/{group_id}"' in page
    assert f'href="/plans/{future_id}"' in page
    assert "Future Movie" in page
    assert "Past Movie" not in page
    assert "Unconfirmed Movie" not in page
    assert "Needs Planning" not in page


@pytest.mark.parametrize(
    ("start", "end", "expected_start", "expected_end"),
    [
        ((9, "00", "AM"), (10, "00", "AM"), "09:00", "10:00"),
        ((9, "05", "AM"), (10, "50", "AM"), "09:05", "10:50"),
        ((1, "35", "PM"), (4, "20", "PM"), "13:35", "16:20"),
        ((11, "00", "PM"), (11, "55", "PM"), "23:00", "23:55"),
        ((12, "00", "AM"), (12, "05", "AM"), "00:00", "00:05"),
        ((12, "00", "PM"), (1, "35", "PM"), "12:00", "13:35"),
    ],
)
def test_availability_picker_stores_normalized_five_minute_times(
    app_and_db, start, end, expected_start, expected_end
):
    app, database = app_and_db
    member_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id

    response = app.test_client().post(f"/users/{member_id}/availability", data={
        "day": "Monday",
        "start_hour": str(start[0]), "start_minute": start[1], "start_period": start[2],
        "end_hour": str(end[0]), "end_minute": end[1], "end_period": end[2],
    })
    saved = database.availability.find_one({"user_id": member_id})

    assert response.status_code == 302
    assert saved["start_time"] == expected_start
    assert saved["end_time"] == expected_end


@pytest.mark.parametrize(
    ("start", "end"),
    [((5, "00", "PM"), (5, "00", "PM")), ((11, "00", "PM"), (1, "00", "AM"))],
)
def test_availability_picker_rejects_invalid_same_day_ranges(app_and_db, start, end):
    app, database = app_and_db
    member_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id

    response = app.test_client().post(f"/users/{member_id}/availability", data={
        "day": "Monday",
        "start_hour": str(start[0]), "start_minute": start[1], "start_period": start[2],
        "end_hour": str(end[0]), "end_minute": end[1], "end_period": end[2],
    }, follow_redirects=True)

    assert response.status_code == 200
    assert b"End time must be later than start time." in response.data
    assert database.availability.count_documents({"user_id": member_id}) == 0


def test_availability_editor_has_weekdays_and_populates_edit_picker(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id
    database.availability.insert_one({
        "user_id": member_id, "day": "Monday", "day_order": 0,
        "start_time": "18:35", "end_time": "20:05", "status": "available",
    })

    page = app.test_client().get(f"/users/{member_id}/availability").get_data(as_text=True)

    assert all(f'data-availability-day="{day}"' in page for day in (
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
    ))
    assert 'value="06:35 PM"' in page
    assert 'value="08:05 PM"' in page


def test_availability_back_link_preserves_group_or_member_parent(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id
    group_id = make_group(database, member_ids=[member_id])
    client = app.test_client()

    group_page = client.get(f"/groups/{group_id}").get_data(as_text=True)
    availability_page = client.get(
        f"/users/{member_id}/availability?return_to=/groups/{group_id}"
    ).get_data(as_text=True)
    members_page = client.get(f"/users/{member_id}/availability").get_data(as_text=True)

    assert f'return_to=/groups/{group_id}' in group_page
    assert f'href="/groups/{group_id}"' in availability_page
    assert "Back to Friday Friends" in availability_page
    assert 'href="/users"' in members_page
    assert "Back to Members" in members_page


def test_group_planning_contains_create_plan_and_branding_is_updated(app_and_db):
    app, database = app_and_db
    group_id = make_group(database)
    page = app.test_client().get(f"/groups/{group_id}").get_data(as_text=True)
    dashboard = app.test_client().get("/").get_data(as_text=True)

    assert page.index("<h2>Planning") < page.index("+ Create Plan")
    assert page.index("Edit Group") < page.index("<h2>Planning")
    assert "Group Planner" in dashboard


def test_current_availability_shows_every_day_and_weekly_overview(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id
    database.availability.insert_many([
        {"user_id": member_id, "day": "Monday", "day_order": 0, "start_time": "09:05", "end_time": "10:50"},
        {"user_id": member_id, "day": "Thursday", "day_order": 3, "start_time": "14:30", "end_time": "17:45"},
    ])

    page = app.test_client().get(
        f"/users/{member_id}/availability?selected_day=Thursday"
    ).get_data(as_text=True)

    assert "Weekly Overview" in page
    assert "Current Availability" in page
    assert all(f"<h3>{day}</h3>" in page for day in (
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
    ))
    assert "9:05 AM – 10:50 AM" in page
    assert "2:30 PM – 5:45 PM" in page
    assert 'data-availability-bulk hidden' in page
    assert 'data-availability-edit>Edit<' in page


def test_add_availability_preserves_selected_day_and_time_values(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id

    response = app.test_client().post(f"/users/{member_id}/availability", data={
        "day": "Thursday", "selected_day": "Thursday",
        "start_hour": "2", "start_minute": "30", "start_period": "PM",
        "end_hour": "5", "end_minute": "45", "end_period": "PM",
    })

    assert "selected_day=Thursday" in response.headers["Location"]
    assert "start_time=14:30" in response.headers["Location"]
    assert "end_time=17:45" in response.headers["Location"]


def test_bulk_availability_edit_updates_and_removes_only_when_saved(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id
    first_id, second_id = [database.availability.insert_one({
        "user_id": member_id, "day": "Monday", "day_order": 0,
        "start_time": start, "end_time": end,
    }).inserted_id for start, end in (("09:00", "10:00"), ("14:00", "15:00"))]
    payload = [
        {"id": str(first_id), "start_time": "09:05", "end_time": "10:50", "deleted": False},
        {"id": str(second_id), "start_time": "14:00", "end_time": "15:00", "deleted": True},
    ]

    response = app.test_client().post(
        f"/users/{member_id}/availability/bulk-edit",
        data={"availability_payload": json.dumps(payload)},
    )

    assert response.status_code == 302
    assert database.availability.find_one({"_id": first_id})["start_time"] == "09:05"
    assert database.availability.find_one({"_id": first_id})["end_time"] == "10:50"
    assert database.availability.find_one({"_id": second_id}) is None


@pytest.mark.parametrize(
    ("start_time", "end_time", "stored_start", "stored_end"),
    [
        ("10:00 AM", "04:00 PM", "10:00", "16:00"),
        ("9:05 am", "10:50AM", "09:05", "10:50"),
        ("235pm", "11:55 PM", "14:35", "23:55"),
    ],
)
def test_unified_availability_time_input_accepts_valid_five_minute_times(
    app_and_db, start_time, end_time, stored_start, stored_end
):
    app, database = app_and_db
    member_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id

    response = app.test_client().post(f"/users/{member_id}/availability", data={
        "day": "Monday", "start_time": start_time, "end_time": end_time,
    })
    saved = database.availability.find_one({"user_id": member_id})

    assert response.status_code == 302
    assert (saved["start_time"], saved["end_time"]) == (stored_start, stored_end)


def test_unified_availability_time_input_rejects_non_five_minute_times(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id

    response = app.test_client().post(f"/users/{member_id}/availability", data={
        "day": "Monday", "start_time": "10:03 AM", "end_time": "04:00 PM",
    }, follow_redirects=True)

    assert b"Please enter a time in 5-minute increments." in response.data
    assert database.availability.count_documents({"user_id": member_id}) == 0


def test_attendance_modifiers_are_optional_and_cleared_for_not_attending(app_and_db):
    app, database = app_and_db
    _group_id, plan_id, member_id = make_six_to_ten_plan(database)
    client = app.test_client()

    client.post(f"/plans/{plan_id}/rsvp", data={
        "member_id": str(member_id), "status": "attending",
        "arriving_late": "on", "leaving_early": "on",
    })
    partial = database.plans.find_one({"_id": plan_id})["rsvps"][str(member_id)]
    assert partial["arriving_late"] is True and partial["leaving_early"] is True
    assert "arrival_time" not in partial and "departure_time" not in partial

    client.post(f"/plans/{plan_id}/rsvp", data={
        "member_id": str(member_id), "status": "not_attending",
    })
    saved = database.plans.find_one({"_id": plan_id})["rsvps"][str(member_id)]
    assert saved["status"] == "not_attending"
    assert "arriving_late" not in saved and "arrival_time" not in saved


def test_availability_page_emphasizes_member_and_uses_input_triggered_picker(app_and_db):
    app, database = app_and_db
    member_id = database.users.insert_one(
        {"display_name": "Alex", "normalized_name": "alex"}
    ).inserted_id

    page = app.test_client().get(f"/users/{member_id}/availability").get_data(as_text=True)

    assert "<h1>Availability for Alex</h1>" in page
    assert "<h2>Recurring Weekly Availability</h2>" in page
    assert page.count("data-unified-time") >= 2
    assert "data-time-picker-trigger" not in page


def test_rsvp_page_uses_stable_details_and_action_regions(app_and_db):
    app, database = app_and_db
    _group_id, plan_id, _member_id = make_six_to_ten_plan(database)

    page = app.test_client().get(f"/plans/{plan_id}").get_data(as_text=True)

    assert "data-rsvp-modifiers" in page
    assert "data-rsvp-details" in page
    assert "rsvp-action" in page
    assert "data-time-picker-trigger" not in page
