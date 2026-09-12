"""Tests for thetadesk.rules: management rules and roll candidates."""

from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

from helpers import AS_OF, make_chain
from thetadesk import rules
from thetadesk.models import Chain, InvalidOrderError, OptionQuote, Position
from thetadesk.rules import (
    ACTION_ASSIGN,
    ACTION_CLOSE,
    ACTION_EXPIRE,
    ACTION_HOLD,
    ACTION_NO_DATA,
    ACTION_ROLL,
    ManagementRules,
    evaluate,
    is_tested,
    roll_candidates,
)

EXPIRY = AS_OF + timedelta(days=35)


def make_position(strike: float = 90.0, right: str = "put", credit: float = 1.0, expiry: date = EXPIRY, **kw) -> Position:
    defaults = dict(id=1, underlying="XYZ", right=right, strike=strike, expiry=expiry, contracts=2, credit=credit,
                    collateral=strike * 100, opened_at="2026-09-11T00:00:00+00:00")
    defaults.update(kw)
    return Position(**defaults)


def with_quote(chain: Chain, strike: float, right: str, bid: float, ask: float, expiry: date = EXPIRY, **kw) -> Chain:
    """Copy the chain, replacing one quote's market."""
    c = copy.deepcopy(chain)
    q = c.get(expiry, strike, right)
    q.bid, q.ask = bid, ask
    for k, v in kw.items():
        setattr(q, k, v)
    return c


class TestRules:
    def test_defaults(self):
        r = ManagementRules()
        assert (r.take_profit_pct, r.stop_loss_multiple, r.manage_dte, r.tested_delta) == (0.5, 2.0, 21, 0.4)

    @pytest.mark.parametrize("kwargs", [dict(take_profit_pct=0), dict(take_profit_pct=1.5), dict(stop_loss_multiple=0), dict(fill="last")])
    def test_invalid(self, kwargs):
        with pytest.raises(InvalidOrderError):
            ManagementRules(**kwargs)


class TestIsTested:
    def test_spot_through_strike(self):
        assert is_tested(make_position(90.0, "put"), 89.0, None, 0.4)
        assert not is_tested(make_position(90.0, "put"), 95.0, None, 0.4)
        assert is_tested(make_position(110.0, "call"), 110.0, None, 0.4)
        assert not is_tested(make_position(110.0, "call"), 100.0, None, 0.4)

    def test_delta(self):
        assert is_tested(make_position(90.0, "put"), 95.0, -0.45, 0.4)
        assert not is_tested(make_position(90.0, "put"), 95.0, -0.2, 0.4)


class TestEvaluate:
    def test_no_chain(self):
        ev = evaluate(make_position(), None)
        assert ev.action == ACTION_NO_DATA and ev.reason == "no_chain" and ev.notes
        assert ev.dte == 35 or ev.dte < 35  # uses today when no chain

    def test_wrong_underlying(self, chain):
        ev = evaluate(make_position(underlying="OTHER"), chain)
        assert ev.action == ACTION_NO_DATA and "Chain is for XYZ" in ev.notes[0]

    def test_quote_missing(self, chain):
        ev = evaluate(make_position(strike=91.0), chain)
        assert ev.action == ACTION_NO_DATA and ev.reason == "quote_missing" and ev.spot == 100.0
        assert ev.tested is False

    def test_hold(self, chain):
        q = chain.get(EXPIRY, 90.0, "put")
        ev = evaluate(make_position(credit=q.bid), chain)
        assert ev.action == ACTION_HOLD and ev.reason == "within_rules"
        assert ev.mark == pytest.approx(q.mid) and ev.close_cost == q.ask
        assert ev.unrealized == pytest.approx((q.bid - q.mid) * 200)
        assert ev.pct_of_max_profit < 0 and ev.delta == q.delta and not ev.tested

    def test_take_profit(self, chain):
        c = with_quote(chain, 90.0, "put", 0.40, 0.44)
        ev = evaluate(make_position(credit=1.0), c)
        assert ev.action == ACTION_CLOSE and ev.reason == "take_profit"
        assert ev.pct_of_max_profit == pytest.approx(0.58)
        assert ev.unrealized == pytest.approx(0.58 * 200)

    def test_stop_loss(self, chain):
        c = with_quote(chain, 90.0, "put", 3.0, 3.2)
        ev = evaluate(make_position(credit=1.0), c)
        assert ev.action == ACTION_CLOSE and ev.reason == "stop_loss"

    def test_tested_by_spot_rolls(self, chain):
        c = with_quote(chain, 90.0, "put", 3.0, 3.2)
        c.spot = 89.0
        ev = evaluate(make_position(credit=2.0), c)
        assert ev.action == ACTION_ROLL and ev.reason == "tested" and ev.tested
        assert ev.roll_candidates and all(rc.expiry > EXPIRY for rc in ev.roll_candidates)

    def test_tested_by_delta(self, chain):
        c = with_quote(chain, 90.0, "put", 1.0, 1.1, delta=-0.45)
        ev = evaluate(make_position(credit=1.0), c)
        assert ev.action == ACTION_ROLL and ev.reason == "tested"

    def test_manage_dte_close_when_profitable(self, chain):
        c = with_quote(chain, 90.0, "put", 0.70, 0.74)
        ev = evaluate(make_position(credit=1.0), c, today=EXPIRY - timedelta(days=21))
        assert ev.dte == 21 and ev.action == ACTION_CLOSE and ev.reason == "manage_dte"

    def test_manage_dte_roll_when_losing(self, chain):
        c = with_quote(chain, 90.0, "put", 1.5, 1.6)
        ev = evaluate(make_position(credit=1.0), c, today=EXPIRY - timedelta(days=20))
        assert ev.action == ACTION_ROLL and ev.reason == "manage_dte"

    def test_roll_without_candidates_notes(self, chain):
        c = with_quote(chain, 90.0, "put", 1.5, 1.6)
        c.options = [q for q in c.options if q.expiry <= EXPIRY]
        ev = evaluate(make_position(credit=1.0), c, today=EXPIRY - timedelta(days=20))
        assert ev.action == ACTION_ROLL and ev.roll_candidates == [] and "No roll candidates" in ev.notes[0]

    def test_expired_worthless(self, chain):
        ev = evaluate(make_position(credit=1.0), chain, today=EXPIRY)
        assert ev.action == ACTION_EXPIRE and ev.reason == "expired_worthless"
        assert ev.mark == 0.0 and ev.unrealized == pytest.approx(200.0) and ev.pct_of_max_profit == 1.0

    def test_expired_in_the_money(self, chain):
        c = copy.deepcopy(chain)
        c.spot = 85.0
        ev = evaluate(make_position(credit=1.0), c, today=EXPIRY + timedelta(days=1))
        assert ev.action == ACTION_ASSIGN and ev.reason == "expired_in_the_money" and ev.tested
        assert ev.mark == 5.0 and ev.unrealized == pytest.approx(-800.0)

    def test_expired_zero_credit_pct(self, chain):
        ev = evaluate(make_position(credit=0.0), chain, today=EXPIRY)
        assert ev.pct_of_max_profit == 0.0

    def test_zero_credit_open_position(self, chain):
        ev = evaluate(make_position(credit=0.0), chain)
        assert ev.pct_of_max_profit == 0.0


