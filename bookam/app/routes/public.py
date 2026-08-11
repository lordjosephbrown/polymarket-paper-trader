"""Public booking flow: business page → pick slot → pay deposit → confirmed."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db as dbmod
from ..config import Settings
from ..main import get_conn, get_payments, get_settings, templates
from ..payments import PaymentError, PaymentProvider
from ..slots import add_minutes, slots_for_date
from .auth_routes import PHONE_RE, normalize_phone

router = APIRouter()

MAX_DAYS_AHEAD = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)  # Ghana is UTC year-round


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


def _available_slots(
    conn: sqlite3.Connection, business: sqlite3.Row, service: sqlite3.Row, date_str: str
) -> list[str]:
    day = _parse_date(date_str)
    hours = conn.execute(
        "SELECT * FROM hours WHERE business_id = ? AND weekday = ?",
        (business["id"], day.weekday()),
    ).fetchone()
    if hours is None:
        return []
    # Pending (unpaid) bookings hold their slot for 15 minutes, then lapse.
    pending_cutoff = (_now() - timedelta(minutes=15)).isoformat(timespec="seconds")
    busy_rows = conn.execute(
        "SELECT start_time, end_time FROM bookings"
        " WHERE business_id = ? AND date = ?"
        " AND (status = 'confirmed' OR (status = 'pending' AND created_at > ?))",
        (business["id"], date_str, pending_cutoff),
    ).fetchall()
    busy = [(r["start_time"], r["end_time"]) for r in busy_rows]
    return slots_for_date(
        hours["open_time"], hours["close_time"], service["duration_min"], busy, date_str, _now()
    )


@router.get("/b/{slug}", response_class=HTMLResponse)
def booking_page(
    request: Request,
    slug: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    business = _get_business(conn, slug)
    services = conn.execute(
        "SELECT * FROM services WHERE business_id = ? AND active = 1 ORDER BY id",
        (business["id"],),
    ).fetchall()
    today = _now().strftime("%Y-%m-%d")
    max_date = (_now() + timedelta(days=MAX_DAYS_AHEAD)).strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        request,
        "book.html",
        {"business": business, "services": services, "today": today, "max_date": max_date},
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
    if date > (_now() + timedelta(days=MAX_DAYS_AHEAD)).strftime("%Y-%m-%d"):
        return {"slots": []}
    return {"slots": _available_slots(conn, business, service, date)}


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
    if date > (_now() + timedelta(days=MAX_DAYS_AHEAD)).strftime("%Y-%m-%d"):
        raise HTTPException(status_code=400, detail="Date is too far ahead")
    if start_time not in _available_slots(conn, business, service, date):
        return templates.TemplateResponse(
            request,
            "slot_taken.html",
            {"business": business},
            status_code=409,
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
            conn.execute(
                "UPDATE bookings SET status = 'confirmed' WHERE id = ? AND status = 'pending'",
                (booking["id"],),
            )
            conn.commit()
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
                request,
                "payment_failed.html",
                {"business": business},
                status_code=402,
            )
    return RedirectResponse(f"/booking/{reference}", status_code=303)


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
