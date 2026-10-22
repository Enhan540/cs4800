import pytest

from services.scheduling_service import find_matching_times


def block(user, start, end, day="Friday"):
    return {"user_id": user, "day": day, "start_time": start, "end_time": end, "status": "available"}


def find(records, duration=60, minimum=3):
    return find_matching_times(
        member_ids=["A", "B", "C"], availability_records=records,
        start_date="2026-10-23", end_date="2026-10-23",
        duration_minutes=duration, minimum_attendance=minimum,
    )


def test_common_availability_for_three_members():
    matches = find([
        block("A", "18:00", "22:00"), block("B", "17:00", "20:00"), block("C", "19:00", "23:00")
    ])
    assert matches[0]["start_time"] == "19:00"
    assert matches[0]["end_time"] == "20:00"
    assert matches[0]["attendance_count"] == 3
    assert matches[0]["is_exact"] is True


@pytest.mark.parametrize("duration", [45, 75, 125, 180])
def test_arbitrary_positive_durations_are_supported(duration):
    records = [block(user, "17:00", "23:30") for user in ["A", "B", "C"]]
    matches = find(records, duration=duration)
    assert matches
    assert matches[0]["is_exact"] is True
    assert matches[0]["available_duration_minutes"] == duration


def test_no_overlap():
    records = [block("A", "09:00", "10:00"), block("B", "11:00", "12:00"), block("C", "13:00", "14:00")]
    assert find(records) == []


def test_minimum_attendance_less_than_group_size():
    records = [block("A", "18:00", "20:00"), block("B", "18:00", "20:00"), block("C", "21:00", "22:00")]
    matches = find(records, minimum=2)
    assert matches[0]["attendance_count"] == 2
    assert set(matches[0]["available_member_ids"]) == {"A", "B"}


def test_shorter_alternative_returned_when_requested_duration_is_too_long():
    records = [block(user, "18:00", "20:00") for user in ["A", "B", "C"]]
    matches = find(records, duration=180)
    assert matches
    assert matches[0]["is_exact"] is False
    assert matches[0]["start_time"] == "18:00"
    assert matches[0]["end_time"] == "20:00"
    assert matches[0]["available_duration_minutes"] == 120
    assert matches[0]["attendance_count"] == 3


def test_alternative_requires_minimum_attendance():
    records = [block("A", "18:00", "20:00"), block("B", "18:00", "20:00"), block("C", "21:00", "22:00")]
    assert find(records, duration=180, minimum=3) == []
    matches = find(records, duration=180, minimum=2)
    assert matches[0]["is_exact"] is False
    assert matches[0]["attendance_count"] == 2
    assert matches[0]["available_duration_minutes"] == 120


def test_overlapping_multiple_blocks_are_merged():
    records = [
        block("A", "18:00", "19:30"), block("A", "19:00", "21:00"),
        block("B", "18:30", "21:00"), block("C", "19:00", "21:00"),
    ]
    matches = find(records, duration=120)
    assert matches[0]["start_time"] == "19:00"
    assert matches[0]["end_time"] == "21:00"
    assert matches[0]["attendance_count"] == 3


@pytest.mark.parametrize("duration", [0, -5, 45.5, "60"])
def test_invalid_durations_are_rejected(duration):
    with pytest.raises(ValueError):
        find([], duration=duration)


def test_calendar_busy_interval_is_subtracted_from_recurring_availability():
    matches = find_matching_times(
        member_ids=["A"],
        availability_records=[block("A", "18:00", "21:00")],
        start_date="2026-10-23",
        end_date="2026-10-23",
        duration_minutes=60,
        minimum_attendance=1,
        busy_intervals_by_member={
            "A": [{"start": "2026-10-23T19:00:00", "end": "2026-10-23T20:00:00"}]
        },
    )
    assert [(match["start_time"], match["end_time"]) for match in matches] == [
        ("18:00", "19:00"),
        ("20:00", "21:00"),
    ]
