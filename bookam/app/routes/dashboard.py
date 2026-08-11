"""Authenticated business dashboard: bookings, services, hours, customers, share."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import Settings
from ..main import current_business, get_conn, get_settings, login_redirect, templates

router = APIRouter(prefix="/dashboard")

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _today() -> str:
    # Ghana is UTC year-round.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@router.get("", response_class=HTMLResponse)
def dashboard(
    request: Request,
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    if business is None:
        return login_redirect()
    today = _today()
    todays = conn.execute(
        "SELECT b.*, s.name AS service_name FROM bookings b JOIN services s ON s.id = b.service_id"
        " WHERE b.business_id = ? AND b.date = ? AND b.status != 'pending'"
        " ORDER BY b.start_time",
        (business["id"], today),
    ).fetchall()
    upcoming = conn.execute(
        "SELECT b.*, s.name AS service_name FROM bookings b JOIN services s ON s.id = b.service_id"
        " WHERE b.business_id = ? AND b.date > ? AND b.status = 'confirmed'"
        " ORDER BY b.date, b.start_time LIMIT 20",
        (business["id"], today),
    ).fetchall()
    stats = conn.execute(
        "SELECT"
        " COUNT(*) FILTER (WHERE status IN ('confirmed','completed','no_show')) AS total,"
        " COUNT(*) FILTER (WHERE status = 'no_show') AS no_shows,"
        " COALESCE(SUM(deposit_ghs) FILTER (WHERE status IN ('confirmed','completed','no_show')), 0) AS deposits"
        " FROM bookings WHERE business_id = ?",
        (business["id"],),
    ).fetchone()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "business": business,
            "today": today,
            "todays": todays,
            "upcoming": upcoming,
            "stats": stats,
            "welcome": request.query_params.get("welcome"),
            "booking_url": f"{settings.base_url}/b/{business['slug']}",
            "wa_deep_link": (
                f"https://wa.me/{settings.wa_public_number}?text=book%20{business['slug']}"
                if settings.wa_public_number
                else ""
            ),
        },
    )


@router.post("/bookings/{booking_id}/status")
def set_booking_status(
    booking_id: int,
    status: str = Form(...),
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    if status in {"completed", "no_show", "cancelled"}:
        conn.execute(
            "UPDATE bookings SET status = ? WHERE id = ? AND business_id = ?"
            " AND status IN ('confirmed','completed','no_show')",
            (status, booking_id, business["id"]),
        )
        conn.commit()
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/services", response_class=HTMLResponse)
def services_page(
    request: Request,
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    services = conn.execute(
        "SELECT * FROM services WHERE business_id = ? AND active = 1 ORDER BY id",
        (business["id"],),
    ).fetchall()
    return templates.TemplateResponse(
        request, "services.html", {"business": business, "services": services, "error": None}
    )


@router.post("/services", response_class=HTMLResponse)
def add_service(
    request: Request,
    name: str = Form(...),
    duration_min: int = Form(...),
    price_ghs: float = Form(...),
    deposit_ghs: float = Form(...),
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    error = None
    if not name.strip():
        error = "Service name is required."
    elif duration_min < 5 or duration_min > 480:
        error = "Duration must be between 5 minutes and 8 hours."
    elif price_ghs < 0 or deposit_ghs < 0:
        error = "Price and deposit cannot be negative."
    elif deposit_ghs > price_ghs:
        error = "Deposit cannot be more than the full price."
    if error:
        services = conn.execute(
            "SELECT * FROM services WHERE business_id = ? AND active = 1 ORDER BY id",
            (business["id"],),
        ).fetchall()
        return templates.TemplateResponse(
            request,
            "services.html",
            {"business": business, "services": services, "error": error},
            status_code=400,
        )
    conn.execute(
        "INSERT INTO services (business_id, name, duration_min, price_ghs, deposit_ghs)"
        " VALUES (?, ?, ?, ?, ?)",
        (business["id"], name.strip(), duration_min, price_ghs, deposit_ghs),
    )
    conn.commit()
    return RedirectResponse("/dashboard/services", status_code=303)


@router.post("/services/{service_id}/delete")
def delete_service(
    service_id: int,
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    conn.execute(
        "UPDATE services SET active = 0 WHERE id = ? AND business_id = ?",
        (service_id, business["id"]),
    )
    conn.commit()
    return RedirectResponse("/dashboard/services", status_code=303)


@router.get("/hours", response_class=HTMLResponse)
def hours_page(
    request: Request,
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    rows = conn.execute(
        "SELECT * FROM hours WHERE business_id = ?", (business["id"],)
    ).fetchall()
    by_day = {row["weekday"]: row for row in rows}
    days = [
        {
            "weekday": i,
            "name": WEEKDAYS[i],
            "open": by_day[i]["open_time"] if i in by_day else "",
            "close": by_day[i]["close_time"] if i in by_day else "",
        }
        for i in range(7)
    ]
    return templates.TemplateResponse(
        request, "hours.html", {"business": business, "days": days, "error": None}
    )


@router.post("/hours")
async def save_hours(
    request: Request,
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    form = await request.form()
    for weekday in range(7):
        enabled = form.get(f"enabled_{weekday}")
        open_t = str(form.get(f"open_{weekday}", "") or "")
        close_t = str(form.get(f"close_{weekday}", "") or "")
        conn.execute(
            "DELETE FROM hours WHERE business_id = ? AND weekday = ?",
            (business["id"], weekday),
        )
        if enabled and open_t and close_t and open_t < close_t:
            conn.execute(
                "INSERT INTO hours (business_id, weekday, open_time, close_time)"
                " VALUES (?, ?, ?, ?)",
                (business["id"], weekday, open_t, close_t),
            )
    conn.commit()
    return RedirectResponse("/dashboard/hours", status_code=303)


@router.get("/customers", response_class=HTMLResponse)
def customers_page(
    request: Request,
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    customers = conn.execute(
        "SELECT customer_phone, customer_name,"
        " COUNT(*) AS visits,"
        " COUNT(*) FILTER (WHERE status = 'no_show') AS no_shows,"
        " MAX(date) AS last_visit"
        " FROM bookings WHERE business_id = ? AND status != 'pending'"
        " GROUP BY customer_phone ORDER BY MAX(date) DESC",
        (business["id"],),
    ).fetchall()
    return templates.TemplateResponse(
        request, "customers.html", {"business": business, "customers": customers}
    )
