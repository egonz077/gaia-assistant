"""Interval arithmetic, computed in Python and never asked of the model.

today_line() exists because the model called 2026-09-11 'Friday' in one turn
and 'Thu' in the next. Reasoning over a day of overlapping events is strictly
harder than naming a weekday.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from gaia.jobs import conflicts

TZ = ZoneInfo("America/New_York")


def _at(h, m=0):
    return datetime(2026, 9, 16, h, m, tzinfo=TZ)


def test_detects_an_overlap():
    assert conflicts.overlaps([(_at(10), _at(11)), (_at(10, 30), _at(11, 30))])


def test_touching_events_do_not_overlap():
    """A 10-11 and an 11-12 are back to back, not double-booked. Reporting
    those as conflicts would make every full day look broken."""
    assert conflicts.overlaps([(_at(10), _at(11)), (_at(11), _at(12))]) == []


def test_free_minutes_subtracts_busy_blocks():
    # 9-18 is 540 minutes; one two-hour meeting leaves 420.
    assert conflicts.free_minutes([(_at(10), _at(12))], day="2026-09-16", tz=TZ) == 420


def test_free_minutes_ignores_time_outside_the_working_day():
    """A 7am flight is not working time, and counting it would report a day as
    fuller than it is."""
    assert conflicts.free_minutes([(_at(6), _at(8))], day="2026-09-16", tz=TZ) == 540


def test_overlapping_meetings_are_not_double_counted():
    assert conflicts.free_minutes(
        [(_at(10), _at(12)), (_at(11), _at(13))], day="2026-09-16", tz=TZ) == 360


def test_no_events_is_a_whole_free_day():
    assert conflicts.free_minutes([], day="2026-09-16", tz=TZ) == 540
