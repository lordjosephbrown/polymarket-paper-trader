"""SQLite schema and helpers. Plain sqlite3, WAL mode, row dicts."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    location TEXT NOT NULL DEFAULT '',
    about TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    name TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    price_ghs REAL NOT NULL,
    deposit_ghs REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS hours (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    weekday INTEGER NOT NULL,          -- 0=Monday .. 6=Sunday
    open_time TEXT NOT NULL,           -- "09:00"
    close_time TEXT NOT NULL,          -- "17:00"
    UNIQUE(business_id, weekday)
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    service_id INTEGER NOT NULL REFERENCES services(id),
    customer_name TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    date TEXT NOT NULL,                -- "YYYY-MM-DD"
    start_time TEXT NOT NULL,          -- "HH:MM"
    end_time TEXT NOT NULL,            -- "HH:MM"
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|confirmed|completed|no_show|cancelled
    deposit_ghs REAL NOT NULL,
    payment_ref TEXT UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bookings_biz_date ON bookings(business_id, date);
CREATE INDEX IF NOT EXISTS idx_services_biz ON services(business_id);
"""

def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: each request gets its own connection, but FastAPI may
    # open it (sync dependency, threadpool) and use it (async handler, event loop)
    # on different threads.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "business"


def unique_slug(conn: sqlite3.Connection, name: str) -> str:
    base = slugify(name)
    slug = base
    n = 2
    while conn.execute("SELECT 1 FROM businesses WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


DEFAULT_HOURS: list[tuple[int, str, str]] = [
    (0, "09:00", "17:00"),
    (1, "09:00", "17:00"),
    (2, "09:00", "17:00"),
    (3, "09:00", "17:00"),
    (4, "09:00", "17:00"),
    (5, "09:00", "17:00"),  # Saturday
]


def create_business(
    conn: sqlite3.Connection,
    *,
    name: str,
    phone: str,
    password_hash: str,
    category: str = "other",
    location: str = "",
) -> sqlite3.Row:
    slug = unique_slug(conn, name)
    cur = conn.execute(
        "INSERT INTO businesses (slug, name, phone, password_hash, category, location, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (slug, name, phone, password_hash, category, location, now_iso()),
    )
    biz_id = cur.lastrowid
    for weekday, open_t, close_t in DEFAULT_HOURS:
        conn.execute(
            "INSERT INTO hours (business_id, weekday, open_time, close_time) VALUES (?, ?, ?, ?)",
            (biz_id, weekday, open_t, close_t),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM businesses WHERE id = ?", (biz_id,)).fetchone()
    assert row is not None
    return row
