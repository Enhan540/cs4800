"""Date-specific availability calculations built on recurring availability."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError



def parse_time(value):
    if not value or len(value) != 5 or value[2] != ":":
        raise ValueError("Time must use HH:MM format.")
    hour, minute = (int(part) for part in value.split(":"))
    if hour not in range(24) or minute not in range(60):
        raise ValueError("Time must use HH:MM format.")
    return hour * 60 + minute


def as_utc_naive(value):
    """Normalize stored/calendar values to comparable UTC-naive datetimes."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.min)
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


def timezone_info(value):
    if hasattr(value, "utcoffset"):
        return value
    try:
        return ZoneInfo(value or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Timezone must be a valid IANA timezone.") from exc


def calendar_value_as_local_naive(value, timezone_name="UTC"):
    """Convert a cached UTC calendar value to local wall-clock time once."""
    value = as_utc_naive(value)
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(
        timezone_info(timezone_name)
    ).replace(tzinfo=None)


def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(available, blocked):
    """Return available intervals after subtracting half-open blocked intervals."""
    remaining = merge_intervals(available)
    for blocked_start, blocked_end in merge_intervals(blocked):
        next_remaining = []
        for start, end in remaining:
            if blocked_end <= start or blocked_start >= end:
                next_remaining.append((start, end))
                continue
            if start < blocked_start:
                next_remaining.append((start, min(end, blocked_start)))
            if blocked_end < end:
                next_remaining.append((max(start, blocked_end), end))
        remaining = next_remaining
    return remaining


def _recurring_for_date(records, member_id, current):
    result = []
    member_key = str(member_id)
    day_name = current.strftime("%A")
    for record in records:
        if str(record.get("user_id", "")) != member_key or record.get("day") != day_name:
            continue
        try:
            start = parse_time(record.get("start_time"))
            end = parse_time(record.get("end_time"))
        except (TypeError, ValueError):
            continue
        if end > start:
            day_start = datetime.combine(current, time.min)
            result.append((day_start + timedelta(minutes=start), day_start + timedelta(minutes=end)))
    return merge_intervals(result)


def _day_clipped_conflicts(intervals, current, *, calendar_timezone=None):
    day_start = datetime.combine(current, time.min)
    day_end = day_start + timedelta(days=1)
    clipped = []
    for interval in intervals or ():
        raw_start = interval.get("start") if isinstance(interval, dict) else None
        raw_end = interval.get("end") if isinstance(interval, dict) else None
        if calendar_timezone is None:
            start = as_utc_naive(raw_start)
            end = as_utc_naive(raw_end)
        else:
            start = calendar_value_as_local_naive(raw_start, calendar_timezone)
            end = calendar_value_as_local_naive(raw_end, calendar_timezone)
        if not start or not end or end <= start or end <= day_start or start >= day_end:
            continue
        clipped.append((max(start, day_start), min(end, day_end)))
    return merge_intervals(clipped)


def effective_availability_for_date(
    records, member_id, current, *, calendar_busy_intervals=None, plan_intervals=None,
    timezone_name="UTC",
):
    """Map recurring windows to ``current`` and subtract private conflicts.

    The returned intervals are date-specific datetime pairs. ``records`` is never
    modified, and calendar conflicts contain no event metadata.
    """
    if isinstance(current, datetime):
        current = current.date()
    if not isinstance(current, date):
        raise TypeError("current must be a date or datetime")
    baseline = _recurring_for_date(records, member_id, current)
    blocked = _day_clipped_conflicts(
        calendar_busy_intervals, current, calendar_timezone=timezone_name
    )
    blocked.extend(_day_clipped_conflicts(plan_intervals, current))
    return subtract_intervals(baseline, blocked)


def availability_segments_for_date(
    records, member_id, current, *, calendar_busy_intervals=None, plan_intervals=None,
    timezone_name="UTC",
):
    """Return baseline segments marked only as available or conflicted."""
    if isinstance(current, datetime):
        current = current.date()
    baseline = _recurring_for_date(records, member_id, current)
    calendar_blocked = _day_clipped_conflicts(
        calendar_busy_intervals, current, calendar_timezone=timezone_name
    )
    plan_blocked = _day_clipped_conflicts(plan_intervals, current)
    blocked = calendar_blocked
    blocked = merge_intervals(blocked + plan_blocked)
    segments = []
    for start, end in baseline:
        points = {start, end}
        for blocked_start, blocked_end in blocked:
            if blocked_start < end and blocked_end > start:
                points.update((max(start, blocked_start), min(end, blocked_end)))
        ordered = sorted(points)
        for left, right in zip(ordered, ordered[1:]):
            if right > left:
                overlapping = [
                    source
                    for source, source_intervals in (
                        ("ICAL_CONFLICT", calendar_blocked),
                        ("PLAN_CONFLICT", plan_blocked),
                    )
                    if any(source_start < right and source_end > left for source_start, source_end in source_intervals)
                ]
                segments.append({
                    "start": left,
                    "end": right,
                    "conflicted": bool(overlapping),
                    "conflict_sources": overlapping,
                })
    return segments


def group_availability_counts(
    records, member_ids, dates, *, calendar_busy_by_member=None,
    plan_conflicts_by_member=None, start_minute=0, end_minute=1440,
    increment_minutes=30, timezone_name="UTC",
):
    """Return date-specific effective availability counts for a group heatmap.

    Each slot is evaluated against the same effective-availability calculation
    used by the scheduler. Calendar data is read only from the member-keyed
    mapping supplied by the caller; no cross-account fallback is performed.
    """
    if increment_minutes <= 0:
        raise ValueError("increment_minutes must be positive.")
    if start_minute < 0 or end_minute > 1440 or end_minute <= start_minute:
        raise ValueError("Invalid heatmap time range.")
    member_ids = [str(member_id) for member_id in member_ids]
    calendar_busy_by_member = calendar_busy_by_member or {}
    plan_conflicts_by_member = plan_conflicts_by_member or {}
    result = []
    for current in dates:
        effective_by_member = {
            member_id: effective_availability_for_date(
                records,
                member_id,
                current,
                calendar_busy_intervals=calendar_busy_by_member.get(member_id, []),
                plan_intervals=plan_conflicts_by_member.get(member_id, []),
                timezone_name=timezone_name,
            )
            for member_id in member_ids
        }
        slots = []
        minute = start_minute
        while minute < end_minute:
            slot_end = min(minute + increment_minutes, end_minute)
            available_ids = [
                member_id for member_id, intervals in effective_by_member.items()
                if any(
                    (start - datetime.combine(current, time.min)).total_seconds() / 60 <= minute
                    and (end - datetime.combine(current, time.min)).total_seconds() / 60 >= slot_end
                    for start, end in intervals
                )
            ]
            slots.append({
                "start_minutes": minute,
                "end_minutes": slot_end,
                "available_count": len(available_ids),
                "total_count": len(member_ids),
                "available_member_ids": available_ids,
                "intensity": (
                    "none" if not available_ids
                    else "low" if len(available_ids) * 2 <= len(member_ids)
                    else "medium" if len(available_ids) < len(member_ids)
                    else "high"
                ),
            })
            minute = slot_end
        result.append({"date": current.isoformat(), "slots": slots})
    return result


def merge_adjacent_availability_intervals(intervals):
    """Merge adjacent blocks only when their semantic availability is identical."""
    merged = []
    for interval in intervals:
        current = dict(interval)
        if not merged:
            merged.append(current)
            continue
        previous = merged[-1]
        previous_members = previous.get("available_member_ids")
        current_members = current.get("available_member_ids")
        same_members = (
            previous_members is None or current_members is None
            or set(previous_members) == set(current_members)
        )
        previous_day = previous.get("day")
        current_day = current.get("day")
        if previous_day is not None or current_day is not None:
            same_day = previous_day == current_day
        else:
            same_day = (
                not isinstance(previous.get("end"), datetime)
                or not isinstance(current.get("start"), datetime)
                or previous["end"].date() == current["start"].date()
            )
        if (
            same_day
            and previous.get("end") == current.get("start")
            and previous.get("available_count") == current.get("available_count")
            and previous.get("total_members") == current.get("total_members")
            and same_members
        ):
            previous["end"] = current["end"]
        else:
            merged.append(current)
    return merged


def group_availability_blocks(
    records, member_ids, dates, *, calendar_busy_by_member=None,
    plan_conflicts_by_member=None, start_minute=0, end_minute=1440,
    timezone_name="UTC",
):
    """Calculate exact effective group blocks, then merge them per day.

    The boundaries come from effective availability endpoints, so a 5-minute
    conflict remains a 5-minute boundary even though the graph displays a
    coarser 30-minute grid.
    """
    if start_minute < 0 or end_minute > 1440 or end_minute <= start_minute:
        raise ValueError("Invalid heatmap time range.")
    member_ids = [str(member_id) for member_id in member_ids]
    calendar_busy_by_member = calendar_busy_by_member or {}
    plan_conflicts_by_member = plan_conflicts_by_member or {}
    result = []
    for current in dates:
        day_start = datetime.combine(current, time.min)
        effective_by_member = {
            member_id: effective_availability_for_date(
                records,
                member_id,
                current,
                calendar_busy_intervals=calendar_busy_by_member.get(member_id, []),
                plan_intervals=plan_conflicts_by_member.get(member_id, []),
                timezone_name=timezone_name,
            )
            for member_id in member_ids
        }
        boundaries = {start_minute, end_minute}
        for intervals in effective_by_member.values():
            for start, end in intervals:
                left = max(start_minute, int((start - day_start).total_seconds() // 60))
                right = min(end_minute, int((end - day_start).total_seconds() // 60))
                if right > left:
                    boundaries.update((left, right))
        ordered = sorted(boundaries)
        blocks = []
        for left, right in zip(ordered, ordered[1:]):
            if right <= left:
                continue
            available_ids = [
                member_id for member_id, intervals in effective_by_member.items()
                if any(
                    int((start - day_start).total_seconds() // 60) <= left
                    and int((end - day_start).total_seconds() // 60) >= right
                    for start, end in intervals
                )
            ]
            if not available_ids:
                continue
            count = len(available_ids)
            blocks.append({
                "day": current.isoformat(),
                "start": left,
                "end": right,
                "start_minutes": left,
                "end_minutes": right,
                "duration_minutes": right - left,
                "available_count": count,
                "total_members": len(member_ids),
                "available_member_ids": available_ids,
                "intensity": (
                    "low" if count * 2 <= len(member_ids)
                    else "medium" if count < len(member_ids)
                    else "high"
                ),
            })
        result.append({"date": current.isoformat(), "blocks": merge_adjacent_availability_intervals(blocks)})
    return result


def effective_availability_records(records, busy_intervals):
    """Backward-compatible normalization for callers using the old helper."""
    return list(records), merge_intervals([
        (as_utc_naive(item.get("start")), as_utc_naive(item.get("end")))
        for item in busy_intervals or ()
        if isinstance(item, dict)
        and as_utc_naive(item.get("start"))
        and as_utc_naive(item.get("end"))
    ])
