"""Tests for thetadesk.models: parsing, normalization, serialization, errors."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from thetadesk.models import (
    CALL,
    PUT,
    DeskError,
    InsufficientBuyingPowerError,
    InvalidChainError,
    NotInitializedError,
    OptionNotFoundError,
    OptionQuote,
    Position,
    PositionClosedError,
    PositionNotFoundError,
    load_chain_file,
    normalize_right,
    parse_chain,
    parse_chains,
    parse_date,
    to_jsonable,
    today_utc,
    utc_now,
)


class TestErrors:
    def test_codes_and_messages(self):
        assert DeskError("x").code == "DESK_ERROR"
        assert NotInitializedError().code == "NOT_INITIALIZED"
        e = InsufficientBuyingPowerError(1500.0, 1000.0)
        assert e.code == "INSUFFICIENT_BUYING_POWER"
        assert "1,500.00" in e.message and e.required == 1500.0 and e.available == 1000.0
        assert PositionNotFoundError(7).position_id == 7
        c = PositionClosedError(3, "closed")
        assert c.status == "closed" and "not open" in c.message
        o = OptionNotFoundError("XYZ", "2026-10-16", 95.0, "put")
        assert o.code == "OPTION_NOT_FOUND" and "XYZ 2026-10-16 95 put" in o.message
        assert InvalidChainError("bad").code == "INVALID_CHAIN"


class TestHelpers:
    def test_utc_now_and_today(self):
        now = utc_now()
        assert now.tzinfo is not None
        assert today_utc() == now.date()

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("2026-10-16", date(2026, 10, 16)),
            ("2026-10-16T14:30:00Z", date(2026, 10, 16)),
            ("2026-10-16T14:30:00+00:00", date(2026, 10, 16)),
            ("2026-10-16 09:30", date(2026, 10, 16)),
            (date(2026, 1, 2), date(2026, 1, 2)),
            (datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc), date(2026, 1, 2)),
        ],
    )
    def test_parse_date(self, value, expected):
        assert parse_date(value) == expected

    def test_parse_date_prefix_fallback(self):
        assert parse_date("2026-10-16junk") == date(2026, 10, 16)

    @pytest.mark.parametrize("value", ["nope", "", 12345, None])
    def test_parse_date_invalid(self, value):
        with pytest.raises(InvalidChainError):
            parse_date(value)

    @pytest.mark.parametrize("value, expected", [("put", PUT), ("P", PUT), ("Puts", PUT), ("call", CALL), ("c", CALL), ("CALLS", CALL)])
    def test_normalize_right(self, value, expected):
        assert normalize_right(value) == expected

    def test_normalize_right_invalid(self):
        with pytest.raises(InvalidChainError):
            normalize_right("straddle")

    def test_to_int_fallback(self):
        from thetadesk.models import _to_int

        assert _to_int(None) == 0 and _to_int("12.7") == 12 and _to_int("abc") == 0

    def test_to_jsonable(self):
        q = OptionQuote("XYZ", date(2026, 10, 16), 95.0, PUT, 1.0, 1.2)
        out = to_jsonable({"q": q, "d": date(2026, 1, 1), "dt": datetime(2026, 1, 1, 1, 1), "l": [1.23456], "t": (1, 2)})
        assert out["q"]["expiry"] == "2026-10-16" and out["d"] == "2026-01-01"
        assert out["dt"].startswith("2026-01-01T01:01") and out["t"] == [1, 2]
        assert to_jsonable({"x": 1.23456}, ndigits=2) == {"x": 1.23}
        assert to_jsonable("s") == "s" and to_jsonable(None) is None


class TestOptionQuote:
    def test_properties(self):
        q = OptionQuote("XYZ", date(2026, 10, 16), 95.0, PUT, 1.0, 1.2, open_interest=10)
        assert q.mid == pytest.approx(1.1)
        assert q.spread == pytest.approx(0.2)
        assert q.spread_pct == pytest.approx(0.2 / 1.1)
        assert q.key == ("2026-10-16", 95.0, PUT)
        assert q.label == "XYZ 2026-10-16 95 put"

    def test_spread_pct_without_mid(self):
        q = OptionQuote("XYZ", date(2026, 10, 16), 95.0, PUT, 0.0, 0.0)
        assert q.spread_pct == 1.0


class TestChain:
    def test_find_get_expiries(self, chain):
        first = chain.options[0]
        assert chain.find(first.expiry, first.strike, first.right) is first
        assert chain.find(first.expiry.isoformat(), first.strike, first.right.upper()) is first
        assert chain.find(first.expiry, 999.0, first.right) is None
        assert chain.get(first.expiry, first.strike, first.right) is first
        with pytest.raises(OptionNotFoundError):
            chain.get(first.expiry, 999.0, first.right)
        assert chain.expiries() == sorted({q.expiry for q in chain.options})

    def test_to_dict_roundtrip(self, chain):
        data = chain.to_dict()
        again = parse_chain(data)
        assert again.underlying == chain.underlying
        assert len(again.options) == len(chain.options)
        assert again.options[0].expiry == chain.options[0].expiry


class TestParseChain:
    def _base(self, **overrides) -> dict:
        data = {
            "underlying": "xyz",
            "spot": 100.0,
            "as_of": "2026-09-11",
            "options": [
                {"expiry": "2026-10-16", "strike": 95, "right": "put", "bid": 1.0, "ask": 1.2,
                 "iv": 0.4, "delta": 0.3, "open_interest": "150", "volume": 10.0, "id": 42}
            ],
        }
        data.update(overrides)
        return data

    def test_canonical(self):
        c = parse_chain(self._base())
        assert c.underlying == "XYZ" and c.spot == 100.0 and c.as_of == date(2026, 9, 11)
        q = c.options[0]
        assert q.delta == -0.3  # sign fixed for puts
        assert q.open_interest == 150 and q.volume == 10 and q.id == "42"
        assert c.earnings_date is None and c.iv_rank is None and c.skipped == 0

    def test_json_string_and_optional_fields(self):
        data = self._base(earnings_date="2026-11-01", iv_rank="55")
        c = parse_chain(json.dumps(data))
        assert c.earnings_date == date(2026, 11, 1) and c.iv_rank == 55.0

    def test_robinhood_field_names(self):
        data = {
            "chain_symbol": "NBIS",
            "last_trade_price": "61.40",
            "updated_at": "2026-09-11T20:00:00Z",
            "instruments": [
                {"expiration_date": "2026-10-16", "strike_price": "55.0000", "type": "call",
                 "bid_price": "2.10", "ask_price": "2.30", "implied_volatility": "92.5",
                 "delta": "-0.25", "open_interest": 300, "instrument_id": "abc"}
            ],
        }
        c = parse_chain(data)
        q = c.options[0]
        assert c.underlying == "NBIS" and c.spot == 61.4
        assert q.right == CALL and q.strike == 55.0 and q.iv == pytest.approx(0.925)
        assert q.delta == 0.25 and q.id == "abc" and q.volume == 0

    def test_mark_fallback_and_skips(self):
        data = self._base(options=[
            {"expiry": "2026-10-16", "strike": 95, "right": "put", "mark_price": 1.5},
            {"expiry": "2026-10-16", "strike": 90, "right": "put"},
            {"expiry": "2026-10-16", "strike": 85, "right": "put", "bid": 0.5},
            {"expiry": "2026-10-16", "strike": 80, "right": "put", "ask": 0.7},
            {"expiry": "2026-10-16", "strike": 75, "right": "put", "bid": 0.9, "ask": 0.6, "iv": 0},
        ])
        c = parse_chain(data)
        assert c.skipped == 1
        assert [q.strike for q in c.options] == [95.0, 85.0, 80.0, 75.0]
        assert c.options[0].bid == c.options[0].ask == 1.5
        assert c.options[1].ask == 0.5 and c.options[2].bid == 0.7
        assert (c.options[3].bid, c.options[3].ask) == (0.6, 0.9)  # swapped
        assert c.options[3].iv is None

    def test_as_of_defaults_to_today(self):
        data = self._base()
        del data["as_of"]
        assert parse_chain(data).as_of == today_utc()

    def test_missing_options_ok(self):
        data = self._base()
        del data["options"]
        assert parse_chain(data).options == []

    @pytest.mark.parametrize(
        "mutate, message",
        [
            (lambda d: d.update(underlying=""), "underlying"),
            (lambda d: d.pop("spot"), "spot"),
            (lambda d: d.update(spot=0), "positive"),
            (lambda d: d.update(spot="abc"), "Invalid spot"),
            (lambda d: d.update(options="nope"), "must be a list"),
            (lambda d: d.update(options=["nope"]), "must be an object"),
            (lambda d: d.update(options=[{"strike": 1, "right": "put", "bid": 1, "ask": 1}]), "needs expiry"),
            (lambda d: d.update(options=[{"expiry": "2026-10-16", "strike": 0, "right": "put", "bid": 1, "ask": 1}]), "non-positive strike"),
            (lambda d: d.update(options=[{"expiry": "2026-10-16", "strike": "x", "right": "put", "bid": 1, "ask": 1}]), "Invalid strike"),
            (lambda d: d.update(options=[{"expiry": "2026-10-16", "strike": 1, "right": "put", "bid": -1, "ask": 1}]), "negative price"),
            (lambda d: d.update(options=[{"expiry": "2026-10-16", "strike": 1, "right": "put", "bid": "x", "ask": 1}]), "Invalid bid"),
        ],
    )
    def test_invalid_chains(self, mutate, message):
        data = self._base()
        mutate(data)
        with pytest.raises(InvalidChainError, match=message):
            parse_chain(data)

    def test_invalid_json_and_types(self):
        with pytest.raises(InvalidChainError, match="valid JSON"):
            parse_chain("{not json")
        with pytest.raises(InvalidChainError, match="JSON object"):
            parse_chain("[1, 2]")


class TestParseChains:
    def test_forms(self):
        one = {"underlying": "A", "spot": 10, "options": []}
        assert len(parse_chains(one)) == 1
        assert len(parse_chains([one, one])) == 2
        assert len(parse_chains(json.dumps([one]))) == 1
        assert parse_chains(json.dumps(one))[0].underlying == "A"

    def test_invalid(self):
        with pytest.raises(InvalidChainError, match="valid JSON"):
            parse_chains("nope")
        with pytest.raises(InvalidChainError, match="Expected a chain"):
            parse_chains(42)


class TestLoadChainFile:
    def test_load(self, tmp_path: Path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"underlying": "A", "spot": 10, "options": []}))
        assert load_chain_file(p).underlying == "A"
        assert load_chain_file(str(p)).spot == 10.0

    def test_missing(self, tmp_path: Path):
        with pytest.raises(InvalidChainError, match="Cannot read"):
            load_chain_file(tmp_path / "missing.json")

    def test_examples_parse(self, examples_dir: Path):
        for name in ("NBIS", "META", "HOOD"):
            c = load_chain_file(examples_dir / f"{name}.json")
            assert c.underlying == name and c.options and c.skipped == 0


class TestPosition:
    def test_properties(self):
        p = Position(1, "XYZ", PUT, 95.0, date(2026, 10, 16), 2, 1.5, 9500.0, "2026-09-11T00:00:00+00:00")
        assert p.strategy == "short_put"
        assert p.label == "XYZ 2026-10-16 95 put"
        assert p.premium_received == pytest.approx(300.0)
        assert p.total_collateral == pytest.approx(19000.0)
        assert Position(2, "XYZ", CALL, 95.0, date(2026, 10, 16), 1, 1.0, 9500.0, "t").strategy == "short_call"
