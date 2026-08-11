"""Business + customer notifications, delivered over WhatsApp (outbox in demo mode)."""
from __future__ import annotations

import sqlite3

from .wa import WhatsAppClient


def _booking_context(conn: sqlite3.Connection, booking_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    booking = conn.execute(
        "SELECT b.*, s.name AS service_name, s.price_ghs FROM bookings b"
        " JOIN services s ON s.id = b.service_id WHERE b.id = ?",
        (booking_id,),
    ).fetchone()
    business = conn.execute(
        "SELECT * FROM businesses WHERE id = ?", (booking["business_id"],)
    ).fetchone()
    return booking, business


def booking_confirmed(conn: sqlite3.Connection, wa: WhatsAppClient, booking_id: int) -> None:
    """Tell the business a paid booking just landed."""
    booking, business = _booking_context(conn, booking_id)
    wa.send_text(
        conn,
        business["phone"],
        f"🎉 New booking: {booking['service_name']} on {booking['date']} at "
        f"{booking['start_time']} for {booking['customer_name']} "
        f"({booking['customer_phone']}). Deposit GHS {booking['deposit_ghs']:g} paid. "
        f"Ref {booking['payment_ref']}.",
    )


def booking_cancelled(conn: sqlite3.Connection, wa: WhatsAppClient, booking_id: int) -> None:
    """Tell the business a customer cancelled — the slot is free again."""
    booking, business = _booking_context(conn, booking_id)
    wa.send_text(
        conn,
        business["phone"],
        f"❌ Cancelled: {booking['service_name']} on {booking['date']} at "
        f"{booking['start_time']} ({booking['customer_name']}). The slot is open again.",
    )


def booking_reminder(conn: sqlite3.Connection, wa: WhatsAppClient, booking_id: int) -> None:
    """Remind the customer shortly before their appointment."""
    booking, business = _booking_context(conn, booking_id)
    wa.send_text(
        conn,
        booking["customer_phone"],
        f"⏰ Reminder: {booking['service_name']} with {business['name']} today at "
        f"{booking['start_time']}"
        + (f" ({business['location']})" if business["location"] else "")
        + f". Balance due: GHS {float(booking['price_ghs']) - float(booking['deposit_ghs']):g}.",
    )
