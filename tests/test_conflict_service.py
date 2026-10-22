from datetime import datetime

from services.conflict_service import effective_plan_interval, intervals_overlap


def scheduled_plan(start="2026-10-23T18:00:00", end="2026-10-23T20:00:00", response=None):
    return {
        "status": "scheduled",
        "scheduled_start": datetime.fromisoformat(start),
        "scheduled_end": datetime.fromisoformat(end),
        "rsvps": {"member": response} if response is not None else {},
    }


def test_intervals_overlap_uses_strict_boundaries():
    start = datetime(2026, 10, 23, 18)
    end = datetime(2026, 10, 23, 20)
    assert intervals_overlap(start, end, datetime(2026, 10, 23, 19), datetime(2026, 10, 23, 21))
    assert not intervals_overlap(start, end, end, datetime(2026, 10, 23, 22))


def test_not_attending_has_no_effective_interval():
    assert effective_plan_interval(scheduled_plan(response={"status": "not_attending"}), "member") is None


def test_pending_uses_full_interval_and_is_marked_pending():
    interval = effective_plan_interval(scheduled_plan(response={"status": "pending"}), "member")
    assert interval["status"] == "pending"
    assert interval["start"].hour == 18 and interval["end"].hour == 20


def test_partial_attendance_uses_exact_times():
    interval = effective_plan_interval(
        scheduled_plan(end="2026-10-23T22:00:00", response={
            "status": "attending",
            "arriving_late": True,
            "leaving_early": True,
            "arrival_time": "20:00",
            "departure_time": "21:00",
        }),
        "member",
    )
    assert (interval["start"].hour, interval["end"].hour) == (20, 21)
    assert interval["partial"] is True


def test_partial_flag_without_exact_time_is_conservative():
    interval = effective_plan_interval(
        scheduled_plan(response={"status": "attending", "arriving_late": True}), "member"
    )
    assert interval["start"].hour == 18
    assert interval["end"].hour == 20
