"""WhatsApp Cloud API webhook: Meta verification handshake + inbound messages."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..bot import Bot
from ..config import Settings
from ..main import get_conn, get_payments, get_settings
from ..payments import PaymentProvider

router = APIRouter()


@router.get("/wa/webhook")
def verify_webhook(request: Request, settings: Settings = Depends(get_settings)):
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == settings.wa_verify_token
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="Verification failed")


def _valid_signature(settings: Settings, raw: bytes, header: str | None) -> bool:
    if not settings.wa_app_secret:
        return True  # demo mode — no secret configured
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.wa_app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


def _extract_messages(payload: dict) -> list[tuple[str, str, str]]:
    """Yield (from_phone, input_text, profile_name) for each inbound message."""
    out: list[tuple[str, str, str]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {
                c.get("wa_id"): c.get("profile", {}).get("name", "")
                for c in value.get("contacts", [])
            }
            for msg in value.get("messages", []):
                sender = msg.get("from", "")
                if not sender:
                    continue
                kind = msg.get("type")
                if kind == "text":
                    text = msg.get("text", {}).get("body", "")
                elif kind == "interactive":
                    interactive = msg.get("interactive", {})
                    reply = interactive.get("list_reply") or interactive.get("button_reply") or {}
                    text = reply.get("id", "")
                elif kind == "location":
                    # A shared pin — render as an address-friendly string with a maps link.
                    loc = msg.get("location", {})
                    lat, lon = loc.get("latitude"), loc.get("longitude")
                    parts = [p for p in (loc.get("name", ""), loc.get("address", "")) if p]
                    label = ", ".join(parts)
                    text = (label + " — " if label else "") + f"📍 https://maps.google.com/?q={lat},{lon}"
                elif kind in ("audio", "voice"):
                    text = "__voice_note__"
                else:
                    text = ""
                out.append((sender, text, names.get(sender, "")))
    return out


@router.post("/wa/webhook")
async def receive_webhook(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    payments: PaymentProvider = Depends(get_payments),
):
    raw = await request.body()
    if not _valid_signature(settings, raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403, detail="Bad signature")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Bad payload")
    bot = Bot(settings, payments, request.app.state.wa)
    for sender, text, name in _extract_messages(payload):
        bot.handle(conn, sender, text, name)
    return {"ok": True}
