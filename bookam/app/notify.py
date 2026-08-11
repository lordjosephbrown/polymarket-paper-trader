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
    where = ""
    if booking["venue"] == "customer":
        where = f"\n🏠 Home visit: {booking['customer_address']}"
        if float(booking["travel_fee_ghs"]):
            where += f" (travel fee GHS {booking['travel_fee_ghs']:g} included)"
    wa.send_text(
        conn,
        business["phone"],
        f"🎉 New booking: {booking['service_name']} on {booking['date']} at "
        f"{booking['start_time']} for {booking['customer_name']} "
        f"({booking['customer_phone']}). Deposit GHS {booking['deposit_ghs']:g} paid. "
        f"Ref {booking['payment_ref']}.{where}",
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


STATUS_MESSAGES = {
    "on_the_way": "🚗 {business} is on the way to you for your {service} appointment!",
    "in_progress": "💈 Your {service} appointment with {business} has started.",
    "completed": "🌟 All done! Thanks for booking {service} with {business}. See you next time!",
    "no_show": "We're sorry we missed you for your {service} appointment with {business}.",
}


def delivery_status_update(
    conn: sqlite3.Connection, wa: WhatsAppClient, booking_id: int, new_status: str
) -> None:
    """Tell the customer their appointment moved through the delivery pipeline."""
    template = STATUS_MESSAGES.get(new_status)
    if template is None:
        return
    booking, business = _booking_context(conn, booking_id)
    wa.send_text(
        conn,
        booking["customer_phone"],
        template.format(business=business["name"], service=booking["service_name"]),
    )


def booking_rescheduled(
    conn: sqlite3.Connection, wa: WhatsAppClient, booking_id: int, old_date: str, old_time: str
) -> None:
    """Tell the customer the business moved their appointment."""
    booking, business = _booking_context(conn, booking_id)
    wa.send_text(
        conn,
        booking["customer_phone"],
        f"📅 Schedule change: {business['name']} moved your {booking['service_name']} "
        f"appointment from {old_date} {old_time} to *{booking['date']} at "
        f"{booking['start_time']}*. If the new time doesn't work, reply \"cancel\".",
    )


def booking_cancelled_by_business(
    conn: sqlite3.Connection, wa: WhatsAppClient, booking_id: int
) -> None:
    """Tell the customer the business cancelled — deposit must be sorted out."""
    booking, business = _booking_context(conn, booking_id)
    wa.send_text(
        conn,
        booking["customer_phone"],
        f"❌ {business['name']} had to cancel your {booking['service_name']} appointment "
        f"on {booking['date']} at {booking['start_time']}. They should refund your "
        f"GHS {booking['deposit_ghs']:g} deposit — contact them on "
        f"{business['phone']} to arrange it.",
    )


def broadcast_to_customer(
    conn: sqlite3.Connection, wa: WhatsAppClient, business: sqlite3.Row, phone: str, message: str
) -> None:
    wa.send_text(conn, phone, f"📣 Update from *{business['name']}*:\n{message}")


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
