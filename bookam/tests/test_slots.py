from __future__ import annotations

from datetime import datetime, timezone

from app.slots import add_minutes, compute_slots, slots_for_date


class TestComputeSlots:
    def test_empty_day_full_availability(self):
        slots = compute_slots("09:00", "11:00", 60, [])
        assert slots == ["09:00", "09:30", "10:00"]

    def test_service_must_fit_before_close(self):
        slots = compute_slots("09:00", "10:00", 60, [])
        assert slots == ["09:00"]

    def test_busy_interval_blocks_overlaps(self):
        slots = compute_slots("09:00", "12:00", 60, [("10:00", "11:00")])
        assert "09:30" not in slots  # would run into the 10:00 booking
        assert "10:00" not in slots
        assert "10:30" not in slots  # starts inside the booking
        assert "09:00" in slots
        assert "11:00" in slots

    def test_adjacent_bookings_allowed(self):
        slots = compute_slots("09:00", "12:00", 60, [("09:00", "10:00")])
        assert "10:00" in slots

    def test_not_before_floor(self):
        slots = compute_slots("09:00", "12:00", 30, [], not_before="10:15")
        assert slots[0] == "10:30"

    def test_no_slots_when_duration_exceeds_day(self):
        assert compute_slots("09:00", "10:00", 120, []) == []

    def test_add_minutes(self):
        assert add_minutes("09:30", 45) == "10:15"
        assert add_minutes("23:30", 30) == "24:00"


class TestSlotsForDate:
    def test_past_date_empty(self):
        now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
        assert slots_for_date("09:00", "17:00", 60, [], "2026-08-10", now) == []

    def test_future_date_full_day(self):
        now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
        slots = slots_for_date("09:00", "11:00", 60, [], "2026-08-12", now)
        assert slots == ["09:00", "09:30", "10:00"]

    def test_today_excludes_past_and_notice_period(self):
        now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
        slots = slots_for_date("09:00", "17:00", 60, [], "2026-08-11", now)
        assert slots[0] == "10:30"  # 30 min notice after 10:00

    def test_today_near_midnight_empty(self):
        now = datetime(2026, 8, 11, 23, 45, tzinfo=timezone.utc)
        assert slots_for_date("09:00", "23:59", 30, [], "2026-08-11", now) == []
