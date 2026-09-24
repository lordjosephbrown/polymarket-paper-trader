"""Multi-location businesses, home-service bookings, and the discovery directory."""
from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from conftest import add_service, open_all_days, signup, tomorrow

CUSTOMER = "233209876543"


def add_branch(client: TestClient, name: str = "East Legon branch", area: str = "East Legon, Accra"):
    resp = client.post(
        "/dashboard/locations", data={"name": name, "area": area}, follow_redirects=False
    )
    assert resp.status_code == 303


def add_home_service(client: TestClient, venue: str = "both", travel_fee: float = 30):
    resp = client.post(
        "/dashboard/services",
        data={
            "name": "Bridal makeup",
            "duration_min": "90",
            "price_ghs": "400",
            "deposit_ghs": "100",
            "venue": venue,
            "travel_fee_ghs": str(travel_fee),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303


class TestLocations:
    def test_signup_creates_default_location(self, client):
        signup(client)
        resp = client.get("/dashboard/locations")
        assert "Main location" in resp.text
        assert "Osu, Accra" in resp.text

    def test_add_branch_and_per_branch_hours(self, client):
        signup(client)
        add_branch(client)
        resp = client.get("/dashboard/locations")
        assert "East Legon branch" in resp.text
        # Hours page shows branch tabs; branch 2 has its own hours.
        resp = client.get("/dashboard/hours?location_id=2")
        assert resp.status_code == 200
        assert "East Legon branch" in resp.text

    def test_cannot_close_last_location(self, client):
        signup(client)
        client.post("/dashboard/locations/1/delete", follow_redirects=False)
        assert "Main location" in client.get("/dashboard/locations").text

    def test_close_branch(self, client):
        signup(client)
        add_branch(client)
        client.post("/dashboard/locations/2/delete", follow_redirects=False)
        assert "East Legon branch" not in client.get("/dashboard/locations").text

    def test_branches_have_independent_calendars(self, client):
        signup(client)
        add_service(client)
        add_branch(client)
        open_all_days(client)  # sets hours for location 1 (default)
        # Give branch 2 the same hours.
        data = {"location_id": "2"}
        for day in range(7):
            data[f"enabled_{day}"] = "on"
            data[f"open_{day}"] = "09:00"
            data[f"close_{day}"] = "17:00"
        client.post("/dashboard/hours", data=data, follow_redirects=False)

        # Book 09:00 at branch 1.
        resp = client.post(
            "/b/adjoa-s-beauty-bar/book",
            data={
                "service_id": "1",
                "location_id": "1",
                "date": tomorrow(),
                "start_time": "09:00",
                "customer_name": "Ama",
                "customer_phone": "0209876543",
            },
            follow_redirects=False,
        )
        client.get(resp.headers["location"])

        # Branch 1 lost the slot; branch 2 still has it.
        b1 = client.get(
            f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={tomorrow()}&location_id=1"
        ).json()["slots"]
        b2 = client.get(
            f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={tomorrow()}&location_id=2"
        ).json()["slots"]
        assert "09:00" not in b1
        assert "09:00" in b2

    def test_unknown_location_404(self, client):
        signup(client)
        add_service(client)
        resp = client.get(
            f"/b/adjoa-s-beauty-bar/slots?service_id=1&date={tomorrow()}&location_id=99"
        )
        assert resp.status_code == 404


class TestHomeService:
    def test_home_booking_requires_address(self, client):
        signup(client)
        add_home_service(client)
        open_all_days(client)
        resp = client.post(
            "/b/adjoa-s-beauty-bar/book",
            data={
                "service_id": "1",
                "date": tomorrow(),
                "start_time": "09:00",
                "customer_name": "Ama",
                "customer_phone": "0209876543",
                "venue": "customer",
                "customer_address": "",
            },
        )
        assert resp.status_code == 400

    def test_home_booking_adds_travel_fee_to_deposit(self, client):
        signup(client)
        add_home_service(client)  # deposit 100, travel 30
        open_all_days(client)
        resp = client.post(
            "/b/adjoa-s-beauty-bar/book",
            data={
                "service_id": "1",
                "date": tomorrow(),
                "start_time": "09:00",
                "customer_name": "Ama",
                "customer_phone": "0209876543",
                "venue": "customer",
                "customer_address": "East Legon, near A&C Mall",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        resp = client.get(resp.headers["location"], follow_redirects=True)
        assert "GH₵130" in resp.text  # 100 deposit + 30 travel
        assert "GH₵300" in resp.text  # balance: 400 + 30 - 130
        assert "East Legon, near A&amp;C Mall" in resp.text

        # Business notification includes the address.
        conn = sqlite3.connect(client.app.state.settings.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT body FROM wa_outbox WHERE to_phone = '233241234567'"
        ).fetchall()
        conn.close()
        assert any("Home visit" in r["body"] and "East Legon" in r["body"] for r in rows)

    def test_shop_visit_has_no_travel_fee(self, client):
        signup(client)
        add_home_service(client)  # venue=both
        open_all_days(client)
        resp = client.post(
            "/b/adjoa-s-beauty-bar/book",
            data={
                "service_id": "1",
                "date": tomorrow(),
                "start_time": "11:00",
                "customer_name": "Ama",
                "customer_phone": "0209876543",
                "venue": "business",
            },
            follow_redirects=False,
        )
        resp = client.get(resp.headers["location"], follow_redirects=True)
        assert "GH₵100" in resp.text  # deposit only

    def test_venue_not_offered_rejected(self, client):
        signup(client)
        add_service(client)  # default venue=business
        open_all_days(client)
        resp = client.post(
            "/b/adjoa-s-beauty-bar/book",
            data={
                "service_id": "1",
                "date": tomorrow(),
                "start_time": "09:00",
                "customer_name": "Ama",
                "customer_phone": "0209876543",
                "venue": "customer",
                "customer_address": "Somewhere",
            },
        )
        assert resp.status_code == 400

    def test_shop_only_service_rejects_home_and_garbage_venue(self, client):
        signup(client)
        add_home_service(client, venue="customer")
        open_all_days(client)
        base = {
            "service_id": "1",
            "date": tomorrow(),
            "start_time": "09:00",
            "customer_name": "Ama",
            "customer_phone": "0209876543",
        }
        assert (
            client.post("/b/adjoa-s-beauty-bar/book", data={**base, "venue": "business"}).status_code
            == 400
        )
        assert (
            client.post("/b/adjoa-s-beauty-bar/book", data={**base, "venue": "weird"}).status_code
            == 400
        )


class TestBotWithLocationsAndHomeService:
    def _wa(self, client, text=None, reply=None, sender=CUSTOMER):
        if text is not None:
            msg = {"from": sender, "type": "text", "text": {"body": text}}
        else:
            kind = "button_reply" if reply.startswith("ven:") else "list_reply"
            msg = {
                "from": sender,
                "type": "interactive",
                "interactive": {kind: {"id": reply, "title": "x"}},
            }
        resp = client.post(
            "/wa/webhook",
            json={"entry": [{"changes": [{"value": {"messages": [msg]}}]}]},
        )
        assert resp.status_code == 200

    def _outbox_last(self, client, to=CUSTOMER):
        conn = sqlite3.connect(client.app.state.settings.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT body FROM wa_outbox WHERE to_phone = ? ORDER BY id DESC LIMIT 1", (to,)
        ).fetchone()
        conn.close()
        return json.loads(row["body"])

    def test_bot_asks_branch_then_venue_then_address(self, client):
        signup(client)
        add_home_service(client)  # venue=both, travel 30
        add_branch(client)
        open_all_days(client)

        self._wa(client, text="book adjoa-s-beauty-bar")
        self._wa(client, reply="svc:1")
        # Branch question
        last = self._outbox_last(client)
        rows = last["interactive"]["action"]["sections"][0]["rows"]
        assert rows[0]["id"] == "loc:1" and rows[1]["id"] == "loc:2"

        self._wa(client, reply="loc:1")
        # Venue buttons with travel fee note
        last = self._outbox_last(client)
        assert last["interactive"]["type"] == "button"
        assert "GHS 30" in last["interactive"]["body"]["text"]

        self._wa(client, reply="ven:customer")
        assert "Where should they come" in json.dumps(self._outbox_last(client))

        self._wa(client, text="East Legon, near A&C Mall")
        # Day list next
        last = self._outbox_last(client)
        assert last["interactive"]["action"]["sections"][0]["rows"][0]["id"].startswith("date:")

        day_id = last["interactive"]["action"]["sections"][0]["rows"][1]["id"]
        self._wa(client, reply=day_id)
        self._wa(client, reply="time:10:00")
        self._wa(client, text="Ama Serwaa")

        confirmation = json.dumps(self._outbox_last(client, to="233241234567"))
        assert "Home visit" in confirmation and "East Legon" in confirmation
        # Deposit charged includes travel fee.
        conn = sqlite3.connect(client.app.state.settings.db_path)
        conn.row_factory = sqlite3.Row
        booking = conn.execute("SELECT * FROM bookings").fetchone()
        conn.close()
        assert booking["deposit_ghs"] == 130
        assert booking["travel_fee_ghs"] == 30
        assert booking["venue"] == "customer"
        assert booking["location_id"] == 1

    def test_bot_shop_only_service_skips_venue_question(self, client):
        signup(client)
        add_service(client)  # venue=business
        open_all_days(client)
        self._wa(client, text="book adjoa-s-beauty-bar")
        self._wa(client, reply="svc:1")
        # Straight to days — single branch, shop-only service.
        last = self._outbox_last(client)
        assert last["interactive"]["action"]["sections"][0]["rows"][0]["id"].startswith("date:")


class TestDiscover:
    def test_directory_lists_and_searches(self, client):
        signup(client)
        add_service(client)
        signup(client, name="Kofi Cuts", phone="0551234567")
        # Kofi has no services yet → hidden from the directory.
        resp = client.get("/discover")
        assert "Adjoa" in resp.text
        assert "Kofi Cuts" not in resp.text

        resp = client.get("/discover", params={"q": "Osu"})
        assert "Adjoa" in resp.text
        resp = client.get("/discover", params={"q": "zzz-nothing"})
        assert "No businesses found" in resp.text
