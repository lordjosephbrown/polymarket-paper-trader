"""Public booking flow: business page → pick slot → pay deposit → confirmed.

Also: customer cancellation and the Paystack server-to-server webhook.
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db as dbmod
from ..availability import MAX_DAYS_AHEAD, available_slots, now_utc
from ..bot import Bot
from ..config import Settings
from ..main import get_conn, get_payments, get_settings, templates
from ..notify import booking_cancelled, booking_confirmed
from ..payments import PaymentError, PaymentProvider
from ..slots import add_minutes
from .auth_routes import PHONE_RE, normalize_phone

router = APIRouter()


def _get_business(conn: sqlite3.Connection, slug: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM businesses WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Business not found")
    return row


def _get_service(conn: sqlite3.Connection, business_id: int, service_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM services WHERE id = ? AND business_id = ? AND active = 1",
        (service_id, business_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return row


def _parse_date(date_str: str) -> datetime:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date")


def _max_date() -> str:
    return (now_utc() + timedelta(days=MAX_DAYS_AHEAD)).strftime("%Y-%m-%d")


@router.get("/discover", response_class=HTMLResponse)
def discover(
    request: Request,
    q: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Public directory: find businesses by name, category, or area."""
    sql = (
        "SELECT b.*, COUNT(s.id) AS service_count,"
        " (SELECT GROUP_CONCAT(l.area, ' · ') FROM locations l"
        "   WHERE l.business_id = b.id AND l.active = 1 AND l.area != '') AS areas"
        " FROM businesses b JOIN services s ON s.business_id = b.id AND s.active = 1"
    )
    params: list[str] = []
    if q.strip():
        like = f"%{q.strip()}%"
        sql += (
            " WHERE b.name LIKE ? OR b.category LIKE ? OR b.location LIKE ?"
            " OR b.id IN (SELECT business_id FROM locations WHERE active = 1 AND area LIKE ?)"
        )
        params = [like, like, like, like]
    sql += " GROUP BY b.id ORDER BY b.created_at DESC LIMIT 50"
    businesses = conn.execute(sql, params).fetchall()
    return templates.TemplateResponse(
        request, "discover.html", {"businesses": businesses, "q": q}
    )


