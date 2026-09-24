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
    window_end_dt = now + timedelta(hours=REMINDER_WINDOW_HOURS)
    today = now.strftime("%Y-%m-%d")
    now_hhmm = now.strftime("%H:%M")
    window_end = window_end_dt.strftime("%H:%M")
    conn = dbmod.connect(db_path)
    try:
        if window_end_dt.strftime("%Y-%m-%d") == today:
            due = conn.execute(
                "SELECT id FROM bookings WHERE status = 'confirmed' AND reminded = 0"
                " AND date = ? AND start_time >= ? AND start_time <= ?",
                (today, now_hhmm, window_end),
            ).fetchall()
        else:
            # Window crosses midnight: late-tonight slots plus early-tomorrow slots.
            tomorrow = window_end_dt.strftime("%Y-%m-%d")
            due = conn.execute(
                "SELECT id FROM bookings WHERE status = 'confirmed' AND reminded = 0"
                " AND ((date = ? AND start_time >= ?) OR (date = ? AND start_time <= ?))",
                (today, now_hhmm, tomorrow, window_end),
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


def reconcile_pending(db_path: str, payments, wa: WhatsAppClient) -> int:
    """Resolve stuck 'pending' payments (Grok checklist: delayed callbacks,
    pending-forever states).

    A booking can sit pending if the customer paid but the callback and
    webhook were both lost, or if they abandoned checkout. Live mode only —
    demo verify() always succeeds, which would wrongly confirm abandoned
    checkouts. Verified-paid bookings confirm (with notifications); unpaid
    holds older than the pending window are cancelled and ledgered as expired.
    """
    from . import db as dbmod
    from .availability import PENDING_HOLD_MIN, now_utc
    from .notify import booking_confirmed

    if payments.demo:
        return 0
    now = now_utc()
    grace_start = (now - timedelta(minutes=3)).isoformat(timespec="seconds")
    day_ago = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    hold_cutoff = (now - timedelta(minutes=PENDING_HOLD_MIN)).isoformat(timespec="seconds")
    conn = dbmod.connect(db_path)
    resolved = 0
    try:
        stuck = conn.execute(
            "SELECT * FROM bookings WHERE status = 'pending'"
            " AND created_at < ? AND created_at > ?",
            (grace_start, day_ago),
        ).fetchall()
        for booking in stuck:
            try:
                paid = payments.verify(booking["payment_ref"])
            except Exception:
                continue  # provider hiccup — retry next scan
            if paid:
                cur = conn.execute(
                    "UPDATE bookings SET status = 'confirmed' WHERE id = ? AND status = 'pending'",
                    (booking["id"],),
                )
                conn.commit()
                if cur.rowcount == 1:
                    dbmod.log_payment_event(
                        conn, booking["payment_ref"], "verified", booking["deposit_ghs"]
                    )
                    dbmod.log_booking_event(conn, booking["id"], "pending", "confirmed", "system")
                    booking_confirmed(conn, wa, booking["id"])
                    resolved += 1
            elif booking["created_at"] < hold_cutoff:
                conn.execute(
                    "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                    (booking["id"],),
                )
                conn.commit()
                dbmod.log_payment_event(conn, booking["payment_ref"], "expired")
                dbmod.log_booking_event(conn, booking["id"], "pending", "cancelled", "system")
                resolved += 1
        return resolved
    finally:
        conn.close()


async def jobs_loop(db_path: str, payments, wa: WhatsAppClient) -> None:
    while True:
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
        try:
            scan_and_send(db_path, wa)
        except Exception:
            # The loop must survive transient DB/network hiccups.
            pass
        try:
            reconcile_pending(db_path, payments, wa)
        except Exception:
            pass
