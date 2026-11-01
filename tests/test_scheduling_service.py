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


def test_smart_plan_finder_uses_same_pacific_calendar_conversion_as_group_view():
    matches = find_matching_times(
        member_ids=["A"],
        availability_records=[block("A", "08:00", "12:00", day="Monday")],
        start_date="2026-10-26",
        end_date="2026-10-26",
        duration_minutes=15,
        minimum_attendance=1,
        increment_minutes=15,
        timezone_name="America/Los_Angeles",
        busy_intervals_by_member={
            "A": [{"start": "2026-10-26T16:00:00", "end": "2026-10-26T16:50:00"}],
        },
    )
    windows = {(match["start_time"], match["end_time"]) for match in matches}
    assert ("08:00", "08:15") in windows
    assert ("09:00", "09:15") not in windows
    assert ("09:50", "10:05") in windows


def test_best_result_favors_attendance_then_earliest_boundary():
    matches = find([
        block("A", "09:05", "11:00"),
        block("B", "09:10", "11:00"),
        block("C", "09:15", "11:00"),
    ], minimum=2)
    assert [(item["start_time"], item["attendance_count"]) for item in matches] == [
        ("09:15", 3), ("09:10", 2),
    ]


def test_shorter_results_favor_attendance_then_duration():
    matches = find([
        block("A", "09:00", "11:00"),
        block("B", "09:00", "10:00"),
        block("C", "09:00", "09:30"),
    ], duration=180, minimum=2)
    assert matches[0]["attendance_count"] == 3
    assert matches[0]["available_duration_minutes"] == 30
    assert matches[1]["attendance_count"] == 2
    assert matches[1]["available_duration_minutes"] == 60


def test_boundary_matcher_agrees_with_exhaustive_reference_for_optimum():
    from itertools import product

    starts = ("09:00", "09:05", "09:10")
    ends = ("09:30", "09:35", "09:40")
    for choices in product(product(starts, ends), repeat=3):
        records = [block(member, *window) for member, window in zip("ABC", choices)]
        results = find_matching_times(
            member_ids=list("ABC"), availability_records=records,
            start_date="2026-10-23", end_date="2026-10-23",
            duration_minutes=15, minimum_attendance=1,
            earliest_time="09:00", latest_time="09:40",
        )
        exhaustive = []
        for minute in range(9 * 60, 9 * 60 + 26, 5):
            count = sum(
                int(start[:2]) * 60 + int(start[3:]) <= minute
                and int(end[:2]) * 60 + int(end[3:]) >= minute + 15
                for start, end in choices
            )
            if count:
                exhaustive.append((count, minute))
        expected_count, expected_minute = max(exhaustive, key=lambda row: (row[0], -row[1]))
        actual = results[0]
        assert actual["attendance_count"] == expected_count
        assert actual["start_time"] == f"{expected_minute // 60:02d}:{expected_minute % 60:02d}"


def test_effective_availability_is_calculated_once_per_member_and_date(monkeypatch):
    import services.scheduling_service as scheduling

    original = scheduling.effective_availability_for_date
    calls = []

    def counted(*args, **kwargs):
        calls.append((str(args[1]), args[2]))
        return original(*args, **kwargs)

    monkeypatch.setattr(scheduling, "effective_availability_for_date", counted)
    find_matching_times(
        member_ids=["A", "B"],
        availability_records=[
            block("A", "09:00", "12:00"), block("B", "09:05", "12:00"),
        ],
        start_date="2026-10-23", end_date="2026-10-23",
        duration_minutes=30, minimum_attendance=1,
    )
    assert len(calls) == 2
    assert {member for member, _date in calls} == {"A", "B"}
