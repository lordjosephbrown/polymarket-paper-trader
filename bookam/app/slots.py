"""Pure slot computation — no side effects, no I/O."""
from __future__ import annotations

from datetime import datetime, timedelta

SLOT_STEP_MIN = 30


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def add_minutes(hhmm: str, minutes: int) -> str:
    return _to_hhmm(_to_minutes(hhmm) + minutes)


def compute_slots(
    open_time: str,
    close_time: str,
    duration_min: int,
    busy: list[tuple[str, str]],
    *,
    not_before: str | None = None,
    step_min: int = SLOT_STEP_MIN,
    capacity: int = 1,
) -> list[str]:
    """Available start times "HH:MM" for a service of duration_min.

    busy: list of (start, end) intervals already booked.
    not_before: exclude slots starting before this time (for today's date).
    capacity: concurrent bookings the calendar can hold (= staff head-count);
    a start is free while fewer than `capacity` busy intervals overlap it.
    """
    open_m = _to_minutes(open_time)
    close_m = _to_minutes(close_time)
    busy_m = [(_to_minutes(s), _to_minutes(e)) for s, e in busy]
    floor = _to_minutes(not_before) if not_before else open_m

    slots: list[str] = []
    start = open_m
    while start + duration_min <= close_m:
        end = start + duration_min
        overlapping = sum(1 for s, e in busy_m if s < end and start < e)
        if start >= floor and overlapping < capacity:
            slots.append(_to_hhmm(start))
        start += step_min
    return slots


def slots_for_date(
    open_time: str,
    close_time: str,
    duration_min: int,
    busy: list[tuple[str, str]],
    date_str: str,
    now: datetime,
    *,
    min_notice_min: int = 30,
    capacity: int = 1,
) -> list[str]:
    """Slots for a calendar date, excluding past times (plus notice period) when the date is today."""
    today = now.strftime("%Y-%m-%d")
    if date_str < today:
        return []
    not_before: str | None = None
    if date_str == today:
        cutoff = now + timedelta(minutes=min_notice_min)
        if cutoff.strftime("%Y-%m-%d") != today:
            return []  # notice period pushes past midnight
        not_before = cutoff.strftime("%H:%M")
    return compute_slots(
        open_time, close_time, duration_min, busy, not_before=not_before, capacity=capacity
    )
