"""Tests for thetadesk.scanner: analysis, filters, ranking, sizing."""

from __future__ import annotations

from datetime import timedelta

import pytest

from helpers import AS_OF, make_chain
from thetadesk import scanner
from thetadesk.models import InvalidOrderError, OptionNotFoundError, OptionQuote
from thetadesk.scanner import Candidate, ScanFilters, analyze_option, analyze_quote, scan, scan_report, size_position


class TestScanFilters:
    def test_defaults_and_normalization(self):
        f = ScanFilters(rights=("P", "Call"))
        assert f.rights == ("put", "call")
        assert f.min_dte == 25 and f.max_dte == 50 and f.fill == "bid"

    @pytest.mark.parametrize(
        "kwargs",
        [dict(fill="last"), dict(min_dte=-1), dict(min_dte=40, max_dte=30), dict(min_delta=0.5, max_delta=0.3), dict(max_delta=1.5)],
    )
    def test_invalid(self, kwargs):
        with pytest.raises(InvalidOrderError):
            ScanFilters(**kwargs)


class TestCollateralAndCredit:
    def test_collateral_basis(self):
        assert scanner.collateral_per_contract(95.0, "put") == (9500.0, "cash_secured_put")
        assert scanner.collateral_per_contract(105.0, "call") == (10500.0, "covered_call_notional")

    def test_credit_for(self):
        q = OptionQuote("X", AS_OF, 95.0, "put", 1.0, 1.2)
        assert scanner.credit_for(q, "bid") == 1.0
        assert scanner.credit_for(q, "mid") == pytest.approx(1.1)


class TestAnalyzeQuote:
    def test_put_metrics(self, chain):
        q = chain.get(AS_OF + timedelta(days=35), 90.0, "put")
        c = analyze_quote(chain, q)
        assert isinstance(c, Candidate)
        assert c.dte == 35 and c.credit == q.bid and c.mid == pytest.approx(q.mid)
        assert c.breakeven == pytest.approx(90 - q.bid)
        assert c.otm_pct == pytest.approx(0.10)
        assert c.collateral == 9000.0 and c.collateral_basis == "cash_secured_put"
        assert c.roc == pytest.approx(q.bid * 100 / 9000)
        assert c.annualized_roc == pytest.approx(c.roc * 365 / 35)
        assert c.delta == q.delta and c.iv == 0.40
        assert c.theta_per_day is not None and c.theta_per_day < 0
        assert 0.7 < c.pop < 0.95 and c.prob_itm < 0.3 and c.prob_touch == pytest.approx(2 * c.prob_itm)
        assert c.expected_move > 0 and c.iv_rank == 50.0 and c.id == q.id
        assert c.flags == [] and not c.earnings_in_window
        assert c.score == pytest.approx(c.pop * c.annualized_roc * (1 - min(q.spread_pct, 0.5)))
        assert c.label == "XYZ 2026-10-16 90 put" and c.strategy == "short_put"

    def test_call_metrics_and_mid_fill(self, chain):
        q = chain.get(AS_OF + timedelta(days=35), 110.0, "call")
        c = analyze_quote(chain, q, fill="mid")
        assert c.credit == pytest.approx(q.mid) and c.otm_pct == pytest.approx(0.10)
        assert c.collateral_basis == "covered_call_notional" and c.strategy == "short_call"
        assert c.breakeven == pytest.approx(110 + q.mid)

    def test_iv_and_delta_computed_when_missing(self):
        chain = make_chain(with_greeks=False)
        q = chain.get(AS_OF + timedelta(days=35), 90.0, "put")
        c = analyze_quote(chain, q)
        assert "iv_from_mid" in c.flags and "delta_computed" in c.flags
        assert c.iv == pytest.approx(0.40, abs=0.02)
        assert c.delta is not None and -0.35 < c.delta < -0.05

    def test_no_iv_when_unpriceable(self):
        chain = make_chain(with_greeks=False)
        expiry = AS_OF + timedelta(days=35)
        chain.options = [OptionQuote("XYZ", expiry, 90.0, "put", 0.0, 0.0)]
        c = analyze_quote(chain, chain.options[0])
        assert "no_iv" in c.flags and c.pop is None and c.score == 0.0 and c.delta is None
        chain.options = [OptionQuote("XYZ", AS_OF, 90.0, "put", 1.0, 1.2)]  # expired: t == 0
        c = analyze_quote(chain, chain.options[0])
        assert "no_iv" in c.flags and c.dte == 0 and c.annualized_roc == 0.0

    def test_delta_given_but_no_iv(self):
        chain = make_chain(with_greeks=False)
        expiry = AS_OF + timedelta(days=35)
        chain.options = [OptionQuote("XYZ", expiry, 90.0, "put", 0.0, 0.0, delta=-0.2)]
        c = analyze_quote(chain, chain.options[0])
        assert c.delta == -0.2 and c.theta_per_day is None

    def test_earnings_in_window_halves_score(self):
        expiry = AS_OF + timedelta(days=35)
        plain = make_chain()
        earn = make_chain(earnings_date=AS_OF + timedelta(days=10))
        a = analyze_quote(plain, plain.get(expiry, 90.0, "put"))
        b = analyze_quote(earn, earn.get(expiry, 90.0, "put"))
        assert b.earnings_in_window and "earnings_in_window" in b.flags
        assert b.score == pytest.approx(a.score * 0.5)
        later = make_chain(earnings_date=AS_OF + timedelta(days=40))
        assert not analyze_quote(later, later.get(expiry, 90.0, "put")).earnings_in_window

    def test_invalid_fill(self, chain):
        with pytest.raises(InvalidOrderError):
            analyze_quote(chain, chain.options[0], fill="last")

    def test_analyze_option_lookup(self, chain):
        c = analyze_option(chain, "2026-10-16", 90, "PUT")
        assert c.strike == 90.0 and c.right == "put"
        with pytest.raises(OptionNotFoundError):
            analyze_option(chain, "2026-10-16", 91, "put")


