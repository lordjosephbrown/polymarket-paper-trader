"""Production-hardening: rate limits, audit trail, payment ledger, reconciliation,
broadcast opt-out, exports, reports, health, security headers, legal pages."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import db as dbmod
from app.reminders import reconcile_pending
from conftest import add_service, open_all_days, signup, tomorrow


def rows(client: TestClient, sql: str, *params) -> list[sqlite3.Row]:
    conn = sqlite3.connect(client.app.state.settings.db_path)
    conn.row_factory = sqlite3.Row
    out = conn.execute(sql, params).fetchall()
    conn.close()
    return out


def book_web(client: TestClient, time: str = "09:00") -> str:
    resp = client.post(
        "/b/adjoa-s-beauty-bar/book",
        data={
            "service_id": "1",
            "date": tomorrow(),
            "start_time": time,
            "customer_name": "Ama",
            "customer_phone": "0209876543",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    callback = resp.headers["location"]
    client.get(callback)
    return callback.split("reference=")[1]


class TestRateLimits:
    def test_login_rate_limited(self, client):
        signup(client)
        client.post("/logout", follow_redirects=False)
        for _ in range(8):
            client.post("/login", data={"phone": "0241234567", "password": "wrong"})
        resp = client.post("/login", data={"phone": "0241234567", "password": "secret123"})
        assert resp.status_code == 429
        assert "Too many attempts" in resp.text

    def test_booking_rate_limited(self, client):
        signup(client)
        add_service(client, duration_min=30)
        open_all_days(client)
        statuses = []
        for i in range(12):
            resp = client.post(
                "/b/adjoa-s-beauty-bar/book",
                data={
                    "service_id": "1",
                    "date": tomorrow(),
                    "start_time": f"{9 + i // 2:02d}:{'30' if i % 2 else '00'}",
                    "customer_name": "Spammer",
                    "customer_phone": "0501234567",
                },
                follow_redirects=False,
            )
            statuses.append(resp.status_code)
        assert 429 in statuses


class TestAuditTrail:
    def test_lifecycle_events_recorded(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        book_web(client)  # pending → confirmed (system)
        client.post("/dashboard/bookings/1/status", data={"status": "completed"}, follow_redirects=False)
        events = rows(client, "SELECT * FROM booking_events ORDER BY id")
        transitions = [(e["from_status"], e["to_status"], e["actor"]) for e in events]
        assert ("pending", "confirmed", "system") in transitions
        assert ("confirmed", "completed", "business") in transitions

    def test_payment_ledger_recorded(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        ref = book_web(client)
        kinds = [e["kind"] for e in rows(client, "SELECT * FROM payment_events WHERE reference = ?", ref)]
        assert "initialized" in kinds
        assert "verified" in kinds


class TestReconciliation:
    class FakePayments:
        demo = False

        def __init__(self, paid: bool):
            self.paid = paid

        def verify(self, ref: str) -> bool:
            return self.paid

    def _pending_booking(self, client, minutes_old: int) -> None:
        conn = sqlite3.connect(client.app.state.settings.db_path)
        created = (
            datetime.now(timezone.utc) - timedelta(minutes=minutes_old)
        ).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO bookings (business_id, location_id, service_id, customer_name,"
            " customer_phone, date, start_time, end_time, status, deposit_ghs, payment_ref, created_at)"
            " VALUES (1, 1, 1, 'Ama', '0209876543', ?, '10:00', '11:00', 'pending', 50, 'stuck_ref', ?)",
            (tomorrow(), created),
        )
        conn.commit()
        conn.close()

    def test_paid_but_stuck_booking_confirms(self, client):
        signup(client)
        add_service(client)
        self._pending_booking(client, minutes_old=10)
        settings = client.app.state.settings
        n = reconcile_pending(settings.db_path, self.FakePayments(paid=True), client.app.state.wa)
        assert n == 1
        assert rows(client, "SELECT status FROM bookings")[0]["status"] == "confirmed"
        # Business was notified of the recovered booking.
        assert any(
            "New booking" in r["body"]
            for r in rows(client, "SELECT body FROM wa_outbox WHERE to_phone = '233241234567'")
        )

    def test_unpaid_expired_booking_cancels(self, client):
        signup(client)
        add_service(client)
        self._pending_booking(client, minutes_old=30)
        settings = client.app.state.settings
        n = reconcile_pending(settings.db_path, self.FakePayments(paid=False), client.app.state.wa)
        assert n == 1
        assert rows(client, "SELECT status FROM bookings")[0]["status"] == "cancelled"
        kinds = [e["kind"] for e in rows(client, "SELECT * FROM payment_events")]
        assert "expired" in kinds

    def test_recent_unpaid_booking_left_alone(self, client):
        signup(client)
        add_service(client)
        self._pending_booking(client, minutes_old=5)
        n = reconcile_pending(
            client.app.state.settings.db_path, self.FakePayments(paid=False), client.app.state.wa
        )
        assert n == 0
        assert rows(client, "SELECT status FROM bookings")[0]["status"] == "pending"

    def test_demo_mode_skips_reconciliation(self, client):
        signup(client)
        add_service(client)
        self._pending_booking(client, minutes_old=30)
        n = reconcile_pending(
            client.app.state.settings.db_path, client.app.state.payments, client.app.state.wa
        )
        assert n == 0


class TestBroadcastOptout:
    def _wa_text(self, client, body, sender="233209876543"):
        client.post(
            "/wa/webhook",
            json={
                "entry": [
                    {"changes": [{"value": {"messages": [{"from": sender, "type": "text", "text": {"body": body}}]}}]}
                ]
            },
        )

    def test_stop_excludes_from_broadcast_and_start_rejoins(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        book_web(client)  # customer 0209876543

        self._wa_text(client, "stop")
        client.post("/dashboard/updates", data={"message": "Big promo!"}, follow_redirects=False)
        texts = [r["body"] for r in rows(client, "SELECT body FROM wa_outbox WHERE to_phone = '233209876543'")]
        assert not any("Big promo" in t for t in texts)
        assert any("won't receive business updates" in t for t in texts)

        self._wa_text(client, "start")
        assert rows(client, "SELECT * FROM broadcast_optouts") == []


class TestReportsAndExports:
    def test_reports_page(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        book_web(client)
        resp = client.get("/dashboard/reports")
        assert resp.status_code == 200
        assert "Knotless braids" in resp.text
        assert "GH₵50" in resp.text

    def test_csv_exports(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        book_web(client)
        resp = client.get("/dashboard/export/bookings.csv")
        assert resp.status_code == 200
        assert "attachment" in resp.headers["content-disposition"]
        assert "Knotless braids" in resp.text
        assert "Ama" in resp.text
        resp = client.get("/dashboard/export/customers.csv")
        assert "0209876543" in resp.text

    def test_exports_require_login(self, client):
        assert client.get("/dashboard/export/bookings.csv", follow_redirects=False).status_code == 303


class TestPlumbing:
    def test_healthz(self, client):
        assert client.get("/healthz").json() == {"ok": True}

    def test_security_headers(self, client):
        resp = client.get("/")
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"

    def test_legal_pages(self, client):
        assert "Data Protection Act" in client.get("/privacy").text
        assert "Deposits" in client.get("/terms").text
