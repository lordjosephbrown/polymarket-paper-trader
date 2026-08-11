"""Paystack integration with a zero-config demo mode.

With PAYSTACK_SECRET_KEY set, deposits are collected for real via Paystack
(mobile money + card). Without it, payments auto-succeed so the whole app
works out of the box for evaluation.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

import httpx

PAYSTACK_BASE = "https://api.paystack.co"


class PaymentError(Exception):
    pass


@dataclass
class InitResult:
    reference: str
    authorization_url: str


def new_reference() -> str:
    return f"bkm_{secrets.token_hex(10)}"


class PaymentProvider:
    """Real Paystack when a secret key is present; demo mode otherwise."""

    def __init__(self, secret_key: str = "") -> None:
        self.secret_key = secret_key

    @property
    def demo(self) -> bool:
        return not self.secret_key

    def initialize(self, *, amount_ghs: float, customer_phone: str, callback_url: str) -> InitResult:
        reference = new_reference()
        if self.demo:
            sep = "&" if "?" in callback_url else "?"
            return InitResult(reference, f"{callback_url}{sep}reference={reference}")
        payload = {
            "email": f"{customer_phone.lstrip('+') or 'customer'}@bookam.app",
            "amount": int(round(amount_ghs * 100)),  # pesewas
            "currency": "GHS",
            "reference": reference,
            "callback_url": callback_url,
            "channels": ["mobile_money", "card"],
        }
        resp = httpx.post(
            f"{PAYSTACK_BASE}/transaction/initialize",
            json=payload,
            headers={"Authorization": f"Bearer {self.secret_key}"},
            timeout=30,
        )
        data = resp.json()
        if resp.status_code != 200 or not data.get("status"):
            raise PaymentError(str(data.get("message", "Payment initialization failed")))
        return InitResult(reference, data["data"]["authorization_url"])

    def verify(self, reference: str) -> bool:
        """True if the transaction for this reference succeeded."""
        if self.demo:
            return True
        resp = httpx.get(
            f"{PAYSTACK_BASE}/transaction/verify/{reference}",
            headers={"Authorization": f"Bearer {self.secret_key}"},
            timeout=30,
        )
        data = resp.json()
        if resp.status_code != 200 or not data.get("status"):
            return False
        return data["data"].get("status") == "success"
