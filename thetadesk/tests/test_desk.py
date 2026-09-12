"""Tests for thetadesk.desk: the orchestration layer."""

from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path

import pytest

from helpers import AS_OF, make_chain
from thetadesk.desk import Desk, ManageResult, PositionView
from thetadesk.models import (
    STATUS_ASSIGNED,
    STATUS_CLOSED,
    STATUS_EXPIRED,
    STATUS_OPEN,
    Chain,
    InvalidOrderError,
    OptionNotFoundError,
    PositionClosedError,
)
from thetadesk.rules import ACTION_ASSIGN, ACTION_CLOSE, ACTION_EXPIRE, ACTION_NO_DATA, ACTION_ROLL, ManagementRules
from thetadesk.scanner import ScanFilters

EXPIRY = AS_OF + timedelta(days=35)


@pytest.fixture
def desk(tmp_data_dir: Path):
    d = Desk(tmp_data_dir)
    d.init_account(100_000.0)
    yield d
    d.close()


def decayed(chain: Chain, strike: float, right: str, factor: float, expiry: date = EXPIRY) -> Chain:
    c = copy.deepcopy(chain)
    q = c.get(expiry, strike, right)
    q.bid, q.ask = round(q.bid * factor, 2), round(q.ask * factor, 2)
    return c


class TestAccount:
    def test_init_balance_reset(self, desk: Desk):
        info = desk.init_account(50_000.0)
        assert info["cash"] == 50_000.0 and info["starting_cash"] == 50_000.0 and "created_at" in info
        assert desk.balance()["buying_power"] == 50_000.0
        desk.reset()
        assert not desk.ledger.is_initialized()


class TestResearch:
    def test_scan(self, desk: Desk, chain: Chain):
        rep = desk.scan([chain], ScanFilters(), limit=3, sort="pop")
        assert rep["returned"] == 3 and rep["sort"] == "pop"

    def test_analyze_sizes_from_account(self, desk: Desk, chain: Chain):
        out = desk.analyze(chain, EXPIRY, 90.0, "put")
        assert out["candidate"].strike == 90.0
        assert out["sizing"].contracts == 0 and out["sizing"].note  # 5% of 100k < 9000
        out = desk.analyze(chain, EXPIRY, 90.0, "put", max_pct_per_position=0.2)
        assert out["sizing"].contracts == 2 and out["sizing"].limited_by == "per_position_limit"

    def test_analyze_explicit_values(self, desk: Desk, chain: Chain):
        out = desk.analyze(chain, EXPIRY, 90.0, "put", account_value=1_000_000.0, buying_power=9_500.0)
        assert out["sizing"].contracts == 1 and out["sizing"].limited_by == "buying_power"

    def test_analyze_without_account(self, tmp_data_dir: Path, chain: Chain):
        d = Desk(tmp_data_dir / "fresh")
        assert d.analyze(chain, EXPIRY, 90.0, "put")["sizing"] is None
        d.close()

    def test_analyze_unknown_option(self, desk: Desk, chain: Chain):
        with pytest.raises(OptionNotFoundError):
            desk.analyze(chain, EXPIRY, 91.0, "put")


class TestTrading:
    def test_sell_at_bid(self, desk: Desk, chain: Chain):
        q = chain.get(EXPIRY, 90.0, "put")
        out = desk.sell(chain, EXPIRY, 90.0, "put", 2, notes="premium")
        p = out["position"]
        assert p.credit == q.bid and p.collateral == 9000.0 and p.contracts == 2
        assert p.spot_at_open == 100.0 and p.delta_at_open == q.delta and p.iv_at_open == 0.4 and p.notes == "premium"
        assert out["candidate"].label == "XYZ 2026-10-16 90 put"
        assert desk.balance()["collateral_in_use"] == 18_000.0

    def test_sell_price_override_and_mid(self, desk: Desk, chain: Chain):
        assert desk.sell(chain, EXPIRY, 90.0, "put", 1, price=2.5)["position"].credit == 2.5
        q = chain.get(EXPIRY, 90.0, "put")
        assert desk.sell(chain, EXPIRY, 90.0, "put", 1, fill="mid")["position"].credit == pytest.approx(q.mid)

    def test_sell_expired_option(self, desk: Desk):
        c = make_chain(dtes=(0,))  # only intrinsic value remains; 110 put is priced
        with pytest.raises(InvalidOrderError, match="expired"):
            desk.sell(c, AS_OF, 110.0, "put", 1)

    def test_close_with_price_or_chain(self, desk: Desk, chain: Chain):
        a = desk.sell(chain, EXPIRY, 90.0, "put", 1)["position"]
        b = desk.sell(chain, EXPIRY, 85.0, "put", 1)["position"]
        with pytest.raises(InvalidOrderError):
            desk.close_position(a.id)
        closed = desk.close_position(a.id, price=0.3, reason="take_profit")
        assert closed.status == STATUS_CLOSED and closed.close_price == 0.3 and closed.close_reason == "take_profit"
        closed = desk.close_position(b.id, chain=chain)
        assert closed.close_price == chain.get(EXPIRY, 85.0, "put").ask

    def test_settle(self, desk: Desk, chain: Chain):
        p = desk.sell(chain, EXPIRY, 90.0, "put", 1)["position"]
        s = desk.settle(p.id, 80.0, today=EXPIRY)
        assert s.status == STATUS_ASSIGNED and s.close_price == 10.0

    def test_roll(self, desk: Desk, chain: Chain):
        p = desk.sell(chain, EXPIRY, 90.0, "put", 1)["position"]
        later = EXPIRY + timedelta(days=28)
        out = desk.roll(p.id, chain, later)
        assert out["closed"].close_reason == "roll" and out["closed"].close_price == chain.get(EXPIRY, 90.0, "put").ask
        assert out["opened"].expiry == later and out["opened"].strike == 90.0 and out["opened"].rolled_from == p.id
        assert out["net_credit"] == pytest.approx(chain.get(later, 90.0, "put").bid - chain.get(EXPIRY, 90.0, "put").ask)
        assert out["net_credit_total"] == pytest.approx(out["net_credit"] * 100)

    def test_roll_new_strike_and_errors(self, desk: Desk, chain: Chain):
        p = desk.sell(chain, EXPIRY, 90.0, "put", 1)["position"]
        later = EXPIRY + timedelta(days=28)
        with pytest.raises(InvalidOrderError, match="later expiry"):
            desk.roll(p.id, chain, EXPIRY)
        out = desk.roll(p.id, chain, later, new_strike=85.0, fill="mid")
        assert out["opened"].strike == 85.0 and out["opened"].collateral == 8500.0
        with pytest.raises(PositionClosedError):
            desk.roll(p.id, chain, later)


