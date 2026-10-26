from datetime import date, datetime, timezone

from services.availability_service import (
    effective_availability_for_date,
    group_availability_blocks,
    group_availability_counts,
    merge_adjacent_availability_intervals,
)


def records():
    return [{"user_id": "member", "day": "Friday", "start_time": "15:00", "end_time": "23:00"}]


def pairs(intervals):
    return [(item[0].strftime("%H:%M"), item[1].strftime("%H:%M")) for item in intervals]


def test_effective_availability_subtracts_multiple_calendar_conflicts():
    result = effective_availability_for_date(
        records(), "member", date(2026, 10, 30),
        calendar_busy_intervals=[
            {"start": "2026-10-30T18:00:00Z", "end": "2026-10-30T19:30:00Z"},
            {"start": "2026-10-30T21:00:00Z", "end": "2026-10-30T22:00:00Z"},
        ],
    )
    assert pairs(result) == [("15:00", "18:00"), ("19:30", "21:00"), ("22:00", "23:00")]


def test_effective_availability_handles_full_and_edge_overlaps():
    result = effective_availability_for_date(
        records(), "member", date(2026, 10, 30),
        calendar_busy_intervals=[
            {"start": "2026-10-30T14:00:00", "end": "2026-10-30T16:00:00"},
            {"start": "2026-10-30T22:00:00", "end": "2026-10-31T00:30:00"},
        ],
    )
    assert pairs(result) == [("16:00", "22:00")]


def test_effective_availability_handles_all_day_and_cross_midnight_conflicts():
    result = effective_availability_for_date(
        records(), "member", date(2026, 10, 30),
        calendar_busy_intervals=[
            {"start": date(2026, 10, 30), "end": date(2026, 10, 31)},
        ],
    )
    assert result == []

    result = effective_availability_for_date(
        records(), "member", date(2026, 10, 30),
        calendar_busy_intervals=[
            {"start": datetime(2026, 10, 29, 23, tzinfo=timezone.utc), "end": datetime(2026, 10, 30, 16, tzinfo=timezone.utc)},
        ],
    )
    assert pairs(result) == [("16:00", "23:00")]


def test_effective_availability_subtracts_group_plan_intervals_without_mutating_baseline():
    baseline = records()
    result = effective_availability_for_date(
        baseline, "member", date(2026, 10, 30),
        plan_intervals=[{"start": "2026-10-30T21:00:00", "end": "2026-10-30T22:00:00"}],
    )
    assert pairs(result) == [("15:00", "21:00"), ("22:00", "23:00")]
    assert baseline == records()


def test_effective_availability_without_ical_uses_recurring_baseline():
    result = effective_availability_for_date(records(), "member", date(2026, 10, 30))
    assert pairs(result) == [("15:00", "23:00")]


def test_group_counts_use_member_scoped_calendar_data_only():
    records = [
        {"user_id": "guest1", "day": "Monday", "start_time": "17:00", "end_time": "20:00"},
        {"user_id": "guest2", "day": "Monday", "start_time": "17:00", "end_time": "20:00"},
    ]
    result = group_availability_counts(
        records,
        ["guest1", "guest2"],
        [date(2026, 10, 26)],
        calendar_busy_by_member={
            "guest2": [{"start": "2026-10-26T18:00:00Z", "end": "2026-10-26T19:00:00Z"}],
        },
        start_minute=17 * 60,
        end_minute=20 * 60,
        increment_minutes=60,
    )
    assert [slot["available_count"] for slot in result[0]["slots"]] == [2, 1, 2]
    assert result[0]["slots"][0]["intensity"] == "high"
    assert result[0]["slots"][1]["intensity"] == "low"


def test_group_counts_subtract_plan_conflicts_without_mutating_recurring_records():
    records = [{"user_id": "member", "day": "Friday", "start_time": "15:00", "end_time": "23:00"}]
    result = group_availability_counts(
        records,
        ["member"],
        [date(2026, 10, 30)],
        plan_conflicts_by_member={
            "member": [{"start": "2026-10-30T18:00:00", "end": "2026-10-30T19:00:00"}],
        },
        start_minute=17 * 60,
        end_minute=20 * 60,
        increment_minutes=60,
    )
    assert [slot["available_count"] for slot in result[0]["slots"]] == [1, 0, 1]
    assert records[0]["start_time"] == "15:00"


def test_cached_utc_calendar_interval_is_converted_to_pacific_local_time_once():
    monday = [{
        "user_id": "member", "day": "Monday",
        "start_time": "08:00", "end_time": "12:00",
    }]
    result = effective_availability_for_date(
        monday,
        "member",
        date(2026, 10, 26),
        calendar_busy_intervals=[{
            "start": datetime(2026, 10, 26, 16),
            "end": datetime(2026, 10, 26, 16, 50),
        }],
        timezone_name="America/Los_Angeles",
    )
    assert pairs(result) == [("08:00", "09:00"), ("09:50", "12:00")]


def test_near_midnight_utc_storage_maps_back_to_correct_pacific_date():
    monday = [{
        "user_id": "member", "day": "Monday",
        "start_time": "22:00", "end_time": "23:59",
    }]
    result = effective_availability_for_date(
        monday,
        "member",
        date(2026, 10, 26),
        calendar_busy_intervals=[{
            "start": datetime(2026, 10, 27, 6, 30),
            "end": datetime(2026, 10, 27, 6, 50),
        }],
        timezone_name="America/Los_Angeles",
    )
    assert pairs(result) == [("22:00", "23:30"), ("23:50", "23:59")]


def test_merge_adjacent_intervals_merges_identical_semantic_blocks():
    intervals = [
        {"day": "Monday", "start": 240 + index * 15, "end": 255 + index * 15,
         "available_count": 5, "total_members": 6, "available_member_ids": ["a", "b"]}
        for index in range(4)
    ]
    merged = merge_adjacent_availability_intervals(intervals)
    assert len(merged) == 1
    assert merged[0]["start"] == 240
    assert merged[0]["end"] == 300


def test_merge_keeps_gaps_days_count_changes_and_member_set_changes_separate():
    intervals = [
        {"day": "Monday", "start": 240, "end": 270, "available_count": 5, "total_members": 6, "available_member_ids": ["a"]},
        {"day": "Monday", "start": 285, "end": 315, "available_count": 5, "total_members": 6, "available_member_ids": ["a"]},
        {"day": "Tuesday", "start": 315, "end": 345, "available_count": 5, "total_members": 6, "available_member_ids": ["a"]},
        {"day": "Tuesday", "start": 345, "end": 375, "available_count": 5, "total_members": 7, "available_member_ids": ["a"]},
        {"day": "Tuesday", "start": 375, "end": 405, "available_count": 5, "total_members": 6, "available_member_ids": ["b"]},
    ]
    merged = merge_adjacent_availability_intervals(intervals)
    assert len(merged) == 5


def test_group_blocks_merge_display_intervals_but_preserve_conflict_boundaries():
    records = [{"user_id": "a", "day": "Monday", "start_time": "08:00", "end_time": "12:00"}]
    result = group_availability_blocks(
        records, ["a"], [date(2026, 10, 26)],
        calendar_busy_by_member={"a": [{"start": "2026-10-26T16:45:00", "end": "2026-10-26T17:15:00"}]},
        start_minute=8 * 60, end_minute=12 * 60,
        timezone_name="America/Los_Angeles",
    )[0]["blocks"]
    assert [(item["start"], item["end"], item["available_count"]) for item in result] == [
        (480, 585, 1), (615, 720, 1),
    ]
