"""Black-Scholes pricing, Greeks, implied volatility, and probability helpers.

Pure functions with no side effects. Volatility and rates are annualized
decimals (0.35 = 35%), time is in years, and prices are per share.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from thetadesk.models import CALL, PUT, normalize_right

DAYS_PER_YEAR = 365.0
RISK_FREE_RATE = 0.04
_MIN_SIGMA = 1e-9


def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def norm_pdf(x: float) -> float:
    """Standard normal probability density function."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def years(dte: float) -> float:
    """Convert days-to-expiry into years (never negative)."""
    return max(float(dte), 0.0) / DAYS_PER_YEAR


def days_to_expiry(expiry: date, as_of: date) -> int:
    """Calendar days from ``as_of`` to ``expiry`` (can be negative once expired)."""
    return (expiry - as_of).days


def intrinsic(spot: float, strike: float, right: str) -> float:
    """Intrinsic value per share."""
    if normalize_right(right) == CALL:
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def _d1_d2(spot: float, strike: float, sigma: float, t: float, rate: float) -> tuple[float, float]:
    sigma = max(sigma, _MIN_SIGMA)
    vol_t = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / vol_t
    return d1, d1 - vol_t


def bs_price(
    spot: float,
    strike: float,
    sigma: float,
    t: float,
    right: str,
    rate: float = RISK_FREE_RATE,
) -> float:
    """Black-Scholes price of a European option (no dividends)."""
    r = normalize_right(right)
    if t <= 0:
        return intrinsic(spot, strike, r)
    d1, d2 = _d1_d2(spot, strike, sigma, t, rate)
    disc = math.exp(-rate * t)
    if r == CALL:
        return spot * norm_cdf(d1) - strike * disc * norm_cdf(d2)
    return strike * disc * norm_cdf(-d2) - spot * norm_cdf(-d1)


@dataclass
class Greeks:
    """Option Greeks. Theta is per calendar day; vega and rho are per 1 point (1%)."""

    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


def greeks(
    spot: float,
    strike: float,
    sigma: float,
    t: float,
    right: str,
    rate: float = RISK_FREE_RATE,
) -> Greeks:
    """Black-Scholes Greeks for a long position of one share-equivalent."""
    r = normalize_right(right)
    if t <= 0:
        if r == CALL:
            delta = 1.0 if spot > strike else 0.0
        else:
            delta = -1.0 if spot < strike else 0.0
        return Greeks(delta=delta, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)
    sigma = max(sigma, _MIN_SIGMA)
    d1, d2 = _d1_d2(spot, strike, sigma, t, rate)
    disc = math.exp(-rate * t)
    pdf = norm_pdf(d1)
    sqrt_t = math.sqrt(t)
    gamma = pdf / (spot * sigma * sqrt_t)
    vega = spot * pdf * sqrt_t / 100.0
    common_theta = -spot * pdf * sigma / (2.0 * sqrt_t)
    if r == CALL:
        delta = norm_cdf(d1)
        theta = common_theta - rate * strike * disc * norm_cdf(d2)
        rho = strike * t * disc * norm_cdf(d2) / 100.0
    else:
        delta = norm_cdf(d1) - 1.0
        theta = common_theta + rate * strike * disc * norm_cdf(-d2)
        rho = -strike * t * disc * norm_cdf(-d2) / 100.0
    return Greeks(delta=delta, gamma=gamma, theta=theta / DAYS_PER_YEAR, vega=vega, rho=rho)


def implied_vol(
    price: float,
    spot: float,
    strike: float,
    t: float,
    right: str,
    rate: float = RISK_FREE_RATE,
    lo: float = 1e-4,
    hi: float = 10.0,
    tol: float = 1e-8,
    max_iter: int = 200,
) -> float | None:
    """Implied volatility by bisection; None when no volatility reproduces ``price``."""
    r = normalize_right(right)
    if t <= 0 or price <= 0:
        return None
    f_lo = bs_price(spot, strike, lo, t, r, rate) - price
    f_hi = bs_price(spot, strike, hi, t, r, rate) - price
    if f_lo > 0 or f_hi < 0:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = bs_price(spot, strike, mid, t, r, rate) - price
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if f_mid < 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def prob_above(
    spot: float,
    level: float,
    sigma: float,
    t: float,
    rate: float = RISK_FREE_RATE,
) -> float:
    """Probability that the underlying finishes above ``level`` (lognormal model)."""
    if level <= 0:
        return 1.0
    if t <= 0:
        return 1.0 if spot > level else 0.0
    sigma = max(sigma, _MIN_SIGMA)
    d2 = (math.log(spot / level) + (rate - 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    return norm_cdf(d2)


def prob_itm(
    spot: float,
    strike: float,
    sigma: float,
    t: float,
    right: str,
    rate: float = RISK_FREE_RATE,
) -> float:
    """Probability the option expires in the money."""
    p_above = prob_above(spot, strike, sigma, t, rate)
    return p_above if normalize_right(right) == CALL else 1.0 - p_above


def prob_touch(
    spot: float,
    strike: float,
    sigma: float,
    t: float,
    right: str,
    rate: float = RISK_FREE_RATE,
) -> float:
    """Approximate probability the strike is touched before expiry (~2x ITM probability)."""
    return min(1.0, 2.0 * prob_itm(spot, strike, sigma, t, right, rate))


def breakeven(strike: float, credit: float, right: str) -> float:
    """Breakeven price at expiry for a short option that collected ``credit``."""
    if normalize_right(right) == CALL:
        return strike + credit
    return max(strike - credit, 0.0)


def short_pop(
    spot: float,
    strike: float,
    credit: float,
    sigma: float,
    t: float,
    right: str,
    rate: float = RISK_FREE_RATE,
) -> float:
    """Probability a short option is profitable at expiry (finishes beyond breakeven)."""
    r = normalize_right(right)
    level = breakeven(strike, credit, r)
    p_above = prob_above(spot, level, sigma, t, rate)
    return 1.0 - p_above if r == CALL else p_above


def expected_move(spot: float, sigma: float, t: float) -> float:
    """One standard deviation expected move of the underlying over ``t`` years."""
    return spot * max(sigma, 0.0) * math.sqrt(max(t, 0.0))


__all__ = [
    "CALL",
    "PUT",
    "DAYS_PER_YEAR",
    "RISK_FREE_RATE",
    "Greeks",
    "breakeven",
    "bs_price",
    "days_to_expiry",
    "expected_move",
    "greeks",
    "implied_vol",
    "intrinsic",
    "norm_cdf",
    "norm_pdf",
    "prob_above",
    "prob_itm",
    "prob_touch",
    "short_pop",
    "years",
]
