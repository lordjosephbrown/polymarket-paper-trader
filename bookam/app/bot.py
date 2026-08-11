"""WhatsApp booking bot — a conversation state machine.

Customers never leave WhatsApp: services, branches, dates and times are
presented as native tap-to-select lists, and in live mode the deposit arrives
as a MoMo approval prompt on their phone (Paystack Charge API). The wa.me deep
link a business shares pre-fills "book <slug>" so the bot knows who they're
booking.

Flow: service → location (if several branches) → venue (shop or home visit,
if the service offers both) → address (for home visits) → date → time → name
→ deposit. Commands understood at any point: "book <slug>", "cancel",
anything else gets the greeting.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta

from . import db as dbmod
from .availability import available_slots, bookable_dates, find_free_staff, now_utc
from .config import Settings
from .notify import booking_cancelled, booking_confirmed
from .payments import PaymentError, PaymentProvider, momo_provider_from_phone, new_reference
from .slots import add_minutes
from .wa import WhatsAppClient, buttons_message, list_message

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


def _phone_variants(phone: str) -> tuple[str, str]:
    """A customer's bookings may be stored in local (024…) or wa (23324…) format —
    web bookings save what they typed, WhatsApp bookings save the wa_id."""
    digits = phone.lstrip("+")
    if digits.startswith("233") and len(digits) == 12:
        return digits, "0" + digits[3:]
    if digits.startswith("0") and len(digits) == 10:
        return "233" + digits[1:], digits
    return digits, digits


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
        if lower in {"status", "my bookings", "bookings"}:
            self._show_status(conn, phone)
            return
        rate_match = re.match(r"rate\s+([1-5])(?:\s+(.+))?$", lower)
        if rate_match:
            self._rate(conn, phone, int(rate_match.group(1)), (rate_match.group(2) or "").strip())
            return
        if lower in {"stop", "unsubscribe"}:
            conn.execute(
                "INSERT OR IGNORE INTO broadcast_optouts (phone, created_at) VALUES (?, ?)",
                (phone, dbmod.now_iso()),
            )
            conn.commit()
            self.wa.send_text(
                conn,
                phone,
                "You won't receive business updates anymore. Booking messages still come "
                "through. Send \"start\" to resubscribe.",
            )
            return
        if lower == "start" and state == "idle":
            conn.execute("DELETE FROM broadcast_optouts WHERE phone = ?", (phone,))
            conn.commit()
            self.wa.send_text(conn, phone, "Welcome back! You'll receive business updates again.")
            return
        if text.startswith("cxl:"):
            self._do_cancel(conn, phone, text[4:])
            return

        if state == "await_service" and text.startswith("svc:"):
            self._pick_service(conn, phone, data, business_id, text[4:])
        elif state == "await_location" and text.startswith("loc:"):
            self._pick_location(conn, phone, data, business_id, text[4:])
        elif state == "await_venue" and text.startswith("ven:"):
            self._pick_venue(conn, phone, data, business_id, text[4:])
        elif state == "await_staff" and text.startswith("stf:"):
            self._pick_staff(conn, phone, data, business_id, text[4:])
        elif state == "await_address":
            self._pick_address(conn, phone, data, business_id, text)
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
            "Send \"status\" to check your bookings, or \"cancel\" to cancel one.",
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
        rows = []
        for s in services:
            desc = f"GHS {s['price_ghs']:g} · {s['duration_min']} min · deposit GHS {s['deposit_ghs']:g}"
            if s["venue"] == "customer":
                desc += " · comes to you"
            elif s["venue"] == "both":
                desc += " · shop or home"
            rows.append({"id": f"svc:{s['id']}", "title": s["name"][:24], "description": desc})
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
        data["service_id"] = service["id"]
        locations = dbmod.active_locations(conn, business_id)
        if len(locations) > 1:
            rows = [
                {"id": f"loc:{l['id']}", "title": l["name"][:24], "description": l["area"]}
                for l in locations
            ]
            self.wa.send(
                conn,
                phone,
                list_message("Which branch?", "Choose branch", rows, header="Branches"),
            )
            _save_conversation(conn, phone, "await_location", data, business_id)
            return
        data["location_id"] = locations[0]["id"] if locations else None
        self._ask_venue(conn, phone, data, business_id, service)

    def _pick_location(
        self, conn: sqlite3.Connection, phone: str, data: dict, business_id: int | None, loc_id: str
    ) -> None:
        location = conn.execute(
            "SELECT * FROM locations WHERE id = ? AND business_id = ? AND active = 1",
            (loc_id, business_id),
        ).fetchone()
        service = conn.execute(
            "SELECT * FROM services WHERE id = ?", (data.get("service_id"),)
        ).fetchone()
        if location is None or service is None:
            self._greet(conn, phone)
            return
        data["location_id"] = location["id"]
        self._ask_venue(conn, phone, data, business_id, service)

    def _ask_venue(
        self, conn: sqlite3.Connection, phone: str, data: dict, business_id: int | None, service: sqlite3.Row
    ) -> None:
        offered = service["venue"]
        if offered == "both":
            fee = float(service["travel_fee_ghs"])
            note = f" (adds GHS {fee:g} travel fee)" if fee else ""
            self.wa.send(
                conn,
                phone,
                buttons_message(
                    f"Where should it happen?{note}",
                    [
                        {"id": "ven:business", "title": "At the shop"},
                        {"id": "ven:customer", "title": "At my place"},
                    ],
                ),
            )
            _save_conversation(conn, phone, "await_venue", data, business_id)
            return
        data["venue"] = offered  # "business" or "customer"
        if offered == "customer":
            self._ask_address(conn, phone, data, business_id)
        else:
            self._ask_staff(conn, phone, data, business_id)

    def _pick_venue(
        self, conn: sqlite3.Connection, phone: str, data: dict, business_id: int | None, choice: str
    ) -> None:
        if choice not in {"business", "customer"}:
            self._greet(conn, phone)
            return
        data["venue"] = choice
        if choice == "customer":
            self._ask_address(conn, phone, data, business_id)
        else:
            self._ask_staff(conn, phone, data, business_id)

    def _ask_address(
        self, conn: sqlite3.Connection, phone: str, data: dict, business_id: int | None
    ) -> None:
        self.wa.send_text(
            conn,
            phone,
            "🏠 Where should they come? Send your area and a landmark or GPS address "
            "(e.g. \"East Legon, near A&C Mall — GA-334-5567\").",
        )
        _save_conversation(conn, phone, "await_address", data, business_id)

    def _pick_address(
        self, conn: sqlite3.Connection, phone: str, data: dict, business_id: int | None, address: str
    ) -> None:
        if not address.strip():
            self.wa.send_text(conn, phone, "Please send your address for the home visit.")
            return
        data["address"] = address.strip()
        self._ask_staff(conn, phone, data, business_id)

    def _ask_staff(
        self, conn: sqlite3.Connection, phone: str, data: dict, business_id: int | None
    ) -> None:
        members = conn.execute(
            "SELECT * FROM staff WHERE location_id = ? AND active = 1 ORDER BY id",
            (data.get("location_id"),),
        ).fetchall()
        if len(members) < 2:
            data["staff_id"] = members[0]["id"] if members else 0
            self._show_dates(conn, phone, data, business_id)
            return
        rows = [{"id": "stf:0", "title": "Anyone available", "description": "First free person"}]
        rows += [{"id": f"stf:{m['id']}", "title": m["name"][:24], "description": ""} for m in members]
        self.wa.send(
            conn,
            phone,
            list_message("Who would you like?", "Choose person", rows, header="Team"),
        )
        _save_conversation(conn, phone, "await_staff", data, business_id)

    def _pick_staff(
        self, conn: sqlite3.Connection, phone: str, data: dict, business_id: int | None, choice: str
    ) -> None:
        if choice != "0":
            member = conn.execute(
                "SELECT * FROM staff WHERE id = ? AND location_id = ? AND active = 1",
                (choice, data.get("location_id")),
            ).fetchone()
            if member is None:
                self._greet(conn, phone)
                return
            data["staff_id"] = member["id"]
        else:
            data["staff_id"] = 0  # anyone
        self._show_dates(conn, phone, data, business_id)

    def _staff_filter(self, data: dict) -> int | None:
        staff_id = data.get("staff_id")
        return staff_id if staff_id else None

    def _rate(self, conn: sqlite3.Connection, phone: str, rating: int, comment: str) -> None:
        wa_fmt, local_fmt = _phone_variants(phone)
        booking = conn.execute(
            "SELECT * FROM bookings WHERE customer_phone IN (?, ?) AND status = 'completed'"
            " AND id NOT IN (SELECT booking_id FROM reviews)"
            " ORDER BY date DESC, start_time DESC LIMIT 1",
            (wa_fmt, local_fmt),
        ).fetchone()
        if booking is None:
            self.wa.send_text(
                conn, phone, "I couldn't find a completed booking to rate — thanks anyway!"
            )
            return
        conn.execute(
            "INSERT OR IGNORE INTO reviews (booking_id, business_id, rating, comment, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (booking["id"], booking["business_id"], rating, comment[:300], dbmod.now_iso()),
        )
        conn.commit()
        stars = "⭐" * rating
        self.wa.send_text(
            conn, phone, f"{stars} Thank you! Your rating helps others find great businesses."
        )

    def _show_dates(
        self, conn: sqlite3.Connection, phone: str, data: dict, business_id: int | None
    ) -> None:
        service = conn.execute(
            "SELECT * FROM services WHERE id = ?", (data.get("service_id"),)
        ).fetchone()
        if service is None or data.get("location_id") is None:
            self._greet(conn, phone)
            return
        dates = bookable_dates(
            conn, business_id, data["location_id"], service["duration_min"],
            staff_id=self._staff_filter(data),
        )
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
        _save_conversation(conn, phone, "await_date", data, business_id)

    def _pick_date(
        self, conn: sqlite3.Connection, phone: str, data: dict, business_id: int | None, date_str: str
    ) -> None:
        service = conn.execute(
            "SELECT * FROM services WHERE id = ?", (data.get("service_id"),)
        ).fetchone()
        if service is None or data.get("location_id") is None:
            self._greet(conn, phone)
            return
        slots = available_slots(
            conn, business_id, data["location_id"], service["duration_min"], date_str,
            staff_id=self._staff_filter(data),
        )
        if not slots:
            self.wa.send_text(conn, phone, "That day just filled up — pick another one.")
            self._show_dates(conn, phone, data, business_id)
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
        if service is None or "date" not in data or data.get("location_id") is None:
            self._greet(conn, phone)
            return
        if time_str not in available_slots(
            conn, business_id, data["location_id"], service["duration_min"], data["date"],
            staff_id=self._staff_filter(data),
        ):
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
        if (
            service is None
            or "date" not in data
            or "time" not in data
            or data.get("location_id") is None
        ):
            self._greet(conn, phone)
            return
        # Re-check the slot right before charging.
        if data["time"] not in available_slots(
            conn, business_id, data["location_id"], service["duration_min"], data["date"],
            staff_id=self._staff_filter(data),
        ):
            self.wa.send_text(conn, phone, "Sorry — that slot was just taken. Let's pick another:")
            self._pick_date(conn, phone, data, business_id, data["date"])
            return

        venue = data.get("venue", "business")
        address = data.get("address", "")
        travel_fee = float(service["travel_fee_ghs"]) if venue == "customer" else 0.0
        deposit_total = float(service["deposit_ghs"]) + travel_fee
        end_time = add_minutes(data["time"], int(service["duration_min"]))
        requested = self._staff_filter(data)
        if requested is not None:
            assigned_staff: int | None = requested
        else:
            try:
                assigned_staff = find_free_staff(
                    conn, business_id, data["location_id"], data["date"], data["time"], end_time
                )
            except LookupError:
                self.wa.send_text(conn, phone, "Sorry — that slot was just taken. Let's pick another:")
                self._pick_date(conn, phone, data, business_id, data["date"])
                return

        if self.payments.demo:
            reference = new_reference()
            status = "confirmed"
            dbmod.log_payment_event(conn, reference, "verified", deposit_total)
        else:
            provider = momo_provider_from_phone(phone) or "mtn"
            try:
                reference = self.payments.charge_momo(
                    amount_ghs=deposit_total, phone=phone, provider=provider
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
            dbmod.log_payment_event(conn, reference, "initialized", deposit_total)

        conn.execute(
            "INSERT INTO bookings (business_id, location_id, staff_id, service_id, venue,"
            " customer_address, travel_fee_ghs, customer_name, customer_phone,"
            " date, start_time, end_time, status, deposit_ghs, payment_ref, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                business_id,
                data["location_id"],
                assigned_staff,
                service["id"],
                venue,
                address,
                travel_fee,
                name,
                phone,
                data["date"],
                data["time"],
                end_time,
                status,
                deposit_total,
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
                f"📲 Almost there! A GHS {deposit_total:g} MoMo request was just sent to this "
                "number. Approve it with your PIN and your booking is locked — I'll "
                "confirm here the moment it lands.",
            )

    # send_confirmation is also called by the Paystack webhook for
    # WhatsApp-originated bookings once the MoMo charge lands.
    def send_confirmation(self, conn: sqlite3.Connection, phone: str, booking_id: int) -> None:
        b = conn.execute(
            "SELECT b.*, s.name AS service_name, s.price_ghs, biz.name AS business_name,"
            " l.name AS location_name, l.area AS location_area FROM bookings b"
            " JOIN services s ON s.id = b.service_id"
            " JOIN businesses biz ON biz.id = b.business_id"
            " LEFT JOIN locations l ON l.id = b.location_id WHERE b.id = ?",
            (booking_id,),
        ).fetchone()
        balance = float(b["price_ghs"]) + float(b["travel_fee_ghs"]) - float(b["deposit_ghs"])
        if b["venue"] == "customer":
            where = f"🏠 They'll come to you: {b['customer_address']}"
        else:
            where = f"📍 {b['location_name']}" + (
                f", {b['location_area']}" if b["location_area"] else ""
            )
        self.wa.send_text(
            conn,
            phone,
            f"✅ Booked! {b['service_name']} with {b['business_name']} on "
            f"{_weekday_label(b['date'])} at {b['start_time']}.\n{where}\n"
            f"Deposit paid: GHS {b['deposit_ghs']:g}. Balance at appointment: GHS {balance:g}."
            f"\nRef: {b['payment_ref']}\nSend \"cancel\" if your plans change.",
        )

    # -- status tracking ---------------------------------------------------

    STATUS_LABELS = {
        "confirmed": "✅ Booked",
        "on_the_way": "🚗 On the way to you",
        "in_progress": "💈 In progress",
    }

    def _show_status(self, conn: sqlite3.Connection, phone: str) -> None:
        today = now_utc().strftime("%Y-%m-%d")
        wa_fmt, local_fmt = _phone_variants(phone)
        rows = conn.execute(
            "SELECT b.*, s.name AS service_name, biz.name AS business_name FROM bookings b"
            " JOIN services s ON s.id = b.service_id"
            " JOIN businesses biz ON biz.id = b.business_id"
            " WHERE b.customer_phone IN (?, ?) AND b.date >= ?"
            " AND b.status IN ('confirmed','on_the_way','in_progress')"
            " ORDER BY b.date, b.start_time LIMIT 5",
            (wa_fmt, local_fmt, today),
        ).fetchall()
        if not rows:
            self.wa.send_text(conn, phone, "You have no upcoming bookings.")
            return
        lines = ["📋 Your bookings:"]
        for b in rows:
            lines.append(
                f"• {b['service_name']} with {b['business_name']} — "
                f"{_weekday_label(b['date'])} at {b['start_time']}: "
                f"{self.STATUS_LABELS[b['status']]}"
            )
        self.wa.send_text(conn, phone, "\n".join(lines))

    # -- cancellation ------------------------------------------------------

    def _offer_cancellations(self, conn: sqlite3.Connection, phone: str) -> None:
        today = now_utc().strftime("%Y-%m-%d")
        wa_fmt, local_fmt = _phone_variants(phone)
        rows = conn.execute(
            "SELECT b.*, s.name AS service_name, biz.name AS business_name FROM bookings b"
            " JOIN services s ON s.id = b.service_id"
            " JOIN businesses biz ON biz.id = b.business_id"
            " WHERE b.customer_phone IN (?, ?) AND b.status = 'confirmed' AND b.date >= ?"
            " ORDER BY b.date, b.start_time LIMIT 10",
            (wa_fmt, local_fmt, today),
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
        wa_fmt, local_fmt = _phone_variants(phone)
        booking = conn.execute(
            "SELECT * FROM bookings WHERE payment_ref = ? AND customer_phone IN (?, ?)"
            " AND status = 'confirmed'",
            (reference, wa_fmt, local_fmt),
        ).fetchone()
        if booking is None:
            self.wa.send_text(conn, phone, "I couldn't find that booking — it may already be cancelled.")
            return
        conn.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status = 'confirmed'",
            (booking["id"],),
        )
        conn.commit()
        dbmod.log_booking_event(conn, booking["id"], "confirmed", "cancelled", "customer")
        self.wa.send_text(
            conn, phone, f"Your booking on {booking['date']} at {booking['start_time']} is cancelled."
        )
        booking_cancelled(conn, self.wa, booking["id"])

    # -- helpers -----------------------------------------------------------

    def _slug(self, conn: sqlite3.Connection, business_id: int | None) -> str:
        row = conn.execute("SELECT slug FROM businesses WHERE id = ?", (business_id,)).fetchone()
        return row["slug"] if row else ""