class TestMonitoring:
    def test_positions_with_and_without_chains(self, desk: Desk, chain: Chain):
        desk.sell(chain, EXPIRY, 90.0, "put", 1)
        views = desk.positions()
        assert len(views) == 1 and isinstance(views[0], PositionView) and views[0].evaluation is None
        views = desk.positions([chain])
        assert views[0].evaluation is not None and views[0].evaluation.spot == 100.0
        other = make_chain("OTHER")
        assert desk.positions([other])[0].evaluation is None

    def test_manage_evaluates_without_apply(self, desk: Desk, chain: Chain):
        p = desk.sell(chain, EXPIRY, 90.0, "put", 1)["position"]
        res = desk.manage([decayed(chain, 90.0, "put", 0.3)])
        assert isinstance(res, ManageResult) and res.summary == {ACTION_CLOSE: 1} and res.applied == []
        assert desk.ledger.get_position(p.id).status == STATUS_OPEN

    def test_manage_apply_close(self, desk: Desk, chain: Chain):
        p = desk.sell(chain, EXPIRY, 90.0, "put", 1)["position"]
        c = decayed(chain, 90.0, "put", 0.3)
        res = desk.manage([c], apply=True)
        assert res.applied[0]["action"] == ACTION_CLOSE and res.applied[0]["reason"] == "take_profit"
        assert res.applied[0]["price"] == c.get(EXPIRY, 90.0, "put").ask
        closed = desk.ledger.get_position(p.id)
        assert closed.status == STATUS_CLOSED and closed.realized_pnl == res.applied[0]["realized_pnl"]

    def test_manage_apply_expire_and_assign(self, desk: Desk, chain: Chain):
        a = desk.sell(chain, EXPIRY, 90.0, "put", 1)["position"]
        b = desk.sell(chain, EXPIRY, 110.0, "call", 1)["position"]
        c = copy.deepcopy(chain)
        c.spot = 120.0
        res = desk.manage([c], apply=True, today=EXPIRY)
        assert res.summary == {ACTION_EXPIRE: 1, ACTION_ASSIGN: 1}
        assert desk.ledger.get_position(a.id).status == STATUS_EXPIRED
        assert desk.ledger.get_position(b.id).status == STATUS_ASSIGNED
        by_id = {x["position_id"]: x for x in res.applied}
        assert by_id[b.id]["price"] == 10.0 and by_id[a.id]["price"] == 0.0

    def test_manage_roll_is_not_executed(self, desk: Desk, chain: Chain):
        p = desk.sell(chain, EXPIRY, 90.0, "put", 1)["position"]
        c = copy.deepcopy(chain)
        c.spot = 88.0
        res = desk.manage([c], ManagementRules(), apply=True)
        assert res.summary == {ACTION_ROLL: 1}
        assert res.applied == [{"position_id": p.id, "action": ACTION_ROLL, "executed": False, "note": "pick a roll candidate and call roll()"}]
        assert desk.ledger.get_position(p.id).status == STATUS_OPEN

    def test_manage_missing_chain(self, desk: Desk, chain: Chain):
        desk.sell(chain, EXPIRY, 90.0, "put", 1)
        res = desk.manage([make_chain("OTHER")], apply=True)
        assert res.summary == {ACTION_NO_DATA: 1} and res.applied == []


class TestJournal:
    def test_stats_journal_export(self, desk: Desk, chain: Chain):
        p = desk.sell(chain, EXPIRY, 90.0, "put", 1)["position"]
        desk.close_position(p.id, price=0.1)
        assert desk.stats()["closed_trades"] == 1
        assert [t.action for t in desk.journal(limit=5)] == ["buy_to_close", "sell_to_open"]
        assert "sell_to_open" in desk.export("trades", "csv")
