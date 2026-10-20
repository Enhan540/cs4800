"""Recurring weekly availability matching, independent from Flask and MongoDB."""

from collections import defaultdict
from datetime import date, datetime, timedelta

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def parse_date(value):
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_time(value):
    """Convert HH:MM into minutes after midnight."""
    if not value or len(value) != 5 or value[2] != ":":
        raise ValueError("Time must use HH:MM format.")
    hour, minute = (int(part) for part in value.split(":"))
    if hour not in range(24) or minute not in range(60):
        raise ValueError("Time must use HH:MM format.")
    return hour * 60 + minute


def format_time(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _merged_intervals(member_ids, availability_records):
    allowed_ids = {str(member_id) for member_id in member_ids}
    intervals = defaultdict(list)
    for record in availability_records:
        member_id = str(record.get("user_id", ""))
        day = record.get("day")
        if member_id not in allowed_ids or day not in DAYS or record.get("status", "available") != "available":
            continue
        try:
            start = parse_time(record.get("start_time"))
            end = parse_time(record.get("end_time"))
        except (TypeError, ValueError):
            continue
        if end > start:
            intervals[(member_id, day)].append((start, end))

    for key, blocks in intervals.items():
        merged = []
        for start, end in sorted(blocks):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        intervals[key] = merged
    return intervals


def _make_result(current, start, end, available_ids, all_members, requested_duration, is_exact):
    available_set = set(available_ids)
    return {
        "date": current.isoformat(),
        "day": current.strftime("%A"),
        "start_time": format_time(start),
        "end_time": format_time(end),
        "available_duration_minutes": end - start,
        "requested_duration_minutes": requested_duration,
        "is_exact": is_exact,
        "attendance_count": len(available_ids),
        "total_members": len(all_members),
        "available_member_ids": available_ids,
        "unavailable_member_ids": [member for member in all_members if member not in available_set],
    }


def _effective_blocks(member_id, current, intervals, busy_intervals_by_member):
    """Return recurring daily blocks minus cached calendar busy time."""
    blocks = list(intervals.get((member_id, current.strftime("%A")), []))
    busy = busy_intervals_by_member.get(str(member_id), []) if busy_intervals_by_member else []
    for interval in busy:
        try:
            busy_start = interval["start"]
            busy_end = interval["end"]
            if isinstance(busy_start, str):
                busy_start = datetime.fromisoformat(busy_start.replace("Z", "+00:00")).replace(tzinfo=None)
            if isinstance(busy_end, str):
                busy_end = datetime.fromisoformat(busy_end.replace("Z", "+00:00")).replace(tzinfo=None)
            day_start = datetime.combine(current, datetime.min.time())
            start = max(0, int((busy_start - day_start).total_seconds() // 60))
            end = min(24 * 60, int((busy_end - day_start).total_seconds() // 60))
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        remaining = []
        for block_start, block_end in blocks:
            if end <= block_start or start >= block_end:
                remaining.append((block_start, block_end))
            else:
                if block_start < start:
                    remaining.append((block_start, start))
                if end < block_end:
                    remaining.append((end, block_end))
        blocks = remaining
    return blocks


def _exact_matches_for_day(
    current, members, intervals, earliest, latest, duration_minutes, minimum_attendance, increment_minutes,
    busy_intervals_by_member,
):
    day = current.strftime("%A")
    latest_start = latest - duration_minutes
    if latest_start < earliest:
        return []

    candidate_starts = set(range(earliest, latest_start + 1, increment_minutes))
    for member_id in members:
        for block_start, _block_end in _effective_blocks(member_id, current, intervals, busy_intervals_by_member):
            if earliest <= block_start <= latest_start:
                candidate_starts.add(block_start)

    matches = []
    for candidate_start in sorted(candidate_starts):
        candidate_end = candidate_start + duration_minutes
        available_ids = [
            member_id
            for member_id in members
            if any(
                block_start <= candidate_start and block_end >= candidate_end
                for block_start, block_end in _effective_blocks(member_id, current, intervals, busy_intervals_by_member)
            )
        ]
        if len(available_ids) >= minimum_attendance:
            matches.append(
                _make_result(
                    current, candidate_start, candidate_end, available_ids, members, duration_minutes, True
                )
            )
    return matches


def _alternative_matches_for_day(
    current, members, intervals, earliest, latest, duration_minutes, minimum_attendance, busy_intervals_by_member
):
    """Find maximal shorter windows for stable attendee sets on one weekday."""
    day = current.strftime("%A")
    candidate_starts = {earliest}
    for member_id in members:
        for block_start, _block_end in _effective_blocks(member_id, current, intervals, busy_intervals_by_member):
            if earliest <= block_start < latest:
                candidate_starts.add(block_start)

    matches = []
    seen = set()
    for candidate_start in sorted(candidate_starts):
        available_until = {}
        for member_id in members:
            ends = [
                min(block_end, latest)
                for block_start, block_end in _effective_blocks(member_id, current, intervals, busy_intervals_by_member)
                if block_start <= candidate_start < block_end and min(block_end, latest) > candidate_start
            ]
            if ends:
                available_until[member_id] = max(ends)

        if len(available_until) < minimum_attendance:
            continue

        for candidate_end in sorted(set(available_until.values()), reverse=True):
            available_ids = [
                member_id for member_id in members if available_until.get(member_id, -1) >= candidate_end
            ]
            available_duration = candidate_end - candidate_start
            if len(available_ids) < minimum_attendance or not 0 < available_duration < duration_minutes:
                continue
            key = (candidate_start, candidate_end, tuple(available_ids))
            if key in seen:
                continue
            seen.add(key)
            matches.append(
                _make_result(
                    current, candidate_start, candidate_end, available_ids, members, duration_minutes, False
                )
            )
    return matches


def find_matching_times(
    *, member_ids, availability_records, start_date, end_date, duration_minutes,
    earliest_time=None, latest_time=None, minimum_attendance=1, max_results=10,
    increment_minutes=30, busy_intervals_by_member=None,
):
    """Return full-duration matches, or the best shorter alternatives if none exist."""
    start = parse_date(start_date)
    end = parse_date(end_date)
    members = [str(member_id) for member_id in member_ids]
    if end < start:
        raise ValueError("End date must be on or after the start date.")
    if not isinstance(duration_minutes, int) or isinstance(duration_minutes, bool) or duration_minutes <= 0:
        raise ValueError("Meeting duration must be a positive number of minutes.")
    if minimum_attendance < 1 or minimum_attendance > len(members):
        raise ValueError("Minimum attendance must be between one and the group size.")
    if increment_minutes <= 0:
        raise ValueError("Search increment must be greater than zero.")

    earliest = parse_time(earliest_time) if earliest_time else 0
    latest = parse_time(latest_time) if latest_time else 24 * 60
    if latest <= earliest:
        raise ValueError("Latest allowed time must be later than earliest allowed time.")

    intervals = _merged_intervals(members, availability_records)
    exact_matches = []
    alternatives = []
    current = start
    while current <= end:
        exact_matches.extend(
            _exact_matches_for_day(
                current, members, intervals, earliest, latest, duration_minutes,
                minimum_attendance, increment_minutes, busy_intervals_by_member,
            )
        )
        alternatives.extend(
            _alternative_matches_for_day(
                current, members, intervals, earliest, latest, duration_minutes, minimum_attendance,
                busy_intervals_by_member,
            )
        )
        current += timedelta(days=1)

    results = exact_matches if exact_matches else alternatives
    results.sort(
        key=lambda match: (
            not match["is_exact"],
            -match["attendance_count"],
            -match["available_duration_minutes"],
            match["date"],
            match["start_time"],
        )
    )
    return results[:max_results]
