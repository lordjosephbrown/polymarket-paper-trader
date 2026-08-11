"""Service-delivery tracking, business-initiated changes, and broadcast updates."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import db as dbmod
from conftest import add_service, open_all_days, signup, tomorrow

CUSTOMER_WA = "233209876543"


def outbox_texts(client: TestClient, to: str) -> list[str]:
    conn = sqlite3.connect(client.app.state.settings.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT body FROM wa_outbox WHERE to_phone = ? ORDER BY id", (to,)
    ).fetchall()
    conn.close()
    return [r["body"] for r in rows]


def insert_booking(
    client: TestClient, *, status: str = "confirmed", venue: str = "business", today: bool = True
) -> str:
    conn = sqlite3.connect(client.app.state.settings.db_path)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d") if today else tomorrow()
    ref = f"ref_{status}_{venue}_{date_str}"
    conn.execute(
        "INSERT INTO bookings (business_id, location_id, service_id, venue, customer_address,"
        " customer_name, customer_phone, date, start_time, end_time, status, deposit_ghs,"
        " payment_ref, created_at)"
        " VALUES (1, 1, 1, ?, ?, 'Ama Serwaa', '0209876543', ?, '10:00', '11:00', ?, 50, ?, ?)",
        (venue, "East Legon" if venue == "customer" else "", date_str, status, ref, dbmod.now_iso()),
    )
    conn.commit()
    conn.close()
    return ref


def wa_text(client: TestClient, body: str, sender: str = CUSTOMER_WA):
    return client.post(
        "/wa/webhook",
        json={
            "entry": [
                {"changes": [{"value": {"messages": [{"from": sender, "type": "text", "text": {"body": body}}]}}]}
            ]
        },
    )


class TestDeliveryTracking:
    def test_home_visit_pipeline_notifies_customer_each_step(self, client):
        signup(client)
        add_service(client)
        insert_booking(client, venue="customer")

        resp = client.get("/dashboard")
        assert "On my way" in resp.text

        client.post("/dashboard/bookings/1/status", data={"status": "on_the_way"}, follow_redirects=False)
        client.post("/dashboard/bookings/1/status", data={"status": "in_progress"}, follow_redirects=False)
        client.post("/dashboard/bookings/1/status", data={"status": "completed"}, follow_redirects=False)

        texts = outbox_texts(client, CUSTOMER_WA)
        assert any("on the way" in t for t in texts)
        assert any("has started" in t for t in texts)
        assert any("All done" in t for t in texts)

    def test_shop_visit_hides_on_my_way_button(self, client):
        signup(client)
        add_service(client)
        insert_booking(client, venue="business")
        assert "On my way" not in client.get("/dashboard").text

    def test_illegal_transitions_ignored(self, client):
        signup(client)
        add_service(client)
        insert_booking(client)
        # completed is terminal.
        client.post("/dashboard/bookings/1/status", data={"status": "completed"}, follow_redirects=False)
        client.post("/dashboard/bookings/1/status", data={"status": "no_show"}, follow_redirects=False)
        conn = sqlite3.connect(client.app.state.settings.db_path)
        status = conn.execute("SELECT status FROM bookings WHERE id = 1").fetchone()[0]
        conn.close()
        assert status == "completed"

    def test_active_statuses_still_block_slot(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        insert_booking(client, status="in_progress", today=False)
        slots = client.get(
            f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={tomorrow()}"
        ).json()["slots"]
        assert "10:00" not in slots

    def test_receipt_page_shows_tracker(self, client):
        signup(client)
        add_service(client)
        ref = insert_booking(client, status="on_the_way", venue="customer")
        resp = client.get(f"/booking/{ref}")
        assert "On the way" in resp.text
        assert 'http-equiv="refresh"' in resp.text  # live statuses auto-refresh

    def test_bot_status_command(self, client):
        signup(client)
        add_service(client)
        insert_booking(client, status="on_the_way", venue="customer", today=False)
        wa_text(client, "status")
        texts = outbox_texts(client, CUSTOMER_WA)
        assert any("On the way to you" in t for t in texts)

    def test_bot_status_command_empty(self, client):
        signup(client)
        wa_text(client, "status")
        assert any("no upcoming bookings" in t for t in outbox_texts(client, CUSTOMER_WA))


class TestBusinessChanges:
    def test_reschedule_notifies_and_moves_slot(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        insert_booking(client, today=False)  # tomorrow 10:00

        resp = client.post(
            "/dashboard/bookings/1/reschedule",
            data={"date": tomorrow(), "start_time": "14:00"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        texts = outbox_texts(client, CUSTOMER_WA)
        assert any("Schedule change" in t and "14:00" in t for t in texts)

        slots = client.get(
            f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={tomorrow()}"
        ).json()["slots"]
        assert "10:00" in slots  # old slot freed
        assert "14:00" not in slots  # new slot taken

    def test_reschedule_to_taken_slot_rejected(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        insert_booking(client, today=False)
        # Same slot it already has → allowed (own slot excluded); a conflicting
        # second booking's slot → 409.
        conn = sqlite3.connect(client.app.state.settings.db_path)
        conn.execute(
            "INSERT INTO bookings (business_id, location_id, service_id, customer_name,"
            " customer_phone, date, start_time, end_time, status, deposit_ghs, payment_ref, created_at)"
            " VALUES (1, 1, 1, 'Kofi', '0501112222', ?, '12:00', '13:00', 'confirmed', 50, 'ref2', ?)",
            (tomorrow(), dbmod.now_iso()),
        )
        conn.commit()
        conn.close()
        resp = client.post(
            "/dashboard/bookings/1/reschedule",
            data={"date": tomorrow(), "start_time": "12:00"},
        )
        assert resp.status_code == 409
        resp = client.post(
            "/dashboard/bookings/1/reschedule",
            data={"date": tomorrow(), "start_time": "10:00"},
            follow_redirects=False,
        )
        assert resp.status_code == 303  # keeping its own slot is fine

    def test_business_cancel_notifies_customer_with_refund_note(self, client):
        signup(client)
        add_service(client)
        insert_booking(client, today=False)
        client.post("/dashboard/bookings/1/status", data={"status": "cancelled"}, follow_redirects=False)
        texts = outbox_texts(client, CUSTOMER_WA)
        assert any("had to cancel" in t and "refund" in t for t in texts)


class TestBroadcasts:
    def test_broadcast_reaches_all_customers_once(self, client):
        signup(client)
        add_service(client)
        insert_booking(client)
        conn = sqlite3.connect(client.app.state.settings.db_path)
        conn.execute(
            "INSERT INTO bookings (business_id, location_id, service_id, customer_name,"
            " customer_phone, date, start_time, end_time, status, deposit_ghs, payment_ref, created_at)"
            " VALUES (1, 1, 1, 'Kofi', '0501112222', '2026-01-05', '12:00', '13:00', 'completed', 50, 'r2', ?)",
            (dbmod.now_iso(),),
        )
        conn.commit()
        conn.close()

        resp = client.post(
            "/dashboard/updates",
            data={"message": "New service alert! We now do pedicures — GH₵80."},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert any("pedicures" in t for t in outbox_texts(client, CUSTOMER_WA))
        assert any("pedicures" in t for t in outbox_texts(client, "233501112222"))
        assert "pedicures" in client.get("/dashboard/updates").text  # history shown

    def test_broadcast_rate_limited_daily(self, client):
        signup(client)
        add_service(client)
        insert_booking(client)
        client.post("/dashboard/updates", data={"message": "First"}, follow_redirects=False)
        resp = client.post("/dashboard/updates", data={"message": "Second"})
        assert resp.status_code == 429
        assert "one update per day" in resp.text

    def test_broadcast_validation(self, client):
        signup(client)
        assert client.post("/dashboard/updates", data={"message": "  "}).status_code == 400
        assert client.post("/dashboard/updates", data={"message": "x" * 501}).status_code == 400
