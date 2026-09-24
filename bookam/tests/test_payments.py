from __future__ import annotations

import pytest

from app import payments as pay
from app.payments import PaymentError, PaymentProvider


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class TestDemoMode:
    def test_initialize_returns_callback(self):
        p = PaymentProvider("")
        assert p.demo
        result = p.initialize(amount_ghs=50, customer_phone="0241234567", callback_url="http://x/cb")
        assert result.authorization_url == f"http://x/cb?reference={result.reference}"
        assert result.reference.startswith("bkm_")

    def test_initialize_appends_to_existing_query(self):
        p = PaymentProvider("")
        result = p.initialize(amount_ghs=50, customer_phone="024", callback_url="http://x/cb?a=1")
        assert f"&reference={result.reference}" in result.authorization_url

    def test_verify_always_true(self):
        assert PaymentProvider("").verify("anything")


class TestPaystackMode:
    def test_initialize_success(self, monkeypatch):
        captured: dict = {}

        def fake_post(url, json, headers, timeout):
            captured.update({"url": url, "json": json, "headers": headers})
            return FakeResponse(200, {"status": True, "data": {"authorization_url": "https://pay.me/x"}})

        monkeypatch.setattr(pay.httpx, "post", fake_post)
        p = PaymentProvider("sk_test_123")
        result = p.initialize(amount_ghs=50.5, customer_phone="+233241234567", callback_url="http://x/cb")
        assert result.authorization_url == "https://pay.me/x"
        assert captured["json"]["amount"] == 5050  # pesewas
        assert captured["json"]["currency"] == "GHS"
        assert captured["json"]["channels"] == ["mobile_money", "card"]
        assert captured["json"]["email"] == "233241234567@bookam.app"
        assert captured["headers"]["Authorization"] == "Bearer sk_test_123"

    def test_initialize_failure_raises(self, monkeypatch):
        monkeypatch.setattr(
            pay.httpx, "post", lambda *a, **k: FakeResponse(400, {"status": False, "message": "bad key"})
        )
        with pytest.raises(PaymentError, match="bad key"):
            PaymentProvider("sk_test_123").initialize(
                amount_ghs=50, customer_phone="024", callback_url="http://x/cb"
            )

    def test_verify_success(self, monkeypatch):
        monkeypatch.setattr(
            pay.httpx, "get", lambda *a, **k: FakeResponse(200, {"status": True, "data": {"status": "success"}})
        )
        assert PaymentProvider("sk_test_123").verify("ref")

    def test_verify_failed_transaction(self, monkeypatch):
        monkeypatch.setattr(
            pay.httpx, "get", lambda *a, **k: FakeResponse(200, {"status": True, "data": {"status": "failed"}})
        )
        assert not PaymentProvider("sk_test_123").verify("ref")

    def test_verify_api_error(self, monkeypatch):
        monkeypatch.setattr(
            pay.httpx, "get", lambda *a, **k: FakeResponse(500, {"status": False})
        )
        assert not PaymentProvider("sk_test_123").verify("ref")


class TestFailedPaymentFlow:
    def test_failed_payment_shows_retry_page(self, settings, monkeypatch):
        from fastapi.testclient import TestClient

        from app.main import create_app
        from conftest import add_service, open_all_days, signup, tomorrow

        app = create_app(settings)
        # Demo provider for initialize, but force verify to fail.
        monkeypatch.setattr(app.state.payments, "verify", lambda ref: False)
        with TestClient(app) as client:
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
                    "customer_phone": "0209876543",
                },
                follow_redirects=False,
            )
            resp = client.get(resp.headers["location"])
            assert resp.status_code == 402
            assert "didn't go through" in resp.text
            assert "Try again" in resp.text
            # The failed booking is cancelled, so the slot is free again.
            slots = client.get(
                f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={tomorrow()}"
            ).json()["slots"]
            assert "09:00" in slots
