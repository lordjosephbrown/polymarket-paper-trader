"""WhatsApp Business Cloud API client.

Every outbound message is recorded in the wa_outbox table. When Meta credentials
are configured the message is also sent for real; without them (demo mode) the
outbox is the delivery, which is what the tests assert against.
"""
from __future__ import annotations

import json
import sqlite3

import httpx

from . import db as dbmod

GRAPH_BASE = "https://graph.facebook.com/v20.0"


def to_wa_number(phone: str) -> str:
    """Normalize a Ghanaian phone number to international wa_id format (no +)."""
    digits = phone.lstrip("+")
    if digits.startswith("0") and len(digits) == 10:
        return "233" + digits[1:]
    return digits


def text_message(body: str) -> dict:
    return {"type": "text", "text": {"body": body}}


def list_message(body: str, button: str, rows: list[dict], header: str = "") -> dict:
    """rows: [{"id": ..., "title": ..., "description": ...}] — max 10, titles max 24 chars."""
    interactive: dict = {
        "type": "list",
        "body": {"text": body},
        "action": {"button": button, "sections": [{"title": header or "Options", "rows": rows[:10]}]},
    }
    return {"type": "interactive", "interactive": interactive}


def buttons_message(body: str, buttons: list[dict]) -> dict:
    """buttons: [{"id": ..., "title": ...}] — max 3, titles max 20 chars."""
    interactive = {
        "type": "button",
        "body": {"text": body},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}} for b in buttons[:3]
            ]
        },
    }
    return {"type": "interactive", "interactive": interactive}


class WhatsAppClient:
    def __init__(self, token: str = "", phone_id: str = "") -> None:
        self.token = token
        self.phone_id = phone_id

    @property
    def live(self) -> bool:
        return bool(self.token and self.phone_id)

    def send(self, conn: sqlite3.Connection, to_phone: str, payload: dict) -> None:
        to = to_wa_number(to_phone)
        conn.execute(
            "INSERT INTO wa_outbox (to_phone, kind, body, created_at) VALUES (?, ?, ?, ?)",
            (to, payload.get("type", "unknown"), json.dumps(payload), dbmod.now_iso()),
        )
        conn.commit()
        if not self.live:
            return
        try:
            httpx.post(
                f"{GRAPH_BASE}/{self.phone_id}/messages",
                json={"messaging_product": "whatsapp", "to": to, **payload},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=30,
            )
        except httpx.HTTPError:
            # Delivery is best-effort; the booking flow must not die on a send failure.
            pass

    def send_text(self, conn: sqlite3.Connection, to_phone: str, body: str) -> None:
        self.send(conn, to_phone, text_message(body))
