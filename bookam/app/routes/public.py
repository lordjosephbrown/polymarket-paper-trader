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
    wa_link = ""
    if settings.wa_public_number:
        wa_link = f"https://wa.me/{settings.wa_public_number}?text=book%20{business['slug']}"
    return templates.TemplateResponse(
        request,
        "book.html",
        {
            "business": business,
            "services": services,
            "today": now_utc().strftime("%Y-%m-%d"),
            "max_date": _max_date(),
            "wa_link": wa_link,
        },
    )


@router.get("/b/{slug}/slots")
def slots_api(
    slug: str,
    service_id: int,
    date: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    business = _get_business(conn, slug)
    service = _get_service(conn, business["id"], service_id)
    _parse_date(date)
    if date > _max_date():
        return {"slots": []}
    return {"slots": available_slots(conn, business["id"], service["duration_min"], date)}


@router.post("/b/{slug}/book", response_class=HTMLResponse)
def book(
    request: Request,
    slug: str,
    service_id: int = Form(...),
    date: str = Form(...),
    start_time: str = Form(...),
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    payments: PaymentProvider = Depends(get_payments),
):
    business = _get_business(conn, slug)
    service = _get_service(conn, business["id"], service_id)
    phone_n = normalize_phone(customer_phone)
    if not customer_name.strip() or not PHONE_RE.match(phone_n):
        raise HTTPException(status_code=400, detail="Enter your name and a valid phone number")
    _parse_date(date)
    if date > _max_date():
        raise HTTPException(status_code=400, detail="Date is too far ahead")
    if start_time not in available_slots(conn, business["id"], service["duration_min"], date):
        return templates.TemplateResponse(
            request, "slot_taken.html", {"business": business}, status_code=409
        )
    end_time = add_minutes(start_time, int(service["duration_min"]))
    callback_url = f"{settings.base_url}/pay/callback"
    try:
        init = payments.initialize(
            amount_ghs=float(service["deposit_ghs"]),
            customer_phone=phone_n,
            callback_url=callback_url,
        )
    except PaymentError:
        raise HTTPException(status_code=502, detail="Payment service unavailable, try again")
    conn.execute(
        "INSERT INTO bookings (business_id, service_id, customer_name, customer_phone,"
        " date, start_time, end_time, status, deposit_ghs, payment_ref, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (
            business["id"],
            service["id"],
            customer_name.strip(),
            phone_n,
            date,
            start_time,
            end_time,
            float(service["deposit_ghs"]),
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
            _confirm_booking(request, conn, booking)
        else:
            conn.execute(
                "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                (booking["id"],),
            )
            conn.commit()
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
    balance = float(booking["price_ghs"]) - float(booking["deposit_ghs"])
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
    booking_cancelled(conn, request.app.state.wa, booking["id"])
    business = conn.execute(
        "SELECT * FROM businesses WHERE id = ?", (booking["business_id"],)
    ).fetchone()
    return templates.TemplateResponse(request, "cancelled.html", {"business": business})
