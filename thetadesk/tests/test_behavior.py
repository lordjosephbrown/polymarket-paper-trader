"""Behavior tests: the workflow an agent runs, end to end, on the sample chains."""

from __future__ import annotations

import copy
import csv
import io
import json
from pathlib import Path

import pytest

from thetadesk import mcp_server
from thetadesk.models import load_chain_file, parse_date


@pytest.fixture(autouse=True)
def fresh_desk(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("THETADESK_DATA_DIR", str(tmp_path / "agent"))
    mcp_server._desk = None
    yield
    if mcp_server._desk is not None:
        mcp_server._desk.close()
        mcp_server._desk = None


def call(fn, *args, **kwargs):
    payload = json.loads(fn(*args, **kwargs))
    assert payload["ok"], payload
    return payload["data"]


def test_agent_workflow_on_sample_chains(examples_dir: Path):
    chains = [json.loads((examples_dir / f"{s}.json").read_text()) for s in ("NBIS", "META", "HOOD")]

    # 1. Set up and learn the chain format.
    assert call(mcp_server.init_account, 100_000.0)["cash"] == 100_000.0
    assert "options" in call(mcp_server.chain_format)["example"]

    # 2. Scan all three underlyings for cash-secured puts.
    report = call(mcp_server.scan_chains, chains, rights="put", limit=5)
    assert report["evaluated"] > 500 and report["returned"] == 5
    top = report["candidates"][0]
    assert 25 <= top["dte"] <= 50 and 0.10 <= abs(top["delta"]) <= 0.30
    assert top["pop"] > 0.5 and top["collateral"] == top["strike"] * 100
    assert not top["earnings_in_window"]

    # 3. Analyze and size the top candidate, then paper-sell it.
    chain = next(c for c in chains if c["underlying"] == top["underlying"])
    analysis = call(mcp_server.analyze_option, chain, top["expiry"], top["strike"], "put", max_pct_per_position=0.10)
    contracts = analysis["sizing"]["contracts"]
    assert contracts >= 1
    sold = call(mcp_server.paper_sell, chain, top["expiry"], top["strike"], "put", contracts, notes="rich IV, 25-delta")
    pid = sold["position"]["id"]
    balance = call(mcp_server.get_balance)
    assert balance["collateral_in_use"] == pytest.approx(top["collateral"] * contracts)
    assert balance["cash"] == pytest.approx(100_000 + top["credit"] * 100 * contracts)

    # 4. Nothing to do while the position is within the rules.
    managed = call(mcp_server.manage_positions, chain)
    assert managed["summary"] == {"HOLD": 1}

    # 5. Premium decays to 40% of the credit: the rules say take profit; apply it.
    decayed = copy.deepcopy(chain)
    for row in decayed["options"]:
        if row["expiry"] == top["expiry"] and row["strike"] == top["strike"] and row["right"] == "put":
            row["bid"], row["ask"] = round(top["credit"] * 0.4, 2), round(top["credit"] * 0.44, 2)
    managed = call(mcp_server.manage_positions, decayed, apply=True)
    assert managed["applied"][0] == {
        "position_id": pid, "action": "CLOSE", "price": round(top["credit"] * 0.44, 2),
        "realized_pnl": managed["applied"][0]["realized_pnl"], "reason": "take_profit",
    }
    assert managed["applied"][0]["realized_pnl"] > 0

    # 6. A new position gets tested when the stock drops through the strike: roll it.
    sold = call(mcp_server.paper_sell, chain, top["expiry"], top["strike"], "put", 1)
    pid2 = sold["position"]["id"]
    crashed = copy.deepcopy(chain)
    crashed["spot"] = top["strike"] * 0.95
    managed = call(mcp_server.manage_positions, crashed, apply=True)
    ev = managed["evaluations"][0]
    assert ev["action"] == "ROLL" and ev["reason"] == "tested" and ev["tested"]
    assert managed["applied"][0]["executed"] is False
    candidate = ev["roll_candidates"][0]
    rolled = call(mcp_server.paper_roll, pid2, crashed, candidate["expiry"], new_strike=candidate["strike"])
    assert rolled["opened"]["rolled_from"] == pid2
    assert rolled["net_credit"] == pytest.approx(candidate["net_credit"], abs=1e-6)

    # 7. The rolled position expires worthless on its expiry date.
    settle_day = parse_date(rolled["opened"]["expiry"])
    settled = call(mcp_server.manage_positions, crashed, apply=True)  # not expired yet: HOLD/ROLL only
    assert all(a["action"] != "EXPIRE" for a in settled["applied"])
    expiry_chain = copy.deepcopy(crashed)
    expiry_chain["as_of"] = settle_day.isoformat()
    expiry_chain["spot"] = candidate["strike"] * 1.2
    settled = call(mcp_server.manage_positions, expiry_chain, apply=True)
    assert settled["applied"][0]["action"] == "EXPIRE"

    # 8. The journal tells the story.
    stats = call(mcp_server.stats)
    assert stats["closed_trades"] == 3 and stats["open_trades"] == 0
    assert stats["by_close_reason"] == {"take_profit": 1, "roll": 1, "expired_worthless": 1}
    assert stats["wins"] >= 2
    actions = [t["action"] for t in call(mcp_server.journal, limit=10)]
    assert actions == ["expire", "sell_to_open", "buy_to_close", "sell_to_open", "buy_to_close", "sell_to_open"]
    rows = list(csv.DictReader(io.StringIO(call(mcp_server.export_journal, "positions", "csv")["content"])))
    assert len(rows) == 3 and {r["status"] for r in rows} == {"closed", "expired"}


def test_robinhood_shaped_payload_scans(examples_dir: Path):
    """An agent can pass broker-style field names straight through."""
    chain = load_chain_file(examples_dir / "HOOD.json")
    payload = {
        "chain_symbol": "HOOD",
        "last_trade_price": chain.spot,
        "updated_at": chain.as_of.isoformat() + "T20:00:00Z",
        "instruments": [
            {
                "expiration_date": q.expiry.isoformat(),
                "strike_price": f"{q.strike:.4f}",
                "type": q.right,
                "bid_price": str(q.bid),
                "ask_price": str(q.ask),
                "implied_volatility": str(q.iv),
                "delta": str(q.delta),
                "open_interest": q.open_interest,
                "volume": q.volume,
            }
            for q in chain.options
        ],
    }
    report = call(mcp_server.scan_chains, payload, limit=3)
    assert report["underlyings"] == ["HOOD"] and report["returned"] == 3
