"""Tests for the thetadesk CLI."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import click.testing
import pytest

from helpers import AS_OF, make_chain
from thetadesk.cli import main

EXPIRY = AS_OF + timedelta(days=35)


@pytest.fixture
def runner():
    return click.testing.CliRunner()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cli"
    d.mkdir()
    return d


@pytest.fixture
def chain_file(tmp_path: Path) -> str:
    p = tmp_path / "XYZ.json"
    p.write_text(json.dumps(make_chain().to_dict()))
    return str(p)


@pytest.fixture
def decayed_file(tmp_path: Path) -> str:
    c = make_chain()
    q = c.get(EXPIRY, 90.0, "put")
    q.bid, q.ask = round(q.bid * 0.3, 2), round(q.ask * 0.3, 2)
    p = tmp_path / "XYZ-decayed.json"
    p.write_text(json.dumps(c.to_dict()))
    return str(p)


@pytest.fixture
def old_chain_file(tmp_path: Path) -> str:
    """A chain dated in the past, so its options have really expired."""
    c = make_chain(as_of=date(2026, 6, 1))
    p = tmp_path / "OLD.json"
    p.write_text(json.dumps(c.to_dict()))
    return str(p)


def run(runner, data_dir: Path, *args: str):
    result = runner.invoke(main, ["--data-dir", str(data_dir), *args])
    try:
        payload = json.loads(result.output)
    except json.JSONDecodeError:
        payload = None
    return result, payload


def init(runner, data_dir: Path, cash: str = "100000"):
    result, payload = run(runner, data_dir, "init", "--cash", cash)
    assert result.exit_code == 0 and payload["ok"]
    return payload


class TestAccountCommands:
    def test_init_balance(self, runner, data_dir):
        payload = init(runner, data_dir, "25000")
        assert payload["data"]["cash"] == 25000.0
        result, payload = run(runner, data_dir, "balance")
        assert result.exit_code == 0 and payload["data"]["buying_power"] == 25000.0

    def test_balance_uninitialized(self, runner, data_dir):
        result, payload = run(runner, data_dir, "balance")
        assert result.exit_code == 1 and payload == {"ok": False, "error": "Account not initialized. Run 'thetadesk init' first.", "code": "NOT_INITIALIZED"}

    def test_reset(self, runner, data_dir):
        init(runner, data_dir)
        result, payload = run(runner, data_dir, "reset")
        assert result.exit_code == 1 and payload["code"] == "DESK_ERROR"
        result, payload = run(runner, data_dir, "reset", "--confirm")
        assert result.exit_code == 0 and payload["data"] == {"reset": True}
        assert run(runner, data_dir, "balance")[0].exit_code == 1

    def test_accounts_are_isolated(self, runner, data_dir):
        init(runner, data_dir)
        result, payload = run(runner, data_dir, "--account", "other", "balance")
        assert result.exit_code == 1 and payload["code"] == "NOT_INITIALIZED"
        assert (data_dir / "default" / "paper.db").exists()

    @pytest.mark.parametrize("name", ["../x", "a/b", "a\\b", " x"])
    def test_invalid_account(self, runner, data_dir, name):
        result, _ = run(runner, data_dir, "--account", name, "balance")
        assert result.exit_code != 0 and "Invalid account name" in result.output


class TestResearchCommands:
    def test_scan_defaults(self, runner, data_dir, chain_file):
        result, payload = run(runner, data_dir, "scan", chain_file, "--limit", "4")
        assert result.exit_code == 0 and payload["data"]["returned"] == 4
        cands = payload["data"]["candidates"]
        assert all(25 <= c["dte"] <= 50 for c in cands)
        assert payload["data"]["filters"]["rights"] == ["put", "call"]

    def test_scan_options(self, runner, data_dir, chain_file):
        result, payload = run(
            runner, data_dir, "scan", chain_file, chain_file, "--right", "call", "--min-dte", "1", "--max-dte", "100",
            "--min-delta", "0.05", "--max-delta", "0.5", "--min-oi", "1", "--min-volume", "1", "--max-spread", "0.5",
            "--min-credit", "0.05", "--min-pop", "0.5", "--include-itm", "--include-earnings", "--fill", "mid",
            "--sort", "dte", "--limit", "50",
        )
        assert result.exit_code == 0
        cands = payload["data"]["candidates"]
        assert cands and all(c["right"] == "call" for c in cands)
        assert [c["dte"] for c in cands] == sorted(c["dte"] for c in cands)
        assert payload["data"]["filters"]["fill"] == "mid" and payload["data"]["filters"]["otm_only"] is False

    def test_scan_missing_file(self, runner, data_dir, tmp_path):
        result, _ = run(runner, data_dir, "scan", str(tmp_path / "nope.json"))
        assert result.exit_code == 2

    def test_scan_bad_chain(self, runner, data_dir, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{}")
        result, payload = run(runner, data_dir, "scan", str(bad))
        assert result.exit_code == 1 and payload["code"] == "INVALID_CHAIN"

    def test_analyze(self, runner, data_dir, chain_file):
        init(runner, data_dir)
        result, payload = run(runner, data_dir, "analyze", chain_file, "--expiry", EXPIRY.isoformat(), "--strike", "90", "--right", "put", "--max-pct", "0.2")
        assert result.exit_code == 0
        assert payload["data"]["candidate"]["strike"] == 90.0 and payload["data"]["sizing"]["contracts"] == 2

    def test_analyze_not_found(self, runner, data_dir, chain_file):
        result, payload = run(runner, data_dir, "analyze", chain_file, "--expiry", EXPIRY.isoformat(), "--strike", "91", "--right", "put")
        assert result.exit_code == 1 and payload["code"] == "OPTION_NOT_FOUND"


def sell(runner, data_dir, chain_file, strike="90", right="put", contracts="1", *extra):
    result, payload = run(runner, data_dir, "sell", chain_file, "--expiry", EXPIRY.isoformat(), "--strike", strike, "--right", right, "--contracts", contracts, *extra)
    assert result.exit_code == 0, result.output
    return payload["data"]["position"]


class TestTradingCommands:
    def test_sell_and_positions(self, runner, data_dir, chain_file):
        init(runner, data_dir)
        pos = sell(runner, data_dir, chain_file, "90", "put", "2", "--notes", "thesis", "--fill", "mid")
        assert pos["id"] == 1 and pos["contracts"] == 2 and pos["notes"] == "thesis"
        result, payload = run(runner, data_dir, "positions")
        assert result.exit_code == 0 and payload["data"][0]["evaluation"] is None
        result, payload = run(runner, data_dir, "positions", chain_file)
        assert payload["data"][0]["evaluation"]["action"] == "HOLD"

    def test_sell_price_override(self, runner, data_dir, chain_file):
        init(runner, data_dir)
        pos = sell(runner, data_dir, chain_file, "90", "put", "1", "--price", "2.5")
        assert pos["credit"] == 2.5

    def test_sell_insufficient(self, runner, data_dir, chain_file):
        init(runner, data_dir, "5000")
        result, payload = run(runner, data_dir, "sell", chain_file, "--expiry", EXPIRY.isoformat(), "--strike", "90", "--right", "put", "--contracts", "1")
        assert result.exit_code == 1 and payload["code"] == "INSUFFICIENT_BUYING_POWER"

    def test_close_price_and_chain(self, runner, data_dir, chain_file):
        init(runner, data_dir)
        a = sell(runner, data_dir, chain_file)
        b = sell(runner, data_dir, chain_file, "85")
        result, payload = run(runner, data_dir, "close", str(a["id"]), "--price", "0.2", "--reason", "take_profit")
        assert result.exit_code == 0 and payload["data"]["status"] == "closed" and payload["data"]["close_reason"] == "take_profit"
        result, payload = run(runner, data_dir, "close", str(b["id"]), "--chain", chain_file)
        assert result.exit_code == 0 and payload["data"]["close_price"] > 0
        result, payload = run(runner, data_dir, "close", "999", "--price", "1")
        assert result.exit_code == 1 and payload["code"] == "POSITION_NOT_FOUND"

    def test_roll(self, runner, data_dir, chain_file):
        init(runner, data_dir)
        a = sell(runner, data_dir, chain_file)
        later = (EXPIRY + timedelta(days=28)).isoformat()
        result, payload = run(runner, data_dir, "roll", str(a["id"]), "--chain", chain_file, "--expiry", later, "--strike", "85", "--fill", "mid")
        assert result.exit_code == 0
        assert payload["data"]["opened"]["strike"] == 85.0 and payload["data"]["opened"]["expiry"] == later
        assert payload["data"]["closed"]["close_reason"] == "roll"

    def test_settle(self, runner, data_dir, chain_file, old_chain_file):
        init(runner, data_dir)
        a = sell(runner, data_dir, chain_file)
        result, payload = run(runner, data_dir, "settle", str(a["id"]), "--spot", "95")
        assert result.exit_code == 1 and payload["code"] == "INVALID_ORDER"
        result, payload = run(runner, data_dir, "sell", old_chain_file, "--expiry", "2026-07-06", "--strike", "90", "--right", "put", "--contracts", "1")
        assert result.exit_code == 0
        old_id = payload["data"]["position"]["id"]
        result, payload = run(runner, data_dir, "settle", str(old_id), "--spot", "80")
        assert result.exit_code == 0 and payload["data"]["status"] == "assigned" and payload["data"]["close_price"] == 10.0


class TestManageCommands:
    def test_manage_and_apply(self, runner, data_dir, chain_file, decayed_file):
        init(runner, data_dir)
        a = sell(runner, data_dir, chain_file)
        result, payload = run(runner, data_dir, "manage", decayed_file, "--take-profit", "0.5", "--stop-loss", "2", "--manage-dte", "21", "--tested-delta", "0.4", "--fill", "bid")
        assert result.exit_code == 0 and payload["data"]["summary"] == {"CLOSE": 1} and payload["data"]["applied"] == []
        result, payload = run(runner, data_dir, "manage", decayed_file, "--apply")
        assert payload["data"]["applied"][0]["position_id"] == a["id"]
        result, payload = run(runner, data_dir, "stats")
        assert result.exit_code == 0 and payload["data"]["closed_trades"] == 1 and payload["data"]["wins"] == 1

    def test_journal_and_export(self, runner, data_dir, chain_file):
        init(runner, data_dir)
        a = sell(runner, data_dir, chain_file)
        run(runner, data_dir, "close", str(a["id"]), "--price", "0.5")
        result, payload = run(runner, data_dir, "journal", "--limit", "1")
        assert result.exit_code == 0 and len(payload["data"]) == 1 and payload["data"][0]["action"] == "buy_to_close"
        result, payload = run(runner, data_dir, "export", "trades")
        assert result.exit_code == 0 and isinstance(payload, list) and len(payload) == 2
        result, _ = run(runner, data_dir, "export", "positions", "--format", "csv")
        assert result.exit_code == 0 and result.output.splitlines()[0].startswith("id,underlying")


class TestMcpCommand:
    def test_mcp_starts_server(self, runner):
        with patch("thetadesk.mcp_server.main") as fake:
            result = runner.invoke(main, ["mcp"])
        assert result.exit_code == 0 and fake.called


class TestChainFromRobinhood:
    def _files(self, tmp_path: Path) -> tuple[str, str, str]:
        inst = {"data": {"instruments": [
            {"id": "a", "chain_symbol": "NBIS", "expiration_date": "2026-10-16", "strike_price": "200.0000", "type": "put", "state": "active"},
        ], "next": None}}
        quotes = {"data": {"results": [{"quote": {"instrument_id": "a", "bid_price": "9.85", "ask_price": "10.40",
                                                   "implied_volatility": "0.78", "delta": "-0.27", "open_interest": 2008, "volume": 526}}]}}
        spot = {"data": {"results": [{"quote": {"symbol": "NBIS", "last_trade_price": "224.43"}}]}}
        paths = []
        for name, payload in (("inst.json", inst), ("quotes.json", quotes), ("spot.json", spot)):
            p = tmp_path / name
            p.write_text(json.dumps(payload))
            paths.append(str(p))
        return tuple(paths)

    def test_writes_chain_file(self, runner, data_dir, tmp_path):
        inst, quotes, spot = self._files(tmp_path)
        out = tmp_path / "NBIS.json"
        result, payload = run(runner, data_dir, "chain-from-robinhood", "--underlying", "nbis", "--spot", spot,
                              "--instruments", inst, "--quotes", quotes, "--as-of", "2026-09-12", "--earnings", "2026-11-10",
                              "--iv-rank", "58", "--out", str(out))
        assert result.exit_code == 0 and payload["data"]["options"] == 1
        chain = json.loads(out.read_text())
        assert chain["spot"] == 224.43 and chain["options"][0]["strike"] == 200.0 and chain["earnings_date"] == "2026-11-10"
        result, payload = run(runner, data_dir, "scan", str(out), "--min-oi", "1")
        assert result.exit_code == 0 and payload["data"]["returned"] == 1

    def test_prints_chain_and_reports_errors(self, runner, data_dir, tmp_path):
        inst, quotes, _ = self._files(tmp_path)
        result, payload = run(runner, data_dir, "chain-from-robinhood", "--underlying", "NBIS", "--spot", "224.43",
                              "--instruments", inst, "--quotes", quotes)
        assert result.exit_code == 0 and payload["underlying"] == "NBIS" and len(payload["options"]) == 1
        result, payload = run(runner, data_dir, "chain-from-robinhood", "--underlying", "NBIS", "--spot", "abc",
                              "--instruments", inst, "--quotes", quotes)
        assert result.exit_code == 1 and payload["code"] == "INVALID_CHAIN"
