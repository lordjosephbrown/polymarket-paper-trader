"""Appointment reminders: a background loop that nudges customers before their slot."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from . import db as dbmod
from .availability import now_utc
from .notify import booking_reminder
from .wa import WhatsAppClient

REMINDER_WINDOW_HOURS = 2
SCAN_INTERVAL_SECONDS = 60


def scan_and_send(db_path: str, wa: WhatsAppClient) -> int:
    """Send reminders for confirmed bookings starting within the next 2 hours.

    Returns the number of reminders sent. Safe to call repeatedly — each
    booking is reminded at most once (the `reminded` flag).
    """
    now = now_utc()
    today = now.strftime("%Y-%m-%d")
    window_end = (now + timedelta(hours=REMINDER_WINDOW_HOURS)).strftime("%H:%M")
    now_hhmm = now.strftime("%H:%M")
    conn = dbmod.connect(db_path)
    try:
        due = conn.execute(
            "SELECT id FROM bookings WHERE status = 'confirmed' AND reminded = 0"
            " AND date = ? AND start_time >= ? AND start_time <= ?",
            (today, now_hhmm, window_end),
        ).fetchall()
        for row in due:
            claimed = conn.execute(
                "UPDATE bookings SET reminded = 1 WHERE id = ? AND reminded = 0", (row["id"],)
            )
            conn.commit()
            if claimed.rowcount == 1:
                booking_reminder(conn, wa, row["id"])
        return len(due)
    finally:
        conn.close()


async def reminder_loop(db_path: str, wa: WhatsAppClient) -> None:
    while True:
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
        try:
            scan_and_send(db_path, wa)
        except Exception:
            # The loop must survive transient DB/network hiccups.
            pass
