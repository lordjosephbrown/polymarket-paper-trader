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

-- A business has 1..N locations (branches). Every booking belongs to one:
-- each branch has its own hours and its own calendar.
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    name TEXT NOT NULL,                -- "Osu branch"
    area TEXT NOT NULL DEFAULT '',     -- "Osu, Accra"
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    name TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    price_ghs REAL NOT NULL,
    deposit_ghs REAL NOT NULL,
    venue TEXT NOT NULL DEFAULT 'business',  -- business|customer|both
    travel_fee_ghs REAL NOT NULL DEFAULT 0,  -- callout fee for home visits
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS hours (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    weekday INTEGER NOT NULL,          -- 0=Monday .. 6=Sunday
    open_time TEXT NOT NULL,           -- "09:00"
    close_time TEXT NOT NULL,          -- "17:00"
    UNIQUE(location_id, weekday)
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    location_id INTEGER REFERENCES locations(id),
    staff_id INTEGER REFERENCES staff(id),
    service_id INTEGER NOT NULL REFERENCES services(id),
    venue TEXT NOT NULL DEFAULT 'business',   -- where it happens
    customer_address TEXT NOT NULL DEFAULT '',-- for home visits
    travel_fee_ghs REAL NOT NULL DEFAULT 0,
    customer_name TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    date TEXT NOT NULL,                -- "YYYY-MM-DD"
    start_time TEXT NOT NULL,          -- "HH:MM"
    end_time TEXT NOT NULL,            -- "HH:MM"
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|confirmed|completed|no_show|cancelled
    deposit_ghs REAL NOT NULL,
    payment_ref TEXT UNIQUE,
    reminded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bookings_biz_date ON bookings(business_id, date);
CREATE INDEX IF NOT EXISTS idx_services_biz ON services(business_id);

-- WhatsApp bot conversation state, keyed by customer phone (wa_id).
CREATE TABLE IF NOT EXISTS conversations (
    phone TEXT PRIMARY KEY,
    business_id INTEGER,
    state TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

-- Broadcast announcements a business sends to its customers ("we moved",
-- "new service", "price update"). Kept for rate limiting + audit.
CREATE TABLE IF NOT EXISTS broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    message TEXT NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Staff members (optional). No staff rows = solo business, capacity 1 per
-- location. With staff, a location can take as many overlapping bookings as
-- it has active staff, and customers may request a specific person.
CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

-- One review per completed booking, collected via WhatsApp or the receipt page.
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    rating INTEGER NOT NULL,           -- 1..5
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- Audit trail: every booking status change, who made it, when.
CREATE TABLE IF NOT EXISTS booking_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings(id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,               -- business|customer|system
    created_at TEXT NOT NULL
);

-- Ledger of payment lifecycle events for reconciliation and audit.
CREATE TABLE IF NOT EXISTS payment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reference TEXT NOT NULL,
    kind TEXT NOT NULL,                -- initialized|verified|failed|webhook_success|expired
    amount_ghs REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payment_events_ref ON payment_events(reference);

-- Customers who opted out of broadcast updates ("stop" on WhatsApp).
CREATE TABLE IF NOT EXISTS broadcast_optouts (
    phone TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

-- Sliding-window rate limiting (login attempts, public booking spam).
CREATE TABLE IF NOT EXISTS rate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_events_key ON rate_events(key, created_at);

-- Every outbound WhatsApp message. In demo mode this is the only delivery;
-- in live mode it doubles as an audit log.
CREATE TABLE IF NOT EXISTS wa_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    to_phone TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

MIGRATIONS = [
    "ALTER TABLE bookings ADD COLUMN reminded INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE services ADD COLUMN venue TEXT NOT NULL DEFAULT 'business'",
    "ALTER TABLE services ADD COLUMN travel_fee_ghs REAL NOT NULL DEFAULT 0",
    "ALTER TABLE bookings ADD COLUMN location_id INTEGER REFERENCES locations(id)",
    "ALTER TABLE bookings ADD COLUMN venue TEXT NOT NULL DEFAULT 'business'",
    "ALTER TABLE bookings ADD COLUMN customer_address TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE bookings ADD COLUMN travel_fee_ghs REAL NOT NULL DEFAULT 0",
    "ALTER TABLE hours ADD COLUMN location_id INTEGER REFERENCES locations(id)",
    "ALTER TABLE bookings ADD COLUMN staff_id INTEGER REFERENCES staff(id)",
]

VENUES = {"business", "customer", "both"}

# Statuses that occupy a calendar slot. Full lifecycle:
# pending → confirmed → on_the_way → in_progress → completed
#                     ↘ no_show / cancelled
ACTIVE_STATUSES = ("confirmed", "on_the_way", "in_progress")

# Which delivery-status moves the business may make from each state.
STATUS_TRANSITIONS: dict[str, set[str]] = {
    "confirmed": {"on_the_way", "in_progress", "completed", "no_show", "cancelled"},
    "on_the_way": {"in_progress", "completed", "cancelled"},
    "in_progress": {"completed"},
}

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
    for migration in MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # column already exists (fresh schema or already migrated)
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
    add_location(conn, biz_id, name="Main location", area=location)
    conn.commit()
    row = conn.execute("SELECT * FROM businesses WHERE id = ?", (biz_id,)).fetchone()
    assert row is not None
    return row


def add_location(
    conn: sqlite3.Connection, business_id: int, *, name: str, area: str = ""
) -> int:
    """Create a location with the default weekly hours. Returns the location id."""
    cur = conn.execute(
        "INSERT INTO locations (business_id, name, area) VALUES (?, ?, ?)",
        (business_id, name, area),
    )
    location_id = cur.lastrowid
    for weekday, open_t, close_t in DEFAULT_HOURS:
        conn.execute(
            "INSERT INTO hours (business_id, location_id, weekday, open_time, close_time)"
            " VALUES (?, ?, ?, ?, ?)",
            (business_id, location_id, weekday, open_t, close_t),
        )
    conn.commit()
    assert location_id is not None
    return location_id


def active_locations(conn: sqlite3.Connection, business_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM locations WHERE business_id = ? AND active = 1 ORDER BY id",
        (business_id,),
    ).fetchall()


def default_location_id(conn: sqlite3.Connection, business_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM locations WHERE business_id = ? AND active = 1 ORDER BY id LIMIT 1",
        (business_id,),
    ).fetchone()
    return row["id"] if row else None


def log_booking_event(
    conn: sqlite3.Connection, booking_id: int, from_status: str, to_status: str, actor: str
) -> None:
    conn.execute(
        "INSERT INTO booking_events (booking_id, from_status, to_status, actor, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (booking_id, from_status, to_status, actor, now_iso()),
    )
    conn.commit()


def log_payment_event(
    conn: sqlite3.Connection, reference: str, kind: str, amount_ghs: float = 0
) -> None:
    conn.execute(
        "INSERT INTO payment_events (reference, kind, amount_ghs, created_at) VALUES (?, ?, ?, ?)",
        (reference, kind, amount_ghs, now_iso()),
    )
    conn.commit()


def rate_limited(
    conn: sqlite3.Connection, key: str, limit: int, window_seconds: int
) -> bool:
    """Record one event for `key` and report whether the window limit is now exceeded."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=window_seconds)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM rate_events WHERE key = ? AND created_at < ?", (key, cutoff))
    conn.execute(
        "INSERT INTO rate_events (key, created_at) VALUES (?, ?)",
        (key, now.isoformat(timespec="seconds")),
    )
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM rate_events WHERE key = ? AND created_at >= ?",
        (key, cutoff),
    ).fetchone()["n"]
    return count > limit
