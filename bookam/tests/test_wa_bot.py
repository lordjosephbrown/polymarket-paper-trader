"""WhatsApp bot behavior: full in-chat booking, cancellation, edge cases.

All in demo mode — outbound messages land in wa_outbox, which is the assertion
surface. Inbound messages are simulated via the /wa/webhook endpoint exactly as
Meta would deliver them.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from conftest import add_service, open_all_days, signup

CUSTOMER = "233209876543"


def wa_text(client: TestClient, body: str, sender: str = CUSTOMER, name: str = "Ama"):
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": sender, "profile": {"name": name}}],
                            "messages": [
                                {"from": sender, "type": "text", "text": {"body": body}}
                            ],
                        }
                    }
                ]
            }
        ]
    }
    resp = client.post("/wa/webhook", json=payload)
    assert resp.status_code == 200
    return resp


def wa_reply(client: TestClient, reply_id: str, sender: str = CUSTOMER):
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": sender,
                                    "type": "interactive",
                                    "interactive": {"list_reply": {"id": reply_id, "title": "x"}},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    resp = client.post("/wa/webhook", json=payload)
    assert resp.status_code == 200
    return resp


def outbox(client: TestClient, to: str = CUSTOMER) -> list[dict]:
    conn = sqlite3.connect(client.app.state.settings.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM wa_outbox WHERE to_phone = ? ORDER BY id", (to,)
    ).fetchall()
    conn.close()
    return [dict(r, body=json.loads(r["body"])) for r in rows]


def last_message_text(client: TestClient, to: str = CUSTOMER) -> str:
    msgs = outbox(client, to)
    assert msgs, f"no messages sent to {to}"
    return json.dumps(msgs[-1]["body"])


def setup_business(client: TestClient) -> None:
    signup(client)
    add_service(client)
    open_all_days(client)


class TestBotConversation:
    def test_greeting_for_unknown_text(self, client):
        wa_text(client, "hello")
        assert "Welcome to Bookam" in last_message_text(client)

    def test_unknown_business_code(self, client):
        wa_text(client, "book nonexistent-place")
        assert "couldn't find" in last_message_text(client)

    def test_business_with_no_services(self, client):
        signup(client)
        wa_text(client, "book adjoa-s-beauty-bar")
        assert "hasn't listed any services" in last_message_text(client)

    def test_full_booking_in_whatsapp(self, client):
        setup_business(client)

        # "book <slug>" → service list
        wa_text(client, "book adjoa-s-beauty-bar")
        last = outbox(client)[-1]["body"]
        assert last["type"] == "interactive"
        rows = last["interactive"]["action"]["sections"][0]["rows"]
        assert rows[0]["id"] == "svc:1"
        assert "Knotless braids" in rows[0]["title"]

        # pick service → day list
        wa_reply(client, "svc:1")
        last = outbox(client)[-1]["body"]
        day_rows = last["interactive"]["action"]["sections"][0]["rows"]
        assert day_rows[0]["id"].startswith("date:")

        # pick a day → time list
        target_date = day_rows[1]["id"]  # not today, so full availability
        wa_reply(client, target_date)
        last = outbox(client)[-1]["body"]
        time_rows = last["interactive"]["action"]["sections"][0]["rows"]
        assert {"time:09:00", "time:09:30"} <= {r["id"] for r in time_rows}

        # pick a time → asked for name
        wa_reply(client, "time:10:00")
        assert "name" in last_message_text(client).lower()

        # send name → booked (demo payments) + business notified
        wa_text(client, "Ama Serwaa")
        confirmation = last_message_text(client)
        assert "Booked!" in confirmation
        assert "GHS 50" in confirmation  # deposit

        biz_msgs = outbox(client, to="233241234567")  # business phone, wa-normalized
        assert any("New booking" in json.dumps(m["body"]) for m in biz_msgs)

        # the booking is real: web slot availability reflects it
        date_str = target_date.removeprefix("date:")
        slots = client.get(
            f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={date_str}"
        ).json()["slots"]
        assert "10:00" not in slots

    def test_taken_slot_reoffers_times(self, client):
        setup_business(client)
        wa_text(client, "book adjoa-s-beauty-bar")
        wa_reply(client, "svc:1")
        day_id = outbox(client)[-1]["body"]["interactive"]["action"]["sections"][0]["rows"][1]["id"]
        wa_reply(client, day_id)
        wa_reply(client, "time:10:00")
        wa_text(client, "Ama")  # booked 10:00

        # Second customer tries the same slot.
        other = "233501112222"
        wa_text(client, "book adjoa-s-beauty-bar", sender=other)
        wa_reply(client, "svc:1", sender=other)
        wa_reply(client, day_id, sender=other)
        wa_reply(client, "time:10:00", sender=other)
        # They get a "just taken" notice followed by a fresh time list without 10:00.
        msgs = [json.dumps(m["body"]) for m in outbox(client, to=other)]
        assert any("just taken" in m for m in msgs)
        fresh_times = outbox(client, to=other)[-1]["body"]["interactive"]["action"]["sections"][0]["rows"]
        assert "time:10:00" not in {r["id"] for r in fresh_times}

    def test_conversation_restart_mid_flow(self, client):
        setup_business(client)
        wa_text(client, "book adjoa-s-beauty-bar")
        wa_reply(client, "svc:1")
        # Customer starts over instead of picking a date.
        wa_text(client, "book adjoa-s-beauty-bar")
        last = outbox(client)[-1]["body"]
        assert last["interactive"]["action"]["sections"][0]["rows"][0]["id"] == "svc:1"

    def test_stale_reply_id_greets(self, client):
        setup_business(client)
        # No conversation in progress: a service reply id goes to the greeting.
        wa_reply(client, "svc:1")
        assert "Welcome to Bookam" in last_message_text(client)


class TestBotCancellation:
    def _book_via_bot(self, client) -> str:
        wa_text(client, "book adjoa-s-beauty-bar")
        wa_reply(client, "svc:1")
        day_id = outbox(client)[-1]["body"]["interactive"]["action"]["sections"][0]["rows"][1]["id"]
        wa_reply(client, day_id)
        wa_reply(client, "time:10:00")
        wa_text(client, "Ama Serwaa")
        return day_id.removeprefix("date:")

    def test_cancel_flow(self, client):
        setup_business(client)
        date_str = self._book_via_bot(client)

        wa_text(client, "cancel")
        last = outbox(client)[-1]["body"]
        rows = last["interactive"]["action"]["sections"][0]["rows"]
        assert rows[0]["id"].startswith("cxl:")

        wa_reply(client, rows[0]["id"])
        assert "cancelled" in last_message_text(client)

        # Slot is free again and the business was told.
        slots = client.get(
            f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={date_str}"
        ).json()["slots"]
        assert "10:00" in slots
        biz_msgs = outbox(client, to="233241234567")
        assert any("Cancelled" in json.dumps(m["body"]) for m in biz_msgs)

    def test_cancel_with_no_bookings(self, client):
        setup_business(client)
        wa_text(client, "cancel")
        assert "no upcoming bookings" in last_message_text(client)

    def test_cancel_twice(self, client):
        setup_business(client)
        self._book_via_bot(client)
        wa_text(client, "cancel")
        ref_id = outbox(client)[-1]["body"]["interactive"]["action"]["sections"][0]["rows"][0]["id"]
        wa_reply(client, ref_id)
        wa_reply(client, ref_id)
        assert "couldn't find that booking" in last_message_text(client)


class TestWebhookPlumbing:
    def test_meta_verification_handshake(self, client):
        resp = client.get(
            "/wa/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "bookam-verify",
                "hub.challenge": "12345",
            },
        )
        assert resp.status_code == 200
        assert resp.text == "12345"

    def test_meta_verification_wrong_token(self, client):
        resp = client.get(
            "/wa/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x"},
        )
        assert resp.status_code == 403

    def test_bad_json_rejected(self, client):
        resp = client.post("/wa/webhook", content=b"not json")
        assert resp.status_code == 400

    def test_signature_enforced_when_secret_set(self, settings):
        import hashlib
        import hmac

        from app.main import create_app

        settings.wa_app_secret = "app-secret"
        app = create_app(settings)
        with TestClient(app) as client:
            body = json.dumps({"entry": []}).encode()
            assert client.post("/wa/webhook", content=body).status_code == 403

            sig = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
            resp = client.post(
                "/wa/webhook", content=body, headers={"X-Hub-Signature-256": f"sha256={sig}"}
            )
            assert resp.status_code == 200

    def test_non_message_events_ignored(self, client):
        resp = client.post(
            "/wa/webhook",
            json={"entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}]},
        )
        assert resp.status_code == 200
        assert outbox(client) == []

    def test_unsupported_message_type_greets(self, client):
        resp = client.post(
            "/wa/webhook",
            json={
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "messages": [
                                        {"from": CUSTOMER, "type": "image", "image": {}}
                                    ]
                                }
                            }
                        ]
                    }
                ]
            },
        )
        assert resp.status_code == 200
        assert "Welcome to Bookam" in last_message_text(client)


class TestFindCommand:
    def test_find_by_area_lists_businesses(self, client):
        setup_business(client)
        wa_text(client, "find osu")
        last = outbox(client)[-1]["body"]
        rows = last["interactive"]["action"]["sections"][0]["rows"]
        assert rows[0]["id"] == "book adjoa-s-beauty-bar"
        assert "Adjoa" in rows[0]["title"]

    def test_find_by_service_name(self, client):
        setup_business(client)
        wa_text(client, "find braids")
        last = outbox(client)[-1]["body"]
        assert "adjoa" in json.dumps(last)

    def test_find_result_tap_starts_booking(self, client):
        setup_business(client)
        wa_text(client, "find osu")
        row_id = outbox(client)[-1]["body"]["interactive"]["action"]["sections"][0]["rows"][0]["id"]
        wa_reply(client, row_id)
        last = outbox(client)[-1]["body"]
        assert last["interactive"]["action"]["sections"][0]["rows"][0]["id"] == "svc:1"

    def test_find_no_results(self, client):
        setup_business(client)
        wa_text(client, "find timbuktu")
        assert "Nothing found" in last_message_text(client)

    def test_bare_find_gives_hint(self, client):
        wa_text(client, "find")
        assert "What are you looking for" in last_message_text(client)

    def test_find_excludes_serviceless_businesses(self, client):
        signup(client)  # no services yet
        wa_text(client, "find osu")
        assert "Nothing found" in last_message_text(client)


class TestRichMessageTypes:
    def test_location_pin_accepted_as_home_visit_address(self, client):
        signup(client)
        client.post(
            "/dashboard/services",
            data={
                "name": "Bridal makeup", "duration_min": "90", "price_ghs": "400",
                "deposit_ghs": "100", "venue": "customer", "travel_fee_ghs": "30",
            },
            follow_redirects=False,
        )
        open_all_days(client)
        wa_text(client, "book adjoa-s-beauty-bar")
        wa_reply(client, "svc:1")  # home-only service → asked for address
        assert "Where should they come" in last_message_text(client)

        # Customer shares a location pin instead of typing.
        client.post(
            "/wa/webhook",
            json={"entry": [{"changes": [{"value": {"messages": [{
                "from": CUSTOMER,
                "type": "location",
                "location": {"latitude": 5.6037, "longitude": -0.187, "name": "A&C Mall",
                             "address": "East Legon, Accra"},
            }]}}]}]},
        )
        # Bot moved on to day selection; the pin became the address.
        last = outbox(client)[-1]["body"]
        assert last["interactive"]["action"]["sections"][0]["rows"][0]["id"].startswith("date:")
        day_id = last["interactive"]["action"]["sections"][0]["rows"][1]["id"]
        wa_reply(client, day_id)
        wa_reply(client, "time:10:00")
        wa_text(client, "Ama Serwaa")
        conn = sqlite3.connect(client.app.state.settings.db_path)
        conn.row_factory = sqlite3.Row
        booking = conn.execute("SELECT * FROM bookings").fetchone()
        conn.close()
        assert "A&C Mall" in booking["customer_address"]
        assert "maps.google.com/?q=5.6037,-0.187" in booking["customer_address"]

    def test_voice_note_gets_polite_fallback(self, client):
        setup_business(client)
        client.post(
            "/wa/webhook",
            json={"entry": [{"changes": [{"value": {"messages": [{
                "from": CUSTOMER, "type": "audio", "audio": {"id": "media123"},
            }]}}]}]},
        )
        assert "can't listen to voice notes" in last_message_text(client)

    def test_voice_note_mid_flow_keeps_conversation(self, client):
        setup_business(client)
        wa_text(client, "book adjoa-s-beauty-bar")
        client.post(
            "/wa/webhook",
            json={"entry": [{"changes": [{"value": {"messages": [{
                "from": CUSTOMER, "type": "audio", "audio": {"id": "media123"},
            }]}}]}]},
        )
        assert "can't listen to voice notes" in last_message_text(client)
        # The conversation survives: picking a service still works.
        wa_reply(client, "svc:1")
        last = outbox(client)[-1]["body"]
        assert last["interactive"]["action"]["sections"][0]["rows"][0]["id"].startswith("date:")


class TestConversationExpiry:
    def test_stale_conversation_resets(self, client):
        setup_business(client)
        wa_text(client, "book adjoa-s-beauty-bar")
        # Age the conversation past the TTL.
        conn = sqlite3.connect(client.app.state.settings.db_path)
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        conn.execute("UPDATE conversations SET updated_at = ?", (old,))
        conn.commit()
        conn.close()
        wa_reply(client, "svc:1")
        assert "Welcome to Bookam" in last_message_text(client)
