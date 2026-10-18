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
