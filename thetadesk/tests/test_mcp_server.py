"""Tests for the thetadesk MCP server tools."""

from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from helpers import AS_OF, make_chain
from thetadesk import mcp_server

EXPIRY = AS_OF + timedelta(days=35)


@pytest.fixture(autouse=True)
def fresh_desk(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("THETADESK_DATA_DIR", str(tmp_path / "mcp"))
    mcp_server._desk = None
    yield
    if mcp_server._desk is not None:
        mcp_server._desk.close()
        mcp_server._desk = None


@pytest.fixture
def chain_dict() -> dict:
    return make_chain().to_dict()


def ok(raw: str):
    payload = json.loads(raw)
    assert payload["ok"], payload
    return payload["data"]


def err(raw: str) -> str:
    payload = json.loads(raw)
    assert not payload["ok"], payload
    return payload["code"]


class TestEnvelopes:
    def test_ok_err(self):
        assert json.loads(mcp_server._ok({"x": 1.23456})) == {"ok": True, "data": {"x": 1.2346}}
        assert json.loads(mcp_server._err("boom", "CODE")) == {"ok": False, "error": "boom", "code": "CODE"}

    def test_err_from(self):
        from thetadesk.models import InvalidOrderError

        assert json.loads(mcp_server._err_from(InvalidOrderError("bad")))["code"] == "INVALID_ORDER"
        assert json.loads(mcp_server._err_from(ValueError("v")))["code"] == "ValueError"
        out = json.loads(mcp_server._err_from(RuntimeError("secret")))
        assert out == {"ok": False, "error": "Internal error", "code": "internal_error"}

    def test_rights_parsing(self):
        assert mcp_server._rights("both") == ("put", "call")
        assert mcp_server._rights("") == ("put", "call")
        assert mcp_server._rights("put") == ("put",)
        assert mcp_server._rights("put, call") == ("put", "call")


class TestDeskLifecycle:
    def test_data_root_env_and_home(self, monkeypatch, tmp_path):
        assert mcp_server._data_root() == tmp_path / "mcp"
        monkeypatch.delenv("THETADESK_DATA_DIR")
        with patch.object(Path, "home", return_value=tmp_path / "home"):
            assert mcp_server._data_root() == tmp_path / "home" / ".thetadesk"

    def test_switching_accounts_closes_previous(self):
        first = mcp_server._get_desk("a")
        assert mcp_server._get_desk("a") is first
        second = mcp_server._get_desk("b")
        assert second is not first and second.ledger.data_dir.name == "b"

    @pytest.mark.parametrize("name", ["", "../x", "a/b", "a\\b", " a"])
    def test_invalid_account(self, name):
        assert err(mcp_server.get_balance(account=name)) == "ValueError"


class TestAccountTools:
    def test_init_balance_reset(self):
        data = ok(mcp_server.init_account(50_000.0))
        assert data["cash"] == 50_000.0
        assert ok(mcp_server.get_balance())["buying_power"] == 50_000.0
        assert ok(mcp_server.reset_account()) == {"reset": True}
        assert err(mcp_server.get_balance()) == "NOT_INITIALIZED"

    def test_init_invalid(self):
        assert err(mcp_server.init_account(0)) == "INVALID_ORDER"
        assert err(mcp_server.reset_account(account="../x")) == "ValueError"


class TestResearchTools:
    def test_chain_format(self):
        data = ok(mcp_server.chain_format())
        assert data["example"]["underlying"] == "NBIS" and data["notes"]

    def test_chain_from_robinhood(self):
        instruments = {"data": {"instruments": [
            {"id": "a", "chain_symbol": "HOOD", "expiration_date": "2026-10-16", "strike_price": "100.0000", "type": "put", "state": "active"},
        ]}}
        quotes = {"data": {"results": [{"quote": {"instrument_id": "a", "bid_price": "2.77", "ask_price": "3.05",
                                                   "implied_volatility": "0.59", "delta": "-0.22", "open_interest": 4606, "volume": 1783}}]}}
        data = ok(mcp_server.chain_from_robinhood("hood", 112.57, instruments, json.dumps(quotes), as_of="2026-09-12", earnings_date="2026-11-04"))
        assert data["underlying"] == "HOOD" and data["options"][0]["bid"] == 2.77 and data["unmatched"]["contracts_without_quotes"] == 0
        report = ok(mcp_server.scan_chains(data, min_open_interest=1))
        assert report["returned"] == 1
        assert err(mcp_server.chain_from_robinhood("HOOD", 0, instruments, quotes)) == "INVALID_CHAIN"

    def test_chain_from_csv(self):
        contracts = "id,expiry,strike,right\n53480429-af13-485b-84c5-ed1b71070b28,2026-10-16,100,put\n"
        quotes = "id,bid,ask,iv,delta,open_interest,volume\n53480429,2.77,3.05,0.59,-0.22,4606,1783\n"
        data = ok(mcp_server.chain_from_csv("hood", "112.57", contracts, quotes, as_of="2026-09-12", earnings_date="2026-11-04"))
        assert data["underlying"] == "HOOD" and data["options"][0]["open_interest"] == 4606 and data["unmatched"]["contracts_without_quotes"] == 0
        assert ok(mcp_server.scan_chains(data))["returned"] == 1
        assert err(mcp_server.chain_from_csv("HOOD", 112.57, contracts, "id,bid\n53480429,1\n")) == "INVALID_CHAIN"

    def test_scan_forms(self, chain_dict):
        as_dict = ok(mcp_server.scan_chains(chain_dict, limit=3))
        as_list = ok(mcp_server.scan_chains([chain_dict], limit=3))
        as_str = ok(mcp_server.scan_chains(json.dumps(chain_dict), limit=3))
        assert as_dict["returned"] == as_list["returned"] == as_str["returned"] == 3
        puts = ok(mcp_server.scan_chains(chain_dict, rights="put", limit=50))
        assert all(c["right"] == "put" for c in puts["candidates"])

    def test_scan_errors(self, chain_dict):
        assert err(mcp_server.scan_chains("not json")) == "INVALID_CHAIN"
        assert err(mcp_server.scan_chains(chain_dict, sort="vibes")) == "INVALID_ORDER"
        assert err(mcp_server.scan_chains(chain_dict, fill="last")) == "INVALID_ORDER"

    def test_analyze_option(self, chain_dict):
        ok(mcp_server.init_account(100_000.0))
        data = ok(mcp_server.analyze_option(chain_dict, EXPIRY.isoformat(), 90.0, "put", max_pct_per_position=0.2))
        assert data["candidate"]["strike"] == 90.0 and data["sizing"]["contracts"] == 2
        assert err(mcp_server.analyze_option(chain_dict, EXPIRY.isoformat(), 91.0, "put")) == "OPTION_NOT_FOUND"

    def test_size_for(self):
        data = ok(mcp_server.size_for(9000.0, 100_000.0, 90_000.0, max_pct_per_position=0.2, max_contracts=1))
        assert data["contracts"] == 1 and data["limited_by"] == "max_contracts"
        assert err(mcp_server.size_for(0.0, 1.0, 1.0)) == "INVALID_ORDER"


class TestTradingTools:
    def test_sell_close(self, chain_dict):
        ok(mcp_server.init_account(100_000.0))
        data = ok(mcp_server.paper_sell(chain_dict, EXPIRY.isoformat(), 90.0, "put", 2, notes="thesis"))
        pid = data["position"]["id"]
        assert data["position"]["contracts"] == 2 and data["candidate"]["pop"] > 0.5
        assert err(mcp_server.paper_close(pid)) == "INVALID_ORDER"
        closed = ok(mcp_server.paper_close(pid, chain=json.dumps(chain_dict), reason="take_profit"))
        assert closed["status"] == "closed" and closed["close_reason"] == "take_profit"
        assert err(mcp_server.paper_close(pid, price=0.1)) == "POSITION_CLOSED"

    def test_sell_errors(self, chain_dict):
        assert err(mcp_server.paper_sell(chain_dict, EXPIRY.isoformat(), 90.0, "put", 1)) == "NOT_INITIALIZED"
        ok(mcp_server.init_account(1_000.0))
        assert err(mcp_server.paper_sell(chain_dict, EXPIRY.isoformat(), 90.0, "put", 1)) == "INSUFFICIENT_BUYING_POWER"

    def test_roll_and_settle(self, chain_dict):
        ok(mcp_server.init_account(100_000.0))
        pid = ok(mcp_server.paper_sell(chain_dict, EXPIRY.isoformat(), 90.0, "put", 1))["position"]["id"]
        later = (EXPIRY + timedelta(days=28)).isoformat()
        data = ok(mcp_server.paper_roll(pid, chain_dict, later, new_strike=85.0, fill="mid"))
        assert data["opened"]["expiry"] == later and data["opened"]["strike"] == 85.0
        assert err(mcp_server.paper_roll(pid, chain_dict, later)) == "POSITION_CLOSED"
        assert err(mcp_server.paper_settle(data["opened"]["id"], 80.0)) == "INVALID_ORDER"
        old = make_chain(as_of=date(2026, 6, 1)).to_dict()
        old_id = ok(mcp_server.paper_sell(old, "2026-07-06", 90.0, "put", 1))["position"]["id"]
        settled = ok(mcp_server.paper_settle(old_id, 80.0))
        assert settled["status"] == "assigned" and settled["close_price"] == 10.0


class TestMonitoringTools:
    def test_positions_and_manage(self, chain_dict):
        ok(mcp_server.init_account(100_000.0))
        pid = ok(mcp_server.paper_sell(chain_dict, EXPIRY.isoformat(), 90.0, "put", 1))["position"]["id"]
        views = ok(mcp_server.paper_positions())
        assert views[0]["position"]["id"] == pid and views[0]["evaluation"] is None
        views = ok(mcp_server.paper_positions(chain_dict, take_profit_pct=0.5))
        assert views[0]["evaluation"]["action"] == "HOLD"
        decayed = make_chain()
        q = decayed.get(EXPIRY, 90.0, "put")
        q.bid, q.ask = round(q.bid * 0.3, 2), round(q.ask * 0.3, 2)
        res = ok(mcp_server.manage_positions([decayed.to_dict()]))
        assert res["summary"] == {"CLOSE": 1} and res["applied"] == []
        res = ok(mcp_server.manage_positions(decayed.to_dict(), apply=True))
        assert res["applied"][0]["action"] == "CLOSE"
        assert err(mcp_server.manage_positions(chain_dict, take_profit_pct=0)) == "INVALID_ORDER"
        assert err(mcp_server.paper_positions("nope")) == "INVALID_CHAIN"


class TestJournalTools:
    def test_stats_journal_export(self, chain_dict):
        ok(mcp_server.init_account(100_000.0))
        pid = ok(mcp_server.paper_sell(chain_dict, EXPIRY.isoformat(), 90.0, "put", 1))["position"]["id"]
        ok(mcp_server.paper_close(pid, price=0.2))
        assert ok(mcp_server.stats())["closed_trades"] == 1
        assert [t["action"] for t in ok(mcp_server.journal(limit=5))] == ["buy_to_close", "sell_to_open"]
        data = ok(mcp_server.export_journal("trades", "csv"))
        assert data["format"] == "csv" and "buy_to_close" in data["content"]
        assert err(mcp_server.export_journal("orders")) == "INVALID_ORDER"
        assert err(mcp_server.stats(account="../x")) == "ValueError"
        assert err(mcp_server.journal(account="../x")) == "ValueError"


class TestServer:
    def test_tools_registered(self):
        import asyncio

        names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
        assert {"scan_chains", "paper_sell", "manage_positions", "paper_roll", "chain_format"} <= names

    def test_main_runs_stdio(self):
        with patch.object(mcp_server.mcp, "run") as fake_run:
            mcp_server.main()
        fake_run.assert_called_once_with(transport="stdio")

    def test_v1_sdk_fallback(self, monkeypatch):
        fake = types.ModuleType("mcp.server.fastmcp")

        class FastMCP:
            def __init__(self, name: str, instructions: str | None = None) -> None:
                self.name, self.instructions, self.tools, self.ran = name, instructions, [], None

            def tool(self):
                def deco(fn):
                    self.tools.append(fn.__name__)
                    return fn

                return deco

            def run(self, transport: str = "stdio") -> None:
                self.ran = transport

        fake.FastMCP = FastMCP
        monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)
        monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fake)
        try:
            reloaded = importlib.reload(mcp_server)
            assert isinstance(reloaded.mcp, FastMCP) and "paper_sell" in reloaded.mcp.tools
            reloaded.main()
            assert reloaded.mcp.ran == "stdio"
        finally:
            monkeypatch.undo()
            importlib.reload(mcp_server)
        assert type(mcp_server.mcp).__name__ != "FastMCP" or "mcp.server.fastmcp" in sys.modules
