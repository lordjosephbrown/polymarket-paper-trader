"""WhatsApp booking bot — a conversation state machine.

Customers never leave WhatsApp: services, dates and times are presented as
native tap-to-select lists, and in live mode the deposit arrives as a MoMo
approval prompt on their phone (Paystack Charge API). The wa.me deep link a
business shares pre-fills "book <slug>" so the bot knows who they're booking.

States: await_service → await_date → await_time → await_name → await_payment.
Commands understood at any point: "book <slug>", "cancel", "menu"/"hi"/"start".
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from . import db as dbmod
from .availability import available_slots, bookable_dates, now_utc
from .config import Settings
from .notify import booking_cancelled, booking_confirmed
from .payments import PaymentError, PaymentProvider, momo_provider_from_phone, new_reference
from .slots import add_minutes
from .wa import WhatsAppClient, list_message

CONVERSATION_TTL_MIN = 60


def _load_conversation(conn: sqlite3.Connection, phone: str) -> tuple[str, dict, int | None]:
    row = conn.execute("SELECT * FROM conversations WHERE phone = ?", (phone,)).fetchone()
    if row is None:
        return "idle", {}, None
    cutoff = (now_utc() - timedelta(minutes=CONVERSATION_TTL_MIN)).isoformat(timespec="seconds")
    if row["updated_at"] < cutoff:
        return "idle", {}, None
    return row["state"], json.loads(row["data"]), row["business_id"]


def _save_conversation(
    conn: sqlite3.Connection, phone: str, state: str, data: dict, business_id: int | None
) -> None:
    conn.execute(
        "INSERT INTO conversations (phone, business_id, state, data, updated_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(phone) DO UPDATE SET business_id=excluded.business_id,"
        " state=excluded.state, data=excluded.data, updated_at=excluded.updated_at",
        (phone, business_id, state, json.dumps(data), dbmod.now_iso()),
    )
    conn.commit()


def _clear_conversation(conn: sqlite3.Connection, phone: str) -> None:
    conn.execute("DELETE FROM conversations WHERE phone = ?", (phone,))
    conn.commit()


def _weekday_label(date_str: str) -> str:
    day = datetime.strptime(date_str, "%Y-%m-%d")
    return day.strftime("%a %d %b")  # "Wed 13 Aug"


class Bot:
    def __init__(self, settings: Settings, payments: PaymentProvider, wa: WhatsAppClient) -> None:
        self.settings = settings
        self.payments = payments
        self.wa = wa

    # -- entry point -------------------------------------------------------

    def handle(self, conn: sqlite3.Connection, phone: str, text: str, profile_name: str = "") -> None:
        """Process one inbound message (plain text or an interactive reply id)."""
        text = text.strip()
        lower = text.lower()
        state, data, business_id = _load_conversation(conn, phone)

        if lower.startswith("book"):
            self._start_booking(conn, phone, lower)
            return
        if lower in {"cancel", "cancel booking"}:
            self._offer_cancellations(conn, phone)
            return
        if text.startswith("cxl:"):
            self._do_cancel(conn, phone, text[4:])
            return

        if state == "await_service" and text.startswith("svc:"):
            self._pick_service(conn, phone, data, business_id, text[4:])
        elif state == "await_date" and text.startswith("date:"):
            self._pick_date(conn, phone, data, business_id, text[5:])
        elif state == "await_time" and text.startswith("time:"):
            self._pick_time(conn, phone, data, business_id, text[5:])
        elif state == "await_name":
            self._pick_name(conn, phone, data, business_id, text or profile_name)
        else:
            self._greet(conn, phone)

    # -- steps -------------------------------------------------------------

    def _greet(self, conn: sqlite3.Connection, phone: str) -> None:
        self.wa.send_text(
            conn,
            phone,
            "👋 Welcome to Bookam! To book an appointment, tap the business's booking "
            "link, or send: book <business-code> (e.g. \"book adjoas-beauty-bar\"). "
            "Send \"cancel\" to cancel an upcoming booking.",
        )

    def _start_booking(self, conn: sqlite3.Connection, phone: str, lower_text: str) -> None:
        slug = lower_text.removeprefix("book").strip().lstrip(":").strip()
        if not slug:
            self._greet(conn, phone)
            return
        business = conn.execute("SELECT * FROM businesses WHERE slug = ?", (slug,)).fetchone()
        if business is None:
            self.wa.send_text(
                conn, phone, f"Hmm, I couldn't find \"{slug}\". Check the code and try again."
            )
            return
        services = conn.execute(
            "SELECT * FROM services WHERE business_id = ? AND active = 1 ORDER BY id",
            (business["id"],),
        ).fetchall()
        if not services:
            self.wa.send_text(
                conn, phone, f"{business['name']} hasn't listed any services yet. Check back soon!"
            )
            return
        rows = [
            {
                "id": f"svc:{s['id']}",
                "title": s["name"][:24],
                "description": f"GHS {s['price_ghs']:g} · {s['duration_min']} min · deposit GHS {s['deposit_ghs']:g}",
            }
            for s in services
        ]
        self.wa.send(
            conn,
            phone,
            list_message(
                f"Booking with *{business['name']}*"
                + (f" ({business['location']})" if business["location"] else "")
                + ". What would you like?",
                "Choose service",
                rows,
                header="Services",
            ),
        )
        _save_conversation(conn, phone, "await_service", {}, business["id"])

    def _pick_service(
        self, conn: sqlite3.Connection, phone: str, data: dict, business_id: int | None, service_id: str
    ) -> None:
        service = conn.execute(
            "SELECT * FROM services WHERE id = ? AND business_id = ? AND active = 1",
            (service_id, business_id),
        ).fetchone()
        if service is None:
            self._greet(conn, phone)
            return
        dates = bookable_dates(conn, business_id, service["duration_min"])
        if not dates:
            self.wa.send_text(
                conn, phone, "😔 No free slots in the next month. Message the business directly."
            )
            _clear_conversation(conn, phone)
            return
        rows = [{"id": f"date:{d}", "title": _weekday_label(d), "description": ""} for d in dates]
        self.wa.send(
            conn,
            phone,
            list_message(f"*{service['name']}* — which day suits you?", "Choose day", rows, header="Days"),
        )
        data["service_id"] = service["id"]
        _save_conversation(conn, phone, "await_date", data, business_id)

    def _pick_date(
        self, conn: sqlite3.Connection, phone: str, data: dict, business_id: int | None, date_str: str
    ) -> None:
        service = conn.execute(
            "SELECT * FROM services WHERE id = ?", (data.get("service_id"),)
        ).fetchone()
        if service is None:
            self._greet(conn, phone)
            return
        slots = available_slots(conn, business_id, service["duration_min"], date_str)
        if not slots:
            self.wa.send_text(conn, phone, "That day just filled up — pick another one.")
            self._pick_service(conn, phone, data, business_id, str(service["id"]))
            return
        rows = [{"id": f"time:{t}", "title": t, "description": ""} for t in slots[:10]]
        self.wa.send(
            conn,
            phone,
            list_message(f"{_weekday_label(date_str)} — what time?", "Choose time", rows, header="Times"),
        )
        data["date"] = date_str
        _save_conversation(conn, phone, "await_time", data, business_id)

    def _pick_time(
        self, conn: sqlite3.Connection, phone: str, data: dict, business_id: int | None, time_str: str
    ) -> None:
        service = conn.execute(
            "SELECT * FROM services WHERE id = ?", (data.get("service_id"),)
        ).fetchone()
        if service is None or "date" not in data:
            self._greet(conn, phone)
            return
        if time_str not in available_slots(conn, business_id, service["duration_min"], data["date"]):
            self.wa.send_text(conn, phone, "That time was just taken — here are the free ones:")
            self._pick_date(conn, phone, data, business_id, data["date"])
            return
        data["time"] = time_str
        self.wa.send_text(conn, phone, "Almost done! What name should we put on the booking?")
        _save_conversation(conn, phone, "await_name", data, business_id)

    def _pick_name(
        self, conn: sqlite3.Connection, phone: str, data: dict, business_id: int | None, name: str
    ) -> None:
        name = name.strip()
        if not name:
            self.wa.send_text(conn, phone, "Please send the name for the booking.")
            return
        service = conn.execute(
            "SELECT * FROM services WHERE id = ?", (data.get("service_id"),)
        ).fetchone()
        if service is None or "date" not in data or "time" not in data:
            self._greet(conn, phone)
            return
        # Re-check the slot right before charging.
        if data["time"] not in available_slots(conn, business_id, service["duration_min"], data["date"]):
            self.wa.send_text(conn, phone, "Sorry — that slot was just taken. Let's pick another:")
            self._pick_date(conn, phone, data, business_id, data["date"])
            return

        deposit = float(service["deposit_ghs"])
        end_time = add_minutes(data["time"], int(service["duration_min"]))

        if self.payments.demo:
            reference = new_reference()
            status = "confirmed"
        else:
            provider = momo_provider_from_phone(phone) or "mtn"
            try:
                reference = self.payments.charge_momo(
                    amount_ghs=deposit, phone=phone, provider=provider
                )
            except PaymentError:
                self.wa.send_text(
                    conn,
                    phone,
                    "😔 We couldn't start the payment. You can book on the web instead: "
                    f"{self.settings.base_url}/b/{self._slug(conn, business_id)}",
                )
                _clear_conversation(conn, phone)
                return
            status = "pending"

        conn.execute(
            "INSERT INTO bookings (business_id, service_id, customer_name, customer_phone,"
            " date, start_time, end_time, status, deposit_ghs, payment_ref, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                business_id,
                service["id"],
                name,
                phone,
                data["date"],
                data["time"],
                end_time,
                status,
                deposit,
                reference,
                dbmod.now_iso(),
            ),
        )
        conn.commit()
        booking_id = conn.execute(
            "SELECT id FROM bookings WHERE payment_ref = ?", (reference,)
        ).fetchone()["id"]
        _clear_conversation(conn, phone)

        if status == "confirmed":
            self.send_confirmation(conn, phone, booking_id)
            booking_confirmed(conn, self.wa, booking_id)
        else:
            self.wa.send_text(
                conn,
                phone,
                f"📲 Almost there! A GHS {deposit:g} MoMo request was just sent to this "
                "number. Approve it with your PIN and your booking is locked — I'll "
                "confirm here the moment it lands.",
            )

    # send_confirmation is also called by the Paystack webhook for
    # WhatsApp-originated bookings once the MoMo charge lands.
    def send_confirmation(self, conn: sqlite3.Connection, phone: str, booking_id: int) -> None:
        b = conn.execute(
            "SELECT b.*, s.name AS service_name, s.price_ghs, biz.name AS business_name,"
            " biz.location FROM bookings b"
            " JOIN services s ON s.id = b.service_id"
            " JOIN businesses biz ON biz.id = b.business_id WHERE b.id = ?",
            (booking_id,),
        ).fetchone()
        balance = float(b["price_ghs"]) - float(b["deposit_ghs"])
        self.wa.send_text(
            conn,
            phone,
            f"✅ Booked! {b['service_name']} with {b['business_name']} on "
            f"{_weekday_label(b['date'])} at {b['start_time']}"
            + (f" ({b['location']})" if b["location"] else "")
            + f".\nDeposit paid: GHS {b['deposit_ghs']:g}. Balance at appointment: GHS {balance:g}."
            f"\nRef: {b['payment_ref']}\nSend \"cancel\" if your plans change.",
        )

    # -- cancellation ------------------------------------------------------

    def _offer_cancellations(self, conn: sqlite3.Connection, phone: str) -> None:
        today = now_utc().strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT b.*, s.name AS service_name, biz.name AS business_name FROM bookings b"
            " JOIN services s ON s.id = b.service_id"
            " JOIN businesses biz ON biz.id = b.business_id"
            " WHERE b.customer_phone = ? AND b.status = 'confirmed' AND b.date >= ?"
            " ORDER BY b.date, b.start_time LIMIT 10",
            (phone, today),
        ).fetchall()
        if not rows:
            self.wa.send_text(conn, phone, "You have no upcoming bookings to cancel.")
            return
        options = [
            {
                "id": f"cxl:{b['payment_ref']}",
                "title": f"{b['date']} {b['start_time']}"[:24],
                "description": f"{b['service_name']} — {b['business_name']}",
            }
            for b in rows
        ]
        self.wa.send(
            conn,
            phone,
            list_message(
                "Which booking do you want to cancel? (Deposits are not refunded automatically.)",
                "Cancel booking",
                options,
                header="Your bookings",
            ),
        )

    def _do_cancel(self, conn: sqlite3.Connection, phone: str, reference: str) -> None:
        booking = conn.execute(
            "SELECT * FROM bookings WHERE payment_ref = ? AND customer_phone = ?"
            " AND status = 'confirmed'",
            (reference, phone),
        ).fetchone()
        if booking is None:
            self.wa.send_text(conn, phone, "I couldn't find that booking — it may already be cancelled.")
            return
        conn.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status = 'confirmed'",
            (booking["id"],),
        )
        conn.commit()
        self.wa.send_text(
            conn, phone, f"Your booking on {booking['date']} at {booking['start_time']} is cancelled."
        )
        booking_cancelled(conn, self.wa, booking["id"])

    # -- helpers -----------------------------------------------------------

    def _slug(self, conn: sqlite3.Connection, business_id: int | None) -> str:
        row = conn.execute("SELECT slug FROM businesses WHERE id = ?", (business_id,)).fetchone()
        return row["slug"] if row else ""