class TestRollCandidates:
    def test_same_strike_and_same_delta(self, chain):
        pos = make_position(credit=1.0, delta_at_open=-0.25)
        out = roll_candidates(pos, chain, ManagementRules(), close_cost=1.5)
        assert out and len(out) <= ManagementRules().max_roll_candidates
        kinds = {rc.kind for rc in out}
        assert "out" in kinds
        assert all(rc.expiry > EXPIRY for rc in out)
        assert all(rc.dte <= 60 for rc in out)
        credits = [rc.net_credit for rc in out]
        assert credits == sorted(credits, reverse=True)
        same = [rc for rc in out if rc.kind == "out"][0]
        assert same.strike == 90.0 and same.net_credit == pytest.approx(chain.get(same.expiry, 90.0, "put").bid - 1.5)
        assert same.pop is not None and same.delta is not None

    def test_kinds_up_and_down(self, chain):
        out = roll_candidates(make_position(strike=85.0, credit=1.0), chain, ManagementRules(max_roll_candidates=10), close_cost=0.5, target_delta=0.5)
        assert any(rc.kind == "out_and_up" for rc in out)
        out = roll_candidates(make_position(strike=95.0, credit=1.0), chain, ManagementRules(max_roll_candidates=10), close_cost=0.5, target_delta=0.05)
        assert any(rc.kind == "out_and_down" for rc in out)

    def test_window_excludes_too_far_or_too_near(self, chain):
        r = ManagementRules(roll_min_days=40, roll_max_dte=60)
        assert roll_candidates(make_position(credit=1.0), chain, r, close_cost=1.0) == []

    def test_wrong_right_ignored(self, chain):
        c = copy.deepcopy(chain)
        c.options = [q for q in c.options if q.right == "call"]
        assert roll_candidates(make_position(credit=1.0), c, ManagementRules(), close_cost=1.0) == []

    def test_no_greeks_chain(self):
        c = make_chain(with_greeks=False)
        out = roll_candidates(make_position(credit=1.0), c, ManagementRules(), close_cost=1.0)
        assert out and all(rc.delta is not None for rc in out)

    def test_unpriceable_quotes_skip_delta(self):
        c = make_chain(with_greeks=False)
        for q in c.options:
            q.bid = q.ask = 0.0
        out = roll_candidates(make_position(credit=1.0), c, ManagementRules(), close_cost=1.0)
        assert out and all(rc.delta is None and rc.pop is None for rc in out)


class TestHelpers:
    def test_iv_and_delta_helpers(self):
        q = OptionQuote("XYZ", EXPIRY, 90.0, "put", 1.0, 1.2, iv=0.3, delta=-0.2)
        assert rules._iv_for(q, 100.0, 0.1, 0.04) == 0.3
        assert rules._delta_for(q, 100.0, 0.1, 0.04) == -0.2
        q2 = OptionQuote("XYZ", EXPIRY, 90.0, "put", 1.0, 1.2)
        assert rules._iv_for(q2, 100.0, 0.1, 0.04) is not None
        assert rules._delta_for(q2, 100.0, 0.1, 0.04) is not None
        assert rules._delta_for(q2, 100.0, 0.0, 0.04) is None
        q3 = OptionQuote("XYZ", EXPIRY, 90.0, "put", 0.0, 0.0)
        assert rules._iv_for(q3, 100.0, 0.1, 0.04) is None
