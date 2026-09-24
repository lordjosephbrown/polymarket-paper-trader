"""Business notifications, reminders, Paystack webhook, and web cancellation."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import payments as pay
from app.payments import momo_provider_from_phone
from app.reminders import scan_and_send
from app.wa import to_wa_number
from conftest import add_service, open_all_days, signup, tomorrow

BIZ_WA = "233241234567"


def outbox_texts(client: TestClient, to: str) -> list[str]:
    conn = sqlite3.connect(client.app.state.settings.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT body FROM wa_outbox WHERE to_phone = ? ORDER BY id", (to,)
    ).fetchall()
    conn.close()
    return [r["body"] for r in rows]


def book_web(client: TestClient, time: str = "09:00") -> str:
    resp = client.post(
        "/b/adjoa-s-beauty-bar/book",
        data={
            "service_id": "1",
            "date": tomorrow(),
            "start_time": time,
            "customer_name": "Ama Serwaa",
            "customer_phone": "0209876543",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    callback = resp.headers["location"]
    client.get(callback)
    return callback.split("reference=")[1]


class TestHelpers:
    def test_to_wa_number(self):
        assert to_wa_number("0241234567") == "233241234567"
        assert to_wa_number("+233241234567") == "233241234567"
        assert to_wa_number("233241234567") == "233241234567"

    def test_momo_provider_inference(self):
        assert momo_provider_from_phone("0241234567") == "mtn"
        assert momo_provider_from_phone("233541234567") == "mtn"
        assert momo_provider_from_phone("0501234567") == "vod"
        assert momo_provider_from_phone("0271234567") == "atl"
        assert momo_provider_from_phone("0111234567") is None


class TestBusinessNotifications:
    def test_web_booking_notifies_business(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        book_web(client)
        texts = outbox_texts(client, BIZ_WA)
        assert any("New booking" in t and "Ama Serwaa" in t for t in texts)

    def test_web_cancellation_notifies_business_and_frees_slot(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        ref = book_web(client)

        resp = client.post(f"/booking/{ref}/cancel")
        assert resp.status_code == 200
        assert "cancelled" in resp.text.lower()
        assert any("Cancelled" in t for t in outbox_texts(client, BIZ_WA))
        slots = client.get(
            f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={tomorrow()}"
        ).json()["slots"]
        assert "09:00" in slots

    def test_cancel_unknown_reference_404(self, client):
        assert client.post("/booking/nope/cancel").status_code == 404


class TestReminders:
    def _insert_confirmed(self, client, start_hhmm: str, date_offset_days: int = 0) -> int:
        conn = sqlite3.connect(client.app.state.settings.db_path)
        date_str = (
            datetime.now(timezone.utc) + timedelta(days=date_offset_days)
        ).strftime("%Y-%m-%d")
        cur = conn.execute(
            "INSERT INTO bookings (business_id, service_id, customer_name, customer_phone,"
            " date, start_time, end_time, status, deposit_ghs, payment_ref, created_at)"
            " VALUES (1, 1, 'Kofi', '233501234567', ?, ?, ?, 'confirmed', 50, ?, ?)",
            (date_str, start_hhmm, start_hhmm, f"ref_{start_hhmm}_{date_offset_days}", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()
        return cur.lastrowid

    def test_reminder_sent_once_within_window(self, client):
        signup(client)
        add_service(client)
        soon_dt = datetime.now(timezone.utc) + timedelta(minutes=90)
        soon = soon_dt.strftime("%H:%M")
        # If 90 minutes from now lands after midnight, the booking is dated tomorrow —
        # the scan handles the midnight-crossing window either way.
        offset = 1 if soon_dt.date() != datetime.now(timezone.utc).date() else 0
        self._insert_confirmed(client, soon, date_offset_days=offset)
        settings = client.app.state.settings
        sent = scan_and_send(settings.db_path, client.app.state.wa)
        assert sent == 1
        texts = outbox_texts(client, "233501234567")
        assert any("Reminder" in t for t in texts)
        # Idempotent: second scan sends nothing.
        assert scan_and_send(settings.db_path, client.app.state.wa) == 0

    def test_no_reminder_for_future_days(self, client):
        signup(client)
        add_service(client)
        self._insert_confirmed(client, "12:00", date_offset_days=3)
        assert scan_and_send(client.app.state.settings.db_path, client.app.state.wa) == 0


class TestPaystackWebhook:
    def test_webhook_confirms_pending_booking(self, settings, monkeypatch):
        from app.main import create_app

        app = create_app(settings)
        with TestClient(app) as client:
            signup(client)
            add_service(client)
            open_all_days(client)
            # Create a pending booking (don't visit the callback).
            resp = client.post(
                "/b/adjoa-s-beauty-bar/book",
                data={
                    "service_id": "1",
                    "date": tomorrow(),
                    "start_time": "09:00",
                    "customer_name": "Ama",
                    "customer_phone": "0209876543",
                },
                follow_redirects=False,
            )
            ref = resp.headers["location"].split("reference=")[1]

            event = {"event": "charge.success", "data": {"reference": ref}}
            resp = client.post("/pay/webhook", json=event)
            assert resp.status_code == 200

            # Confirmed: receipt page now exists, business notified, customer messaged.
            assert client.get(f"/booking/{ref}").status_code == 200
            assert any("New booking" in t for t in outbox_texts(client, BIZ_WA))
            assert any("Booked!" in t for t in outbox_texts(client, "233209876543"))

    def test_webhook_signature_enforced_with_key(self, settings):
        from app.main import create_app

        settings.paystack_secret_key = "sk_test_abc"
        app = create_app(settings)
        with TestClient(app) as client:
            body = json.dumps({"event": "charge.success", "data": {"reference": "x"}}).encode()
            assert client.post("/pay/webhook", content=body).status_code == 403
            sig = hmac.new(b"sk_test_abc", body, hashlib.sha512).hexdigest()
            resp = client.post(
                "/pay/webhook", content=body, headers={"X-Paystack-Signature": sig}
            )
            assert resp.status_code == 200  # unknown reference is a no-op, not an error

    def test_webhook_ignores_other_events(self, client):
        resp = client.post("/pay/webhook", json={"event": "transfer.success", "data": {}})
        assert resp.status_code == 200

    def test_webhook_bad_json(self, client):
        assert client.post("/pay/webhook", content=b"junk").status_code == 400


class TestMomoCharge:
    def test_demo_charge_returns_reference(self):
        p = pay.PaymentProvider("")
        ref = p.charge_momo(amount_ghs=50, phone="233241234567", provider="mtn")
        assert ref.startswith("bkm_")

    def test_live_charge_payload(self, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"status": True, "data": {"status": "pay_offline"}}

        def fake_post(url, json, headers, timeout):
            captured.update({"url": url, "json": json})
            return FakeResponse()

        monkeypatch.setattr(pay.httpx, "post", fake_post)
        p = pay.PaymentProvider("sk_test_123")
        ref = p.charge_momo(amount_ghs=50, phone="233241234567", provider="mtn")
        assert captured["url"].endswith("/charge")
        assert captured["json"]["mobile_money"] == {"phone": "0241234567", "provider": "mtn"}
        assert captured["json"]["amount"] == 5000
        assert captured["json"]["reference"] == ref

    def test_live_charge_error(self, monkeypatch):
        class FakeResponse:
            status_code = 400

            @staticmethod
            def json():
                return {"status": False, "message": "insufficient funds"}

        monkeypatch.setattr(pay.httpx, "post", lambda *a, **k: FakeResponse())
        p = pay.PaymentProvider("sk_test_123")
        import pytest

        with pytest.raises(pay.PaymentError, match="insufficient funds"):
            p.charge_momo(amount_ghs=50, phone="0241234567", provider="mtn")
