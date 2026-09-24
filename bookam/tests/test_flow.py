"""End-to-end behavior tests: signup → services → public booking → deposit → dashboard."""
from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import add_service, open_all_days, signup, tomorrow


def _book(client: TestClient, slug: str = "adjoa-s-beauty-bar", time: str = "09:00", phone: str = "0209876543"):
    return client.post(
        f"/b/{slug}/book",
        data={
            "service_id": "1",
            "date": tomorrow(),
            "start_time": time,
            "customer_name": "Ama Serwaa",
            "customer_phone": phone,
        },
        follow_redirects=False,
    )


class TestAuth:
    def test_landing_page(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Bookam" in resp.text

    def test_signup_and_dashboard(self, client):
        signup(client)
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "Adjoa" in resp.text
        assert "/b/adjoa-s-beauty-bar" in resp.text

    def test_signup_validation(self, client):
        base = {"name": "Biz", "phone": "0241234567", "password": "secret123"}
        assert client.post("/signup", data={**base, "name": " "}).status_code == 400
        assert client.post("/signup", data={**base, "phone": "abc"}).status_code == 400
        assert client.post("/signup", data={**base, "password": "abc"}).status_code == 400

    def test_duplicate_phone_rejected(self, client):
        signup(client)
        resp = client.post(
            "/signup",
            data={"name": "Other", "phone": "0241234567", "password": "secret123"},
        )
        assert resp.status_code == 400
        assert "already exists" in resp.text

    def test_login_logout(self, client):
        signup(client)
        client.post("/logout", follow_redirects=False)
        assert client.get("/dashboard", follow_redirects=False).status_code == 303

        resp = client.post(
            "/login", data={"phone": "024 123 4567", "password": "secret123"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert client.get("/dashboard").status_code == 200

    def test_login_wrong_password(self, client):
        signup(client)
        resp = client.post("/login", data={"phone": "0241234567", "password": "nope"})
        assert resp.status_code == 401

    def test_landing_redirects_when_logged_in(self, client):
        signup(client)
        assert client.get("/", follow_redirects=False).status_code == 303

    def test_dashboard_pages_require_login(self, client):
        for path in ["/dashboard", "/dashboard/services", "/dashboard/hours", "/dashboard/customers"]:
            assert client.get(path, follow_redirects=False).status_code == 303


class TestServices:
    def test_add_and_remove_service(self, client):
        signup(client)
        add_service(client)
        resp = client.get("/dashboard/services")
        assert "Knotless braids" in resp.text
        client.post("/dashboard/services/1/delete", follow_redirects=False)
        assert "Knotless braids" not in client.get("/dashboard/services").text

    def test_service_validation(self, client):
        signup(client)
        bad = [
            {"name": " ", "duration_min": "60", "price_ghs": "100", "deposit_ghs": "20"},
            {"name": "X", "duration_min": "2", "price_ghs": "100", "deposit_ghs": "20"},
            {"name": "X", "duration_min": "60", "price_ghs": "-5", "deposit_ghs": "20"},
            {"name": "X", "duration_min": "60", "price_ghs": "100", "deposit_ghs": "150"},
        ]
        for data in bad:
            assert client.post("/dashboard/services", data=data).status_code == 400


class TestHours:
    def test_edit_hours(self, client):
        signup(client)
        open_all_days(client)
        resp = client.get("/dashboard/hours")
        assert resp.status_code == 200
        assert resp.text.count('checked') == 7

    def test_closed_day_has_no_slots(self, client):
        signup(client)
        add_service(client)
        # Close every day.
        client.post("/dashboard/hours", data={}, follow_redirects=False)
        resp = client.get(f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={tomorrow()}")
        assert resp.json() == {"slots": []}


class TestPublicBooking:
    def test_public_page(self, client):
        signup(client)
        add_service(client)
        resp = client.get("/b/adjoa-s-beauty-bar")
        assert resp.status_code == 200
        assert "Knotless braids" in resp.text

    def test_unknown_slug_404(self, client):
        assert client.get("/b/nope").status_code == 404

    def test_slots_api(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        resp = client.get(f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={tomorrow()}")
        slots = resp.json()["slots"]
        assert "09:00" in slots
        assert "16:00" in slots  # last 60-min slot before 17:00
        assert "16:30" not in slots

    def test_slots_api_bad_inputs(self, client):
        signup(client)
        add_service(client)
        assert client.get("/b/adjoa-s-beauty-bar/slots?service_id=99&date=2030-01-01").status_code == 404
        assert client.get(f"/b/adjoa-s-beauty-bar/slots?service_id=1&date=not-a-date").status_code == 400
        far = client.get("/b/adjoa-s-beauty-bar/slots?service_id=1&date=2099-01-01")
        assert far.json() == {"slots": []}

    def test_full_booking_flow_demo_payment(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)

        resp = _book(client)
        assert resp.status_code == 303
        callback = resp.headers["location"]
        assert "/pay/callback?reference=" in callback

        resp = client.get(callback, follow_redirects=True)
        assert resp.status_code == 200
        assert "Booked!" in resp.text
        assert "GH₵50" in resp.text   # deposit
        assert "GH₵200" in resp.text  # balance

        # Slot is now taken.
        slots = client.get(f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={tomorrow()}").json()["slots"]
        assert "09:00" not in slots
        assert "09:30" not in slots

        # Dashboard shows it as upcoming; customer list has the client.
        assert "Ama Serwaa" in client.get("/dashboard").text
        assert "Ama Serwaa" in client.get("/dashboard/customers").text

    def test_double_booking_conflict(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        first = _book(client)
        client.get(first.headers["location"])
        resp = _book(client, phone="0201112222")
        assert resp.status_code == 409
        assert "just taken" in resp.text

    def test_booking_validation(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        resp = client.post(
            "/b/adjoa-s-beauty-bar/book",
            data={
                "service_id": "1",
                "date": tomorrow(),
                "start_time": "09:00",
                "customer_name": "Ama",
                "customer_phone": "abc",
            },
        )
        assert resp.status_code == 400

    def test_unavailable_time_rejected(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        resp = _book(client, time="23:00")
        assert resp.status_code == 409

    def test_far_future_booking_rejected(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        resp = client.post(
            "/b/adjoa-s-beauty-bar/book",
            data={
                "service_id": "1",
                "date": "2099-01-01",
                "start_time": "09:00",
                "customer_name": "Ama",
                "customer_phone": "0209876543",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_callback_unknown_reference_404(self, client):
        assert client.get("/pay/callback?reference=nope").status_code == 404

    def test_booking_page_pending_hidden(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        resp = _book(client)
        ref = resp.headers["location"].split("reference=")[1]
        # Pending (unpaid) booking detail is not exposed.
        assert client.get(f"/booking/{ref}").status_code == 404


class TestDashboardActions:
    def _confirmed_booking_today(self, client) -> None:
        """Insert a confirmed booking dated today, bypassing the notice window."""
        import sqlite3
        from datetime import datetime, timezone

        from app import db as dbmod

        conn = sqlite3.connect(client.app.state.settings.db_path)
        conn.execute(
            "INSERT INTO bookings (business_id, service_id, customer_name, customer_phone,"
            " date, start_time, end_time, status, deposit_ghs, payment_ref, created_at)"
            " VALUES (1, 1, 'Kofi Mensah', '0501234567', ?, '10:00', '11:00', 'confirmed', 50, 'ref_today', ?)",
            (datetime.now(timezone.utc).strftime("%Y-%m-%d"), dbmod.now_iso()),
        )
        conn.commit()
        conn.close()

    def test_mark_completed_and_no_show(self, client):
        signup(client)
        add_service(client)
        self._confirmed_booking_today(client)

        resp = client.get("/dashboard")
        assert "Kofi Mensah" in resp.text
        assert "No-show" in resp.text

        client.post("/dashboard/bookings/1/status", data={"status": "no_show"}, follow_redirects=False)
        resp = client.get("/dashboard")
        assert "no show" in resp.text
        # Customer page shows the no-show count.
        assert "1 no-show" in client.get("/dashboard/customers").text

    def test_invalid_status_ignored(self, client):
        signup(client)
        add_service(client)
        self._confirmed_booking_today(client)
        client.post("/dashboard/bookings/1/status", data={"status": "hacked"}, follow_redirects=False)
        assert "confirmed" in client.get("/dashboard").text

    def test_stats_shown(self, client):
        signup(client)
        add_service(client)
        self._confirmed_booking_today(client)
        resp = client.get("/dashboard")
        assert "Deposits collected" in resp.text
        assert "GH₵50" in resp.text
