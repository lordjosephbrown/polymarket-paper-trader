"""Landing page, signup, login, logout."""
from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db as dbmod
from ..auth import hash_password, make_session_token, verify_password
from ..config import Settings
from ..main import SESSION_COOKIE, current_business, get_conn, get_settings, templates

router = APIRouter()

PHONE_RE = re.compile(r"^\+?\d{9,15}$")


def normalize_phone(phone: str) -> str:
    return re.sub(r"[\s\-()]", "", phone.strip())


def _set_session(response: RedirectResponse, business_id: int, settings: Settings) -> None:
    token = make_session_token(business_id, settings.secret_key, settings.session_ttl_seconds)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
    )


@router.get("/", response_class=HTMLResponse)
def landing(request: Request, business: sqlite3.Row | None = Depends(current_business)):
    if business:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "landing.html", {})


@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return templates.TemplateResponse(request, "signup.html", {"error": None, "form": {}})


@router.post("/signup", response_class=HTMLResponse)
def signup(
    request: Request,
    name: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    category: str = Form("other"),
    location: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    form = {"name": name.strip(), "phone": phone, "category": category, "location": location.strip()}
    phone_n = normalize_phone(phone)
    error = None
    if not form["name"]:
        error = "Business name is required."
    elif not PHONE_RE.match(phone_n):
        error = "Enter a valid phone number (e.g. 0241234567)."
    elif len(password) < 6:
        error = "Password must be at least 6 characters."
    elif conn.execute("SELECT 1 FROM businesses WHERE phone = ?", (phone_n,)).fetchone():
        error = "An account with this phone number already exists. Log in instead."
    if error:
        return templates.TemplateResponse(
            request, "signup.html", {"error": error, "form": form}, status_code=400
        )

    valid_categories = {c[0] for c in templates.env.globals["categories"]}
    biz = dbmod.create_business(
        conn,
        name=form["name"],
        phone=phone_n,
        password_hash=hash_password(password),
        category=category if category in valid_categories else "other",
        location=form["location"],
    )
    response = RedirectResponse("/dashboard?welcome=1", status_code=303)
    _set_session(response, int(biz["id"]), settings)
    return response


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    phone_n = normalize_phone(phone)
    if dbmod.rate_limited(conn, f"login:{phone_n}", limit=8, window_seconds=600):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many attempts. Wait a few minutes and try again."},
            status_code=429,
        )
    row = conn.execute("SELECT * FROM businesses WHERE phone = ?", (phone_n,)).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Wrong phone number or password."}, status_code=401
        )
    response = RedirectResponse("/dashboard", status_code=303)
    _set_session(response, int(row["id"]), settings)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {})


@router.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse(request, "terms.html", {})
