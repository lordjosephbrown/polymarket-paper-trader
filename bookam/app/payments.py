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


# Ghanaian mobile prefixes → Paystack mobile money provider codes.
_MOMO_PREFIXES = {
    "mtn": ("024", "025", "053", "054", "055", "059"),
    "vod": ("020", "050"),  # Telecel (formerly Vodafone)
    "atl": ("026", "027", "056", "057"),  # AT (AirtelTigo)
}


def momo_provider_from_phone(phone: str) -> str | None:
    digits = phone.lstrip("+")
    if digits.startswith("233") and len(digits) == 12:
        digits = "0" + digits[3:]
    for provider, prefixes in _MOMO_PREFIXES.items():
        if digits.startswith(prefixes):
            return provider
    return None


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

    def charge_momo(self, *, amount_ghs: float, phone: str, provider: str) -> str:
        """Push a MoMo approval prompt to the customer's phone (Paystack Charge API).

        Returns the transaction reference. The final result arrives via the
        Paystack webhook (charge.success). In demo mode the charge "succeeds"
        immediately — the caller should treat the reference as paid.
        """
        reference = new_reference()
        if self.demo:
            return reference
        digits = phone.lstrip("+")
        if digits.startswith("233") and len(digits) == 12:
            local = "0" + digits[3:]
        else:
            local = digits
        payload = {
            "email": f"{digits or 'customer'}@bookam.app",
            "amount": int(round(amount_ghs * 100)),
            "currency": "GHS",
            "reference": reference,
            "mobile_money": {"phone": local, "provider": provider},
        }
        resp = httpx.post(
            f"{PAYSTACK_BASE}/charge",
            json=payload,
            headers={"Authorization": f"Bearer {self.secret_key}"},
            timeout=30,
        )
        data = resp.json()
        if resp.status_code != 200 or not data.get("status"):
            raise PaymentError(str(data.get("message", "Charge failed")))
        return reference

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
