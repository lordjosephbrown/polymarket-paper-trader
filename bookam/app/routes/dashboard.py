"""Authenticated business dashboard: bookings, services, hours, customers, share."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db as dbmod
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
        " COUNT(*) FILTER (WHERE status NOT IN ('pending','cancelled')) AS total,"
        " COUNT(*) FILTER (WHERE status = 'no_show') AS no_shows,"
        " COALESCE(SUM(deposit_ghs) FILTER (WHERE status NOT IN ('pending','cancelled')), 0) AS deposits"
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
    request: Request,
    booking_id: int,
    status: str = Form(...),
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE id = ? AND business_id = ?",
        (booking_id, business["id"]),
    ).fetchone()
    if booking is not None and status in dbmod.STATUS_TRANSITIONS.get(booking["status"], set()):
        conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
        conn.commit()
        from ..notify import booking_cancelled_by_business, delivery_status_update

        if status == "cancelled":
            booking_cancelled_by_business(conn, request.app.state.wa, booking_id)
        else:
            delivery_status_update(conn, request.app.state.wa, booking_id, status)
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/bookings/{booking_id}/reschedule")
def reschedule_booking(
    request: Request,
    booking_id: int,
    date: str = Form(...),
    start_time: str = Form(...),
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    booking = conn.execute(
        "SELECT b.*, s.duration_min FROM bookings b JOIN services s ON s.id = b.service_id"
        " WHERE b.id = ? AND b.business_id = ? AND b.status = 'confirmed'",
        (booking_id, business["id"]),
    ).fetchone()
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    from ..availability import available_slots
    from ..slots import add_minutes

    free = available_slots(
        conn,
        business["id"],
        booking["location_id"],
        booking["duration_min"],
        date,
        exclude_booking_id=booking["id"],
    )
    if start_time not in free:
        raise HTTPException(status_code=409, detail="That slot isn't free")
    old_date, old_time = booking["date"], booking["start_time"]
    conn.execute(
        "UPDATE bookings SET date = ?, start_time = ?, end_time = ?, reminded = 0 WHERE id = ?",
        (date, start_time, add_minutes(start_time, booking["duration_min"]), booking_id),
    )
    conn.commit()
    from ..notify import booking_rescheduled

    booking_rescheduled(conn, request.app.state.wa, booking_id, old_date, old_time)
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/updates", response_class=HTMLResponse)
def updates_page(
    request: Request,
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    reach = conn.execute(
        "SELECT COUNT(DISTINCT customer_phone) AS n FROM bookings"
        " WHERE business_id = ? AND status != 'pending'",
        (business["id"],),
    ).fetchone()["n"]
    recent = conn.execute(
        "SELECT * FROM broadcasts WHERE business_id = ? ORDER BY id DESC LIMIT 5",
        (business["id"],),
    ).fetchall()
    return templates.TemplateResponse(
        request,
        "updates.html",
        {"business": business, "reach": reach, "recent": recent, "error": None},
    )


@router.post("/updates", response_class=HTMLResponse)
def send_update(
    request: Request,
    message: str = Form(...),
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    message = message.strip()
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    recent_send = conn.execute(
        "SELECT 1 FROM broadcasts WHERE business_id = ? AND created_at > ?",
        (business["id"], day_ago),
    ).fetchone()
    error = None
    if not message:
        error = "Write your update first."
    elif len(message) > 500:
        error = "Keep updates under 500 characters."
    elif recent_send:
        error = "You can send one update per day — try again tomorrow."
    if error:
        reach = conn.execute(
            "SELECT COUNT(DISTINCT customer_phone) AS n FROM bookings"
            " WHERE business_id = ? AND status != 'pending'",
            (business["id"],),
        ).fetchone()["n"]
        recent = conn.execute(
            "SELECT * FROM broadcasts WHERE business_id = ? ORDER BY id DESC LIMIT 5",
            (business["id"],),
        ).fetchall()
        return templates.TemplateResponse(
            request,
            "updates.html",
            {"business": business, "reach": reach, "recent": recent, "error": error},
            status_code=429 if recent_send else 400,
        )
    from ..notify import broadcast_to_customer

    customers = conn.execute(
        "SELECT DISTINCT customer_phone FROM bookings"
        " WHERE business_id = ? AND status != 'pending'",
        (business["id"],),
    ).fetchall()
    for row in customers:
        broadcast_to_customer(
            conn, request.app.state.wa, business, row["customer_phone"], message
        )
    conn.execute(
        "INSERT INTO broadcasts (business_id, message, sent_count, created_at)"
        " VALUES (?, ?, ?, ?)",
        (business["id"], message, len(customers), dbmod.now_iso()),
    )
    conn.commit()
    return RedirectResponse("/dashboard/updates", status_code=303)


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
    venue: str = Form("business"),
    travel_fee_ghs: float = Form(0),
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    if venue == "business":
        travel_fee_ghs = 0
    error = None
    if not name.strip():
        error = "Service name is required."
    elif duration_min < 5 or duration_min > 480:
        error = "Duration must be between 5 minutes and 8 hours."
    elif price_ghs < 0 or deposit_ghs < 0 or travel_fee_ghs < 0:
        error = "Price, deposit and travel fee cannot be negative."
    elif deposit_ghs > price_ghs:
        error = "Deposit cannot be more than the full price."
    elif venue not in dbmod.VENUES:
        error = "Choose where this service happens."
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
        "INSERT INTO services (business_id, name, duration_min, price_ghs, deposit_ghs,"
        " venue, travel_fee_ghs) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (business["id"], name.strip(), duration_min, price_ghs, deposit_ghs, venue, travel_fee_ghs),
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


def _resolve_own_location(
    conn: sqlite3.Connection, business_id: int, location_id: int | None
) -> sqlite3.Row | None:
    if location_id is None:
        location_id = dbmod.default_location_id(conn, business_id)
    return conn.execute(
        "SELECT * FROM locations WHERE id = ? AND business_id = ? AND active = 1",
        (location_id, business_id),
    ).fetchone()


@router.get("/hours", response_class=HTMLResponse)
def hours_page(
    request: Request,
    location_id: int | None = None,
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    locations = dbmod.active_locations(conn, business["id"])
    location = _resolve_own_location(conn, business["id"], location_id)
    if location is None:
        return RedirectResponse("/dashboard/locations", status_code=303)
    rows = conn.execute(
        "SELECT * FROM hours WHERE location_id = ?", (location["id"],)
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
        request,
        "hours.html",
        {
            "business": business,
            "days": days,
            "error": None,
            "locations": locations,
            "location": location,
        },
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
    raw_loc = form.get("location_id")
    location = _resolve_own_location(
        conn, business["id"], int(str(raw_loc)) if raw_loc else None
    )
    if location is None:
        return RedirectResponse("/dashboard/locations", status_code=303)
    for weekday in range(7):
        enabled = form.get(f"enabled_{weekday}")
        open_t = str(form.get(f"open_{weekday}", "") or "")
        close_t = str(form.get(f"close_{weekday}", "") or "")
        conn.execute(
            "DELETE FROM hours WHERE location_id = ? AND weekday = ?",
            (location["id"], weekday),
        )
        if enabled and open_t and close_t and open_t < close_t:
            conn.execute(
                "INSERT INTO hours (business_id, location_id, weekday, open_time, close_time)"
                " VALUES (?, ?, ?, ?, ?)",
                (business["id"], location["id"], weekday, open_t, close_t),
            )
    conn.commit()
    return RedirectResponse(f"/dashboard/hours?location_id={location['id']}", status_code=303)


@router.get("/locations", response_class=HTMLResponse)
def locations_page(
    request: Request,
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    locations = dbmod.active_locations(conn, business["id"])
    return templates.TemplateResponse(
        request, "locations.html", {"business": business, "locations": locations, "error": None}
    )


@router.post("/locations", response_class=HTMLResponse)
def add_location(
    request: Request,
    name: str = Form(...),
    area: str = Form(""),
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    if not name.strip():
        locations = dbmod.active_locations(conn, business["id"])
        return templates.TemplateResponse(
            request,
            "locations.html",
            {"business": business, "locations": locations, "error": "Branch name is required."},
            status_code=400,
        )
    dbmod.add_location(conn, business["id"], name=name.strip(), area=area.strip())
    return RedirectResponse("/dashboard/locations", status_code=303)


@router.post("/locations/{location_id}/delete")
def delete_location(
    location_id: int,
    business: sqlite3.Row | None = Depends(current_business),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if business is None:
        return login_redirect()
    active = dbmod.active_locations(conn, business["id"])
    if len(active) > 1:  # a business must keep at least one location
        conn.execute(
            "UPDATE locations SET active = 0 WHERE id = ? AND business_id = ?",
            (location_id, business["id"]),
        )
        conn.commit()
    return RedirectResponse("/dashboard/locations", status_code=303)


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
