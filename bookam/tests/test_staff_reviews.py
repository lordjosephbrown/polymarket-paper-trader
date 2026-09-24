"""Multi-staff capacity, specific-person bookings, and ratings/reviews."""
from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from conftest import add_service, open_all_days, signup, tomorrow

CUSTOMER = "233209876543"


def add_staff(client: TestClient, name: str, location_id: int = 1):
    resp = client.post(
        "/dashboard/staff",
        data={"name": name, "location_id": str(location_id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def book(client: TestClient, time: str = "09:00", phone: str = "0209876543", staff_id: int = 0):
    return client.post(
        "/b/adjoa-s-beauty-bar/book",
        data={
            "service_id": "1",
            "date": tomorrow(),
            "start_time": time,
            "customer_name": "Ama",
            "customer_phone": phone,
            "staff_id": str(staff_id),
        },
        follow_redirects=False,
    )


def confirm(client: TestClient, resp) -> str:
    callback = resp.headers["location"]
    client.get(callback)
    return callback.split("reference=")[1]


def db_rows(client: TestClient, sql: str, *params):
    conn = sqlite3.connect(client.app.state.settings.db_path)
    conn.row_factory = sqlite3.Row
    out = conn.execute(sql, params).fetchall()
    conn.close()
    return out


class TestStaffCapacity:
    def test_solo_mode_capacity_one(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        confirm(client, book(client))
        assert book(client, phone="0501112222").status_code == 409  # same slot taken

    def test_two_staff_two_concurrent_bookings(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        add_staff(client, "Efua")
        add_staff(client, "Akos")

        r1 = book(client)
        confirm(client, r1)
        r2 = book(client, phone="0501112222")
        assert r2.status_code == 303  # second booking same time succeeds
        confirm(client, r2)
        r3 = book(client, phone="0261113333")
        assert r3.status_code == 409  # both staff busy now

        # Auto-assignment gave each booking a different person.
        staff_ids = [r["staff_id"] for r in db_rows(client, "SELECT staff_id FROM bookings WHERE status='confirmed'")]
        assert sorted(staff_ids) == [1, 2]

    def test_specific_staff_request(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        add_staff(client, "Efua")
        add_staff(client, "Akos")

        confirm(client, book(client, staff_id=1))  # Efua at 09:00
        # Efua is busy at 09:00 for her specific calendar…
        slots = client.get(
            f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={tomorrow()}&staff_id=1"
        ).json()["slots"]
        assert "09:00" not in slots
        # …but Akos is free.
        slots = client.get(
            f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={tomorrow()}&staff_id=2"
        ).json()["slots"]
        assert "09:00" in slots
        # Booking Efua again at 09:00 conflicts; Akos works.
        assert book(client, phone="0501112222", staff_id=1).status_code == 409
        assert book(client, phone="0501112222", staff_id=2).status_code == 303

    def test_unknown_staff_404(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        assert book(client, staff_id=99).status_code == 404

    def test_remove_staff_reduces_capacity(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        add_staff(client, "Efua")
        add_staff(client, "Akos")
        client.post("/dashboard/staff/2/delete", follow_redirects=False)
        confirm(client, book(client))
        assert book(client, phone="0501112222").status_code == 409  # back to capacity 1

    def test_bot_asks_for_person_with_two_staff(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        add_staff(client, "Efua")
        add_staff(client, "Akos")

        def wa(text=None, reply=None):
            if text is not None:
                msg = {"from": CUSTOMER, "type": "text", "text": {"body": text}}
            else:
                msg = {"from": CUSTOMER, "type": "interactive",
                       "interactive": {"list_reply": {"id": reply, "title": "x"}}}
            client.post("/wa/webhook", json={"entry": [{"changes": [{"value": {"messages": [msg]}}]}]})

        def last():
            row = db_rows(client, "SELECT body FROM wa_outbox WHERE to_phone = ? ORDER BY id DESC LIMIT 1", CUSTOMER)[0]
            return json.loads(row["body"])

        wa(text="book adjoa-s-beauty-bar")
        wa(reply="svc:1")
        rows = last()["interactive"]["action"]["sections"][0]["rows"]
        assert rows[0]["id"] == "stf:0"
        assert {"stf:1", "stf:2"} <= {r["id"] for r in rows}

        wa(reply="stf:1")  # Efua
        day_id = last()["interactive"]["action"]["sections"][0]["rows"][1]["id"]
        wa(reply=day_id)
        wa(reply="time:10:00")
        wa(text="Ama Serwaa")
        booking = db_rows(client, "SELECT * FROM bookings")[0]
        assert booking["staff_id"] == 1
        assert booking["status"] == "confirmed"


class TestReviews:
    def _completed_booking(self, client) -> str:
        confirm(client, book(client))
        client.post("/dashboard/bookings/1/status", data={"status": "completed"}, follow_redirects=False)
        return db_rows(client, "SELECT payment_ref FROM bookings")[0]["payment_ref"]

    def test_completion_message_asks_for_rating(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        self._completed_booking(client)
        texts = [r["body"] for r in db_rows(client, "SELECT body FROM wa_outbox WHERE to_phone = ?", CUSTOMER)]
        assert any("rate" in t for t in texts)

    def test_rate_via_whatsapp(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        self._completed_booking(client)
        client.post("/wa/webhook", json={"entry": [{"changes": [{"value": {"messages": [
            {"from": CUSTOMER, "type": "text", "text": {"body": "rate 5 great braids"}}
        ]}}]}]})
        reviews = db_rows(client, "SELECT * FROM reviews")
        assert len(reviews) == 1
        assert reviews[0]["rating"] == 5
        assert reviews[0]["comment"] == "great braids"
        # Rating shows on the public page and directory.
        assert "⭐ 5.0" in client.get("/b/adjoa-s-beauty-bar").text
        assert "⭐ 5.0" in client.get("/discover").text

    def test_rate_without_completed_booking(self, client):
        signup(client)
        client.post("/wa/webhook", json={"entry": [{"changes": [{"value": {"messages": [
            {"from": CUSTOMER, "type": "text", "text": {"body": "rate 4"}}
        ]}}]}]})
        assert db_rows(client, "SELECT * FROM reviews") == []
        texts = [r["body"] for r in db_rows(client, "SELECT body FROM wa_outbox WHERE to_phone = ?", CUSTOMER)]
        assert any("couldn't find a completed booking" in t for t in texts)

    def test_rate_via_web_receipt(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        ref = self._completed_booking(client)
        resp = client.get(f"/booking/{ref}")
        assert "How was it?" in resp.text
        resp = client.post(
            f"/booking/{ref}/review", data={"rating": "4", "comment": "solid"}, follow_redirects=True
        )
        assert "You rated this" in resp.text
        assert db_rows(client, "SELECT rating FROM reviews")[0]["rating"] == 4

    def test_no_double_rating(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        ref = self._completed_booking(client)
        client.post(f"/booking/{ref}/review", data={"rating": "4"})
        client.post(f"/booking/{ref}/review", data={"rating": "1"})
        reviews = db_rows(client, "SELECT * FROM reviews")
        assert len(reviews) == 1 and reviews[0]["rating"] == 4

    def test_cannot_rate_unfinished_booking(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        ref = confirm(client, book(client))
        assert client.post(f"/booking/{ref}/review", data={"rating": "5"}).status_code == 404

    def test_invalid_rating_rejected(self, client):
        signup(client)
        add_service(client)
        open_all_days(client)
        ref = self._completed_booking(client)
        assert client.post(f"/booking/{ref}/review", data={"rating": "9"}).status_code == 400
