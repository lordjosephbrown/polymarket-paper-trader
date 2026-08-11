"""Shared availability lookup used by the web booking flow and the WhatsApp bot."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .slots import slots_for_date

MAX_DAYS_AHEAD = 30
PENDING_HOLD_MIN = 15


def now_utc() -> datetime:
    return datetime.now(timezone.utc)  # Ghana is UTC year-round


def staff_count(conn: sqlite3.Connection, location_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM staff WHERE location_id = ? AND active = 1",
        (location_id,),
    ).fetchone()["n"]


def _busy_intervals(
    conn: sqlite3.Connection,
    business_id: int,
    location_id: int,
    date_str: str,
    now: datetime,
    staff_id: int | None,
    exclude_booking_id: int | None,
) -> list[tuple[str, str]]:
    from .db import ACTIVE_STATUSES

    # Pending (unpaid) bookings hold their slot briefly, then lapse.
    pending_cutoff = (now - timedelta(minutes=PENDING_HOLD_MIN)).isoformat(timespec="seconds")
    active_list = ", ".join(f"'{s}'" for s in ACTIVE_STATUSES)
    sql = (
        "SELECT start_time, end_time FROM bookings"
        " WHERE business_id = ? AND location_id = ? AND date = ?"
        f" AND (status IN ({active_list}) OR (status = 'pending' AND created_at > ?))"
        " AND id IS NOT ?"
    )
    params: list = [business_id, location_id, date_str, pending_cutoff, exclude_booking_id]
    if staff_id is not None:
        sql += " AND staff_id = ?"
        params.append(staff_id)
    return [(r["start_time"], r["end_time"]) for r in conn.execute(sql, params).fetchall()]


def available_slots(
    conn: sqlite3.Connection,
    business_id: int,
    location_id: int,
    duration_min: int,
    date_str: str,
    now: datetime | None = None,
    exclude_booking_id: int | None = None,
    staff_id: int | None = None,
) -> list[str]:
    """Free start times at one location. Each branch runs its own calendar;
    home-visit bookings occupy the calendar of the branch that dispatches them.

    Capacity: a solo business (no staff rows) holds one booking at a time; with
    staff, the location can hold as many overlapping bookings as it has active
    staff. Passing staff_id asks for one specific person's free times.

    exclude_booking_id ignores one booking — used when rescheduling it, so its
    own current slot doesn't count as busy.
    """
    now = now or now_utc()
    day = datetime.strptime(date_str, "%Y-%m-%d")
    hours = conn.execute(
        "SELECT * FROM hours WHERE location_id = ? AND weekday = ?",
        (location_id, day.weekday()),
    ).fetchone()
    if hours is None:
        return []
    busy = _busy_intervals(
        conn, business_id, location_id, date_str, now, staff_id, exclude_booking_id
    )
    capacity = 1 if staff_id is not None else max(1, staff_count(conn, location_id))
    return slots_for_date(
        hours["open_time"], hours["close_time"], duration_min, busy, date_str, now,
        capacity=capacity,
    )


def find_free_staff(
    conn: sqlite3.Connection,
    business_id: int,
    location_id: int,
    date_str: str,
    start_time: str,
    end_time: str,
    now: datetime | None = None,
) -> int | None:
    """Pick a staff member free for [start_time, end_time), or None for a
    solo business (no staff rows). Raises LookupError if all staff are busy."""
    staff_rows = conn.execute(
        "SELECT id FROM staff WHERE location_id = ? AND active = 1 ORDER BY id",
        (location_id,),
    ).fetchall()
    if not staff_rows:
        return None
    now = now or now_utc()
    for row in staff_rows:
        busy = _busy_intervals(conn, business_id, location_id, date_str, now, row["id"], None)
        if not any(s < end_time and start_time < e for s, e in busy):
            return row["id"]
    raise LookupError("no free staff for that slot")


def bookable_dates(
    conn: sqlite3.Connection,
    business_id: int,
    location_id: int,
    duration_min: int,
    days: int = 7,
    now: datetime | None = None,
    staff_id: int | None = None,
) -> list[str]:
    """The next `days` calendar dates that have at least one free slot."""
    now = now or now_utc()
    result: list[str] = []
    for offset in range(0, MAX_DAYS_AHEAD + 1):
        date_str = (now + timedelta(days=offset)).strftime("%Y-%m-%d")
        if available_slots(
            conn, business_id, location_id, duration_min, date_str, now, staff_id=staff_id
        ):
            result.append(date_str)
            if len(result) >= days:
                break
    return result