@router.get("/b/{slug}", response_class=HTMLResponse)
def booking_page(
    request: Request,
    slug: str,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    business = _get_business(conn, slug)
    services = conn.execute(
        "SELECT * FROM services WHERE business_id = ? AND active = 1 ORDER BY id",
        (business["id"],),
    ).fetchall()
    locations = dbmod.active_locations(conn, business["id"])
    wa_link = ""
    if settings.wa_public_number:
        wa_link = f"https://wa.me/{settings.wa_public_number}?text=book%20{business['slug']}"
    return templates.TemplateResponse(
        request,
        "book.html",
        {
            "business": business,
            "services": services,
            "locations": locations,
            "today": now_utc().strftime("%Y-%m-%d"),
            "max_date": _max_date(),
            "wa_link": wa_link,
        },
    )


def _resolve_location(
    conn: sqlite3.Connection, business_id: int, location_id: int | None
) -> sqlite3.Row:
    if location_id is None:
        location_id = dbmod.default_location_id(conn, business_id)
    row = conn.execute(
        "SELECT * FROM locations WHERE id = ? AND business_id = ? AND active = 1",
        (location_id, business_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Location not found")
    return row


@router.get("/b/{slug}/slots")
def slots_api(
    slug: str,
    service_id: int,
    date: str,
    location_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    business = _get_business(conn, slug)
    service = _get_service(conn, business["id"], service_id)
    location = _resolve_location(conn, business["id"], location_id)
    _parse_date(date)
    if date > _max_date():
        return {"slots": []}
    return {
        "slots": available_slots(
            conn, business["id"], location["id"], service["duration_min"], date
        )
    }


@router.post("/b/{slug}/book", response_class=HTMLResponse)
def book(
    request: Request,
    slug: str,
    service_id: int = Form(...),
    date: str = Form(...),
    start_time: str = Form(...),
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    location_id: int | None = Form(None),
    venue: str = Form("business"),
    customer_address: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    payments: PaymentProvider = Depends(get_payments),
):
    business = _get_business(conn, slug)
    service = _get_service(conn, business["id"], service_id)
    location = _resolve_location(conn, business["id"], location_id)
    phone_n = normalize_phone(customer_phone)
    if not customer_name.strip() or not PHONE_RE.match(phone_n):
        raise HTTPException(status_code=400, detail="Enter your name and a valid phone number")
    _parse_date(date)
    if date > _max_date():
        raise HTTPException(status_code=400, detail="Date is too far ahead")

    # Venue: where the appointment happens, constrained by what the service offers.
    if venue not in {"business", "customer"}:
        raise HTTPException(status_code=400, detail="Invalid venue")
    offered = service["venue"]
    if (venue == "customer" and offered == "business") or (
        venue == "business" and offered == "customer"
    ):
        raise HTTPException(status_code=400, detail="This service isn't offered there")
    customer_address = customer_address.strip()
    if venue == "customer" and not customer_address:
        raise HTTPException(status_code=400, detail="Enter your address for a home visit")
    if dbmod.rate_limited(conn, f"book:{phone_n}", limit=10, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many booking attempts — try later")
    travel_fee = float(service["travel_fee_ghs"]) if venue == "customer" else 0.0
    deposit_total = float(service["deposit_ghs"]) + travel_fee

    if start_time not in available_slots(
        conn, business["id"], location["id"], service["duration_min"], date
    ):
        return templates.TemplateResponse(
            request, "slot_taken.html", {"business": business}, status_code=409
        )
    end_time = add_minutes(start_time, int(service["duration_min"]))
    callback_url = f"{settings.base_url}/pay/callback"
    try:
        init = payments.initialize(
            amount_ghs=deposit_total,
            customer_phone=phone_n,
            callback_url=callback_url,
        )
    except PaymentError:
        raise HTTPException(status_code=502, detail="Payment service unavailable, try again")
    dbmod.log_payment_event(conn, init.reference, "initialized", deposit_total)
    conn.execute(
        "INSERT INTO bookings (business_id, location_id, service_id, venue, customer_address,"
        " travel_fee_ghs, customer_name, customer_phone,"
        " date, start_time, end_time, status, deposit_ghs, payment_ref, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (
            business["id"],
            location["id"],
            service["id"],
            venue,
            customer_address,
            travel_fee,
            customer_name.strip(),
            phone_n,
            date,
            start_time,
            end_time,
            deposit_total,
            init.reference,
            dbmod.now_iso(),
        ),
    )
    conn.commit()
    return RedirectResponse(init.authorization_url, status_code=303)


def _confirm_booking(request: Request, conn: sqlite3.Connection, booking: sqlite3.Row) -> bool:
    """Confirm a pending booking exactly once. Returns True if this call confirmed it."""
    cur = conn.execute(
        "UPDATE bookings SET status = 'confirmed' WHERE id = ? AND status = 'pending'",
        (booking["id"],),
    )
    conn.commit()
    if cur.rowcount == 1:
        dbmod.log_booking_event(conn, booking["id"], "pending", "confirmed", "system")
        booking_confirmed(conn, request.app.state.wa, booking["id"])
        return True
    return False


@router.get("/pay/callback", response_class=HTMLResponse)
def pay_callback(
    request: Request,
    reference: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
    payments: PaymentProvider = Depends(get_payments),
):
    booking = conn.execute(
        "SELECT * FROM bookings WHERE payment_ref = ?", (reference,)
    ).fetchone()
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking["status"] == "pending":
        if payments.verify(reference):
            dbmod.log_payment_event(conn, reference, "verified", booking["deposit_ghs"])
            _confirm_booking(request, conn, booking)
        else:
            dbmod.log_payment_event(conn, reference, "failed")
            conn.execute(
                "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                (booking["id"],),
            )
            conn.commit()
            dbmod.log_booking_event(conn, booking["id"], "pending", "cancelled", "system")
            business = conn.execute(
                "SELECT * FROM businesses WHERE id = ?", (booking["business_id"],)
            ).fetchone()
            return templates.TemplateResponse(
                request, "payment_failed.html", {"business": business}, status_code=402
            )
    return RedirectResponse(f"/booking/{reference}", status_code=303)


@router.post("/pay/webhook")
async def paystack_webhook(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    payments: PaymentProvider = Depends(get_payments),
):
    """Server-to-server confirmation from Paystack (charge.success)."""
    raw = await request.body()
    if settings.paystack_secret_key:
        signature = request.headers.get("X-Paystack-Signature", "")
        expected = hmac.new(settings.paystack_secret_key.encode(), raw, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=403, detail="Bad signature")
    import json

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Bad payload")
    if event.get("event") != "charge.success":
        return {"ok": True}
    reference = event.get("data", {}).get("reference", "")
    booking = conn.execute(
        "SELECT * FROM bookings WHERE payment_ref = ?", (reference,)
    ).fetchone()
    if booking is not None and booking["status"] == "pending":
        dbmod.log_payment_event(conn, reference, "webhook_success", booking["deposit_ghs"])
        if _confirm_booking(request, conn, booking):
            # WhatsApp-originated bookings get their confirmation in the chat.
            bot = Bot(settings, payments, request.app.state.wa)
            bot.send_confirmation(conn, booking["customer_phone"], booking["id"])
    return {"ok": True}


@router.get("/booking/{reference}", response_class=HTMLResponse)
def booking_detail(
    request: Request,
    reference: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    booking = conn.execute(
        "SELECT b.*, s.name AS service_name, s.price_ghs FROM bookings b"
        " JOIN services s ON s.id = b.service_id WHERE b.payment_ref = ?",
        (reference,),
    ).fetchone()
    if booking is None or booking["status"] == "pending":
        raise HTTPException(status_code=404, detail="Booking not found")
    business = conn.execute(
        "SELECT * FROM businesses WHERE id = ?", (booking["business_id"],)
    ).fetchone()
    # Total owed = service price + travel fee; the paid deposit already includes the fee.
    balance = (
        float(booking["price_ghs"])
        + float(booking["travel_fee_ghs"])
        - float(booking["deposit_ghs"])
    )
    return templates.TemplateResponse(
        request,
        "confirmed.html",
        {"business": business, "booking": booking, "balance": balance},
    )


@router.post("/booking/{reference}/cancel", response_class=HTMLResponse)
def cancel_booking(
    request: Request,
    reference: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    booking = conn.execute(
        "SELECT * FROM bookings WHERE payment_ref = ? AND status = 'confirmed'", (reference,)
    ).fetchone()
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    conn.execute(
        "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status = 'confirmed'",
        (booking["id"],),
    )
    conn.commit()
    dbmod.log_booking_event(conn, booking["id"], "confirmed", "cancelled", "customer")
    booking_cancelled(conn, request.app.state.wa, booking["id"])
    business = conn.execute(
        "SELECT * FROM businesses WHERE id = ?", (booking["business_id"],)
    ).fetchone()
    return templates.TemplateResponse(request, "cancelled.html", {"business": business})
