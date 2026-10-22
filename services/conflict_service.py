"""Reusable RSVP-aware scheduled-plan conflict calculations."""

from datetime import datetime, timezone


def as_utc_naive(value):
    """Return a comparable UTC-naive datetime for Mongo and scheduler values."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def intervals_overlap(start_a, end_a, start_b, end_b):
    """Return true only when two half-open intervals share actual time."""
    return bool(start_a and end_a and start_b and end_b and start_a < end_b and start_b < end_a)


def effective_plan_interval(plan, member_id):
    """Return a user's effective attendance interval and RSVP state."""
    start = as_utc_naive(plan.get("scheduled_start"))
    end = as_utc_naive(plan.get("scheduled_end"))
    if not start or not end or end <= start or plan.get("status") != "scheduled":
        return None

    response = (plan.get("rsvps") or {}).get(str(member_id), {})
    status = response.get("status", "pending")
    arriving_late = response.get("arriving_late", False) or status in {
        "arriving_late", "arriving_late_leaving_early"
    }
    leaving_early = response.get("leaving_early", False) or status in {
        "leaving_early", "arriving_late_leaving_early"
    }
    if status in {"arriving_late", "leaving_early", "arriving_late_leaving_early"}:
        status = "attending"
    if status == "no_response" or status not in {"attending", "not_attending", "pending"}:
        status = "pending"
    if status == "not_attending":
        return None

    effective_start, effective_end = start, end
    if status == "attending" and arriving_late and response.get("arrival_time"):
        try:
            arrival = datetime.strptime(response["arrival_time"], "%H:%M").replace(
                year=start.year, month=start.month, day=start.day
            )
            if start < arrival < end:
                effective_start = arrival
        except (TypeError, ValueError):
            pass
    if status == "attending" and leaving_early and response.get("departure_time"):
        try:
            departure = datetime.strptime(response["departure_time"], "%H:%M").replace(
                year=start.year, month=start.month, day=start.day
            )
            if start < departure < end:
                effective_end = departure
        except (TypeError, ValueError):
            pass
    if effective_end <= effective_start:
        return None
    return {
        "start": effective_start,
        "end": effective_end,
        "status": status,
        "partial": effective_start != start or effective_end != end,
    }
