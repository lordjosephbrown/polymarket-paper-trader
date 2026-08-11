"""Shared availability lookup used by the web booking flow and the WhatsApp bot."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .slots import slots_for_date

MAX_DAYS_AHEAD = 30
PENDING_HOLD_MIN = 15


def now_utc() -> datetime:
    return datetime.now(timezone.utc)  # Ghana is UTC year-round


def available_slots(
    conn: sqlite3.Connection,
    business_id: int,
    location_id: int,
    duration_min: int,
    date_str: str,
    now: datetime | None = None,
) -> list[str]:
    """Free start times at one location. Each branch runs its own calendar;
    home-visit bookings occupy the calendar of the branch that dispatches them."""
    now = now or now_utc()
    day = datetime.strptime(date_str, "%Y-%m-%d")
    hours = conn.execute(
        "SELECT * FROM hours WHERE location_id = ? AND weekday = ?",
        (location_id, day.weekday()),
    ).fetchone()
    if hours is None:
        return []
    # Pending (unpaid) bookings hold their slot briefly, then lapse.
    pending_cutoff = (now - timedelta(minutes=PENDING_HOLD_MIN)).isoformat(timespec="seconds")
    busy_rows = conn.execute(
        "SELECT start_time, end_time FROM bookings"
        " WHERE business_id = ? AND location_id = ? AND date = ?"
        " AND (status = 'confirmed' OR (status = 'pending' AND created_at > ?))",
        (business_id, location_id, date_str, pending_cutoff),
    ).fetchall()
    busy = [(r["start_time"], r["end_time"]) for r in busy_rows]
    return slots_for_date(
        hours["open_time"], hours["close_time"], duration_min, busy, date_str, now
    )


def bookable_dates(
    conn: sqlite3.Connection,
    business_id: int,
    location_id: int,
    duration_min: int,
    days: int = 7,
    now: datetime | None = None,
) -> list[str]:
    """The next `days` calendar dates that have at least one free slot."""
    now = now or now_utc()
    result: list[str] = []
    for offset in range(0, MAX_DAYS_AHEAD + 1):
        date_str = (now + timedelta(days=offset)).strftime("%Y-%m-%d")
        if available_slots(conn, business_id, location_id, duration_min, date_str, now):
            result.append(date_str)
            if len(result) >= days:
                break
    return result
