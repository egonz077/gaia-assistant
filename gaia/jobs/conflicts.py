"""What today already contains, computed rather than inferred."""

from datetime import date as date_cls
from datetime import datetime, time, timedelta

# Hardcoded, deliberately. A users.working_hours column is the obvious next
# step and nothing needs it yet; inventing the column now would mean inventing
# a default for every existing row too.
WORK_START_HOUR = 9
WORK_END_HOUR = 18


def overlaps(intervals: list[tuple[datetime, datetime]]) -> list[tuple]:
    """Pairs that genuinely collide. Back-to-back is not a collision: a 10-11
    followed by an 11-12 is a normal day, and reporting it would make every
    busy day look broken."""
    out = []
    ordered = sorted(intervals)
    for i, (start, end) in enumerate(ordered):
        for other_start, other_end in ordered[i + 1:]:
            if other_start >= end:
                break
            out.append(((start, end), (other_start, other_end)))
    return out


def free_minutes(intervals, *, day: str, tz, start_hour: int = WORK_START_HOUR,
                 end_hour: int = WORK_END_HOUR) -> int:
    """Unbooked minutes inside the working day.

    Merged before subtracting, so two meetings that overlap each other are not
    counted twice -- otherwise a double-booked morning reports negative time
    and the digest says something absurd.
    """
    d = date_cls.fromisoformat(day)
    window_start = datetime.combine(d, time(start_hour), tzinfo=tz)
    window_end = datetime.combine(d, time(end_hour), tzinfo=tz)

    clipped = []
    for start, end in sorted(intervals):
        start, end = max(start, window_start), min(end, window_end)
        if start < end:
            clipped.append((start, end))

    merged: list[list[datetime]] = []
    for start, end in clipped:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    busy = sum((e - s for s, e in merged), timedelta())
    return int(((window_end - window_start) - busy).total_seconds() // 60)
