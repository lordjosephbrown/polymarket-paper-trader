"""FastAPI app factory and shared dependencies."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db as dbmod
from .auth import parse_session_token
from .config import Settings, load_settings
from .payments import PaymentProvider

APP_DIR = Path(__file__).parent

SESSION_COOKIE = "bookam_session"

CATEGORIES = [
    ("salon", "Hair salon"),
    ("barber", "Barbershop"),
    ("nails", "Nails & lashes"),
    ("makeup", "Makeup artist"),
    ("tailor", "Tailor / seamstress"),
    ("spa", "Spa & massage"),
    ("clinic", "Clinic / therapist"),
    ("photo", "Photographer"),
    ("tutor", "Tutor / lessons"),
    ("other", "Other"),
]

templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.globals["categories"] = CATEGORIES


def ghs(amount: float) -> str:
    text = f"{amount:,.2f}"
    if text.endswith(".00"):
        text = text[:-3]
    return f"GH₵{text}"


templates.env.filters["ghs"] = ghs
templates.env.filters["urlquote"] = lambda s: quote(str(s), safe="")


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = dbmod.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_payments(request: Request) -> PaymentProvider:
    return request.app.state.payments  # type: ignore[no-any-return]


def current_business(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> sqlite3.Row | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    business_id = parse_session_token(token, settings.secret_key)
    if business_id is None:
        return None
    return conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Bookam", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.payments = PaymentProvider(settings.paystack_secret_key)
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

    from .routes.auth_routes import router as auth_router
    from .routes.dashboard import router as dashboard_router
    from .routes.public import router as public_router

    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(public_router)
    return app