class TestFilters:
    def _cand(self, chain, strike=90.0, right="put", dte=35):
        return analyze_quote(chain, chain.get(AS_OF + timedelta(days=dte), strike, right))

    def test_passes_defaults(self, chain):
        assert scanner.passes_filters(self._cand(chain), ScanFilters())

    def test_each_rejection(self, chain):
        c = self._cand(chain)
        assert not scanner.passes_filters(c, ScanFilters(rights=("call",)))
        assert not scanner.passes_filters(c, ScanFilters(min_dte=40, max_dte=50))
        assert not scanner.passes_filters(self._cand(chain, strike=110.0), ScanFilters())  # ITM put
        assert scanner.passes_filters(self._cand(chain, strike=110.0), ScanFilters(otm_only=False, max_delta=1.0))
        assert not scanner.passes_filters(c, ScanFilters(min_delta=0.01, max_delta=0.05))
        assert not scanner.passes_filters(c, ScanFilters(min_open_interest=10_000))
        assert not scanner.passes_filters(c, ScanFilters(min_volume=10_000))
        assert not scanner.passes_filters(c, ScanFilters(max_spread_pct=0.0))
        assert not scanner.passes_filters(c, ScanFilters(min_credit=50.0))
        assert not scanner.passes_filters(c, ScanFilters(min_pop=0.999))
        c.pop = None
        assert not scanner.passes_filters(c, ScanFilters(min_pop=0.5))
        c.delta = None
        assert not scanner.passes_filters(c, ScanFilters())

    def test_earnings_filter(self):
        earn = make_chain(earnings_date=AS_OF + timedelta(days=10))
        c = self._cand(earn)
        assert not scanner.passes_filters(c, ScanFilters())
        assert scanner.passes_filters(c, ScanFilters(exclude_earnings=False))


class TestScan:
    def test_ranking_and_limit(self, chain):
        out = scan([chain], ScanFilters(), limit=5)
        assert 0 < len(out) <= 5
        assert all(25 <= c.dte <= 50 for c in out)
        assert all(0.10 <= abs(c.delta) <= 0.30 for c in out)
        scores = [c.score for c in out]
        assert scores == sorted(scores, reverse=True)
        assert scan([chain], ScanFilters(), limit=0) == []

    @pytest.mark.parametrize("sort", ["annualized_roc", "pop", "credit"])
    def test_sort_desc(self, chain, sort):
        out = scan([chain], ScanFilters(), limit=50, sort=sort)
        vals = [getattr(c, sort) for c in out]
        assert vals == sorted(vals, reverse=True)

    def test_sort_dte_asc(self, chain):
        out = scan([chain], ScanFilters(min_dte=0, max_dte=100), limit=50, sort="dte")
        dtes = [c.dte for c in out]
        assert dtes == sorted(dtes)

    def test_invalid_sort(self, chain):
        with pytest.raises(InvalidOrderError):
            scan([chain], ScanFilters(), sort="vibes")

    def test_multiple_chains(self):
        a = make_chain("AAA", spot=50.0, strikes=(40, 45, 50, 55, 60))
        b = make_chain("BBB", spot=200.0, strikes=(160, 180, 200, 220, 240), iv=0.8)
        out = scan([a, b], ScanFilters(), limit=50)
        assert {c.underlying for c in out} == {"AAA", "BBB"}

    def test_report(self, chain):
        rep = scan_report([chain], None, limit=3)
        assert rep["evaluated"] == len(chain.options) and rep["returned"] == 3
        assert rep["underlyings"] == ["XYZ"] and rep["as_of"] == AS_OF and rep["skipped_rows"] == 0
        assert isinstance(rep["filters"], ScanFilters) and rep["sort"] == "score"
        empty = scan_report([], ScanFilters())
        assert empty["as_of"] is None and empty["candidates"] == []


class TestSizing:
    def test_budget_limited(self):
        s = size_position(9000.0, 100_000.0, 90_000.0)
        assert s.contracts == 0 or s.contracts == 0  # 5% budget = 5000 < 9000
        assert s.limited_by == "none_fit" and "Consider" in s.note
        s = size_position(4500.0, 100_000.0, 90_000.0)
        assert s.contracts == 1 and s.limited_by == "per_position_limit"
        assert s.total_collateral == 4500.0 and s.pct_of_account == pytest.approx(0.045)
        assert s.max_allocation == 5000.0 and s.note is None

    def test_buying_power_limited(self):
        s = size_position(1000.0, 100_000.0, 2500.0)
        assert s.contracts == 2 and s.limited_by == "buying_power"

    def test_max_contracts(self):
        s = size_position(1000.0, 100_000.0, 100_000.0, max_pct_per_position=0.5, max_contracts=3)
        assert s.contracts == 3 and s.limited_by == "max_contracts"

    def test_zero_account(self):
        s = size_position(1000.0, 0.0, 0.0)
        assert s.contracts == 0 and s.pct_of_account == 0.0

    @pytest.mark.parametrize("kwargs", [dict(collateral=0.0), dict(max_pct_per_position=0.0), dict(max_pct_per_position=1.5)])
    def test_invalid(self, kwargs):
        args = dict(collateral=1000.0, account_value=10_000.0, buying_power=10_000.0)
        args.update(kwargs)
        with pytest.raises(InvalidOrderError):
            size_position(**args)
