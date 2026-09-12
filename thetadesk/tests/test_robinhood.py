"""Tests for thetadesk.robinhood: joining Robinhood contract and quote payloads."""

from __future__ import annotations

import json
from datetime import date

import pytest

from thetadesk import robinhood
from thetadesk.models import InvalidChainError, parse_chain
from thetadesk.robinhood import build_chain, spot_from_equity_quotes


def instrument(ident: str, strike: float, right: str, expiry: str = "2026-10-16", symbol: str = "NBIS", state: str = "active") -> dict:
    return {
        "id": ident, "chain_id": "chain-1", "chain_symbol": symbol, "underlying_type": "equity",
        "expiration_date": expiry, "strike_price": f"{strike:.4f}", "type": right,
        "state": state, "tradability": "tradable", "trade_value_multiplier": "100.0000",
    }


def quote(ident: str, bid: str, ask: str, iv: str = "0.80", delta: str = "-0.25", oi: int = 500, vol: int = 40) -> dict:
    return {"quote": {
        "instrument_id": ident, "bid_price": bid, "ask_price": ask, "mark_price": ask,
        "implied_volatility": iv, "delta": delta, "open_interest": oi, "volume": vol,
        "chance_of_profit_short": "0.75", "updated_at": "2026-09-11T19:59:59Z",
    }, "close": {"price": bid}}


@pytest.fixture
def pages() -> tuple[list[dict], dict]:
    page1 = {"data": {"instruments": [instrument("a", 200, "put"), instrument("b", 250, "call")], "next": "http://x?cursor=abc"}}
    page2 = {"data": {"instruments": [instrument("c", 190, "put", "2026-10-30"), instrument("d", 300, "call", state="inactive"),
                                       instrument("e", 100, "put", symbol="OTHER")], "next": None}}
    quotes = {"data": {"results": [quote("a", "9.85", "10.40"), quote("b", "12.60", "13.35", delta="0.38"),
                                    quote("c", "8.70", "11.90", oi=0), quote("d", "1", "2"), quote("zzz", "1", "2")],
                       "closes_error": "closes omitted"}}
    return [page1, page2], quotes


class TestBuildChain:
    def test_joins_pages_and_quotes(self, pages):
        instruments, quotes = pages
        chain = build_chain("nbis", 224.43, instruments, quotes, as_of="2026-09-12", earnings_date="2026-11-10", iv_rank=58)
        assert chain["underlying"] == "NBIS" and chain["spot"] == 224.43 and chain["as_of"] == "2026-09-12"
        assert chain["earnings_date"] == "2026-11-10" and chain["iv_rank"] == 58.0 and chain["source"] == "robinhood"
        assert [o["id"] for o in chain["options"]] == ["a", "b", "c"]
        first = chain["options"][0]
        assert first == {"id": "a", "expiry": "2026-10-16", "strike": 200.0, "right": "put", "bid": 9.85, "ask": 10.4,
                         "mark": 10.4, "iv": 0.8, "delta": -0.25, "open_interest": 500, "volume": 40}
        # inactive 'd' and other-symbol 'e' are ignored; quote 'zzz' has no contract
        assert chain["unmatched"] == {"contracts_without_quotes": 0, "quotes_without_contracts": 2}
        parsed = parse_chain(chain)
        assert len(parsed.options) == 3 and parsed.earnings_date == date(2026, 11, 10)

    def test_accepts_json_strings_and_bare_rows(self, pages):
        instruments, quotes = pages
        chain = build_chain("NBIS", "224.43", json.dumps(instruments), json.dumps(quotes))
        assert len(chain["options"]) == 3 and chain["spot"] == 224.43
        bare = build_chain("NBIS", 224.43, [instrument("a", 200, "put")], [quote("a", "1", "2")["quote"]])
        assert len(bare["options"]) == 1
        single_page = build_chain("NBIS", 224.43, instruments[0], quotes)
        assert [o["id"] for o in single_page["options"]] == ["a", "b"]
        assert single_page["unmatched"]["quotes_without_contracts"] == 3

    def test_contracts_without_quotes_are_counted(self):
        chain = build_chain("NBIS", 224.43, [instrument("a", 200, "put"), instrument("b", 210, "put")], [quote("a", "1", "2")])
        assert len(chain["options"]) == 1 and chain["unmatched"]["contracts_without_quotes"] == 1

    def test_as_of_defaults_to_today(self):
        from thetadesk.models import today_utc

        chain = build_chain("NBIS", 224.43, [], [])
        assert chain["as_of"] == today_utc().isoformat() and chain["options"] == []
        assert "earnings_date" not in chain and "iv_rank" not in chain

    def test_spot_from_equity_quotes_payload(self):
        payload = {"data": {"results": [
            {"quote": {"symbol": "META", "last_trade_price": "648.230000"}, "close": {"price": "644.38"}},
            {"quote": {"symbol": "NBIS", "last_trade_price": "224.430000"}, "close": {"price": "228.11"}},
        ]}}
        assert spot_from_equity_quotes(payload, "nbis") == 224.43
        chain = build_chain("NBIS", payload, [], [])
        assert chain["spot"] == 224.43
        chain = build_chain("NBIS", json.dumps(payload), [], [])
        assert chain["spot"] == 224.43

    def test_spot_fallback_fields(self):
        payload = {"results": [{"quote": {"symbol": "NBIS", "last_trade_price": "", "mark_price": "225.5"}}]}
        assert spot_from_equity_quotes(payload, "NBIS") == 225.5
        with pytest.raises(InvalidChainError, match="No quote for HOOD"):
            spot_from_equity_quotes(payload, "HOOD")
        with pytest.raises(InvalidChainError, match="Expected a JSON object"):
            spot_from_equity_quotes([1, 2], "NBIS")

    def test_rows_without_ids_are_skipped(self):
        chain = build_chain("NBIS", 224.43, [instrument("a", 200, "put"), {"strike_price": "1"}], [quote("a", "1", "2"), {"quote": {"bid_price": "1"}}])
        assert len(chain["options"]) == 1 and chain["unmatched"] == {"contracts_without_quotes": 0, "quotes_without_contracts": 1}

    def test_strings_inside_lists_are_parsed_as_json(self):
        chain = build_chain("NBIS", 224.43, [json.dumps(instrument("a", 200, "put"))], [json.dumps(quote("a", "1", "2"))])
        assert len(chain["options"]) == 1
        with pytest.raises(InvalidChainError, match="not valid JSON"):
            build_chain("NBIS", 224.43, ["junk"], [])

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            (dict(underlying="  "), "underlying is required"),
            (dict(spot=0), "spot must be positive"),
            (dict(spot="abc"), "not valid JSON"),
            (dict(spot={"results": []}), "No quote"),
            (dict(instruments="{bad json"), "not valid JSON"),
            (dict(instruments=42), "Expected a JSON object"),
        ],
    )
    def test_invalid_inputs(self, kwargs, message):
        args = dict(underlying="NBIS", spot=224.43, instruments=[], quotes=[])
        args.update(kwargs)
        with pytest.raises(InvalidChainError, match=message):
            build_chain(**args)

    def test_helpers(self):
        assert robinhood._to_float(None) is None and robinhood._to_float("") is None
        assert robinhood._to_float("x") is None and robinhood._to_float("1.5") == 1.5
        assert robinhood._rows(None, ("results",)) == []
        assert robinhood._rows({"items": [{"a": 1}]}, ("results", "items")) == [{"a": 1}]
