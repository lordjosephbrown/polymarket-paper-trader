"""Tests for thetadesk.pricing against textbook Black-Scholes values."""

from __future__ import annotations

from datetime import date

import pytest

from thetadesk import pricing
from thetadesk.models import InvalidChainError


class TestDistribution:
    def test_norm_cdf_pdf(self):
        assert pricing.norm_cdf(0.0) == pytest.approx(0.5)
        assert pricing.norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)
        assert pricing.norm_cdf(-10) == pytest.approx(0.0, abs=1e-12)
        assert pricing.norm_pdf(0.0) == pytest.approx(0.39894, abs=1e-5)

    def test_time_helpers(self):
        assert pricing.years(365) == pytest.approx(1.0)
        assert pricing.years(-5) == 0.0
        assert pricing.days_to_expiry(date(2026, 10, 16), date(2026, 9, 11)) == 35
        assert pricing.days_to_expiry(date(2026, 9, 1), date(2026, 9, 11)) == -10


class TestPricing:
    def test_textbook_values(self):
        # S=100, K=100, sigma=20%, T=1y, r=5%: call 10.4506, put 5.5735
        assert pricing.bs_price(100, 100, 0.2, 1.0, "call", 0.05) == pytest.approx(10.4506, abs=1e-4)
        assert pricing.bs_price(100, 100, 0.2, 1.0, "put", 0.05) == pytest.approx(5.5735, abs=1e-4)

    def test_put_call_parity(self):
        c = pricing.bs_price(100, 95, 0.3, 0.5, "call", 0.04)
        p = pricing.bs_price(100, 95, 0.3, 0.5, "put", 0.04)
        import math
        assert c - p == pytest.approx(100 - 95 * math.exp(-0.04 * 0.5), abs=1e-9)

    def test_expired_is_intrinsic(self):
        assert pricing.bs_price(100, 90, 0.2, 0.0, "call") == 10.0
        assert pricing.bs_price(100, 90, 0.2, -1.0, "put") == 0.0
        assert pricing.intrinsic(80, 90, "put") == 10.0
        assert pricing.intrinsic(80, 90, "call") == 0.0

    def test_zero_vol_is_handled(self):
        import math
        forward_intrinsic = 100 - 90 * math.exp(-0.04 * 0.5)
        assert pricing.bs_price(100, 90, 0.0, 0.5, "call") == pytest.approx(forward_intrinsic, abs=1e-6)
        assert pricing.bs_price(100, 110, 0.0, 0.5, "call") == pytest.approx(0.0, abs=1e-9)

    def test_invalid_right(self):
        with pytest.raises(InvalidChainError):
            pricing.bs_price(100, 100, 0.2, 1.0, "swap")


class TestGreeks:
    def test_textbook_values(self):
        g = pricing.greeks(100, 100, 0.2, 1.0, "call", 0.05)
        assert g.delta == pytest.approx(0.6368, abs=1e-4)
        assert g.gamma == pytest.approx(0.0188, abs=1e-4)
        assert g.theta == pytest.approx(-6.414 / 365, abs=1e-4)
        assert g.vega == pytest.approx(0.3752, abs=1e-4)
        assert g.rho == pytest.approx(0.5323, abs=1e-4)
        p = pricing.greeks(100, 100, 0.2, 1.0, "put", 0.05)
        assert p.delta == pytest.approx(g.delta - 1.0)
        assert p.gamma == pytest.approx(g.gamma) and p.vega == pytest.approx(g.vega)
        assert p.rho < 0 and p.theta < 0

    @pytest.mark.parametrize(
        "spot, strike, right, delta",
        [(110, 100, "call", 1.0), (90, 100, "call", 0.0), (90, 100, "put", -1.0), (110, 100, "put", 0.0)],
    )
    def test_expired_greeks(self, spot, strike, right, delta):
        g = pricing.greeks(spot, strike, 0.2, 0.0, right)
        assert g.delta == delta and g.gamma == 0.0 and g.theta == 0.0 and g.vega == 0.0


class TestImpliedVol:
    def test_roundtrip(self):
        for sigma in (0.15, 0.4, 1.2):
            for right in ("put", "call"):
                price = pricing.bs_price(100, 95, sigma, 0.25, right, 0.04)
                assert pricing.implied_vol(price, 100, 95, 0.25, right, 0.04) == pytest.approx(sigma, abs=1e-5)

    def test_out_of_bounds(self):
        assert pricing.implied_vol(0.0, 100, 95, 0.25, "put") is None
        assert pricing.implied_vol(1.0, 100, 95, 0.0, "put") is None
        assert pricing.implied_vol(0.0001, 100, 100, 0.25, "call") is None  # below any model price
        assert pricing.implied_vol(150.0, 100, 100, 0.25, "call") is None  # above any model price

    def test_max_iter_exit(self):
        price = pricing.bs_price(100, 100, 0.3, 0.25, "call")
        assert pricing.implied_vol(price, 100, 100, 0.25, "call", max_iter=0) == pytest.approx(5.00005, abs=1e-3)


class TestProbabilities:
    def test_prob_above_edges(self):
        assert pricing.prob_above(100, 0, 0.3, 0.1) == 1.0
        assert pricing.prob_above(100, 90, 0.3, 0.0) == 1.0
        assert pricing.prob_above(100, 110, 0.3, 0.0) == 0.0
        assert 0.4 < pricing.prob_above(100, 100, 0.3, 0.1, rate=0.0) < 0.5

    def test_itm_touch_pop(self):
        t = 30 / 365
        put_itm = pricing.prob_itm(100, 90, 0.3, t, "put")
        call_itm = pricing.prob_itm(100, 110, 0.3, t, "call")
        assert 0.05 < put_itm < 0.2 and 0.05 < call_itm < 0.2
        assert pricing.prob_touch(100, 90, 0.3, t, "put") == pytest.approx(2 * put_itm)
        assert pricing.prob_touch(100, 100, 0.3, t, "put") == 1.0
        pop_put = pricing.short_pop(100, 90, 1.5, 0.3, t, "put")
        pop_call = pricing.short_pop(100, 110, 1.5, 0.3, t, "call")
        assert pop_put > 1 - put_itm and pop_call > 1 - call_itm
        assert pricing.short_pop(100, 90, 0.0, 0.3, t, "put") == pytest.approx(1 - put_itm)

    def test_breakeven_and_expected_move(self):
        assert pricing.breakeven(90, 1.5, "put") == 88.5
        assert pricing.breakeven(1, 5, "put") == 0.0
        assert pricing.breakeven(110, 1.5, "call") == 111.5
        assert pricing.expected_move(100, 0.3, 30 / 365) == pytest.approx(8.6007, abs=1e-3)
        assert pricing.expected_move(100, -0.3, 1.0) == 0.0
