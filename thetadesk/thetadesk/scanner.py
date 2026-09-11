"""Opportunity scanning, single-option analysis, and position sizing.

All functions are pure: they take chains (already parsed) and return
dataclasses. The scanner ranks short options — cash-secured puts and short
calls — by a transparent score::

    score = pop * annualized_roc * (1 - min(spread_pct, 0.5))

where ``pop`` is the probability the short option is profitable at expiry,
``annualized_roc`` is credit / collateral (strike x 100, the cash-secured
basis for puts and calls alike) scaled to a year, and the last factor
penalizes wide markets. Candidates with earnings inside the holding
window are excluded by default (or halved in score when included).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from thetadesk import pricing
from thetadesk.models import (
    CALL,
    CONTRACT_MULTIPLIER,
    PUT,
    RIGHTS,
    Chain,
    InvalidOrderError,
    OptionQuote,
    normalize_right,
)

FILL_BID = "bid"
FILL_MID = "mid"
FILLS = (FILL_BID, FILL_MID)
SORT_KEYS = ("score", "annualized_roc", "pop", "credit", "dte")
EARNINGS_SCORE_PENALTY = 0.5


@dataclass
class ScanFilters:
    """Filters and assumptions for the scanner (defaults: 25-50 DTE, 10-30 delta)."""

    rights: tuple[str, ...] = RIGHTS
    min_dte: int = 25
    max_dte: int = 50
    min_delta: float = 0.10
    max_delta: float = 0.30
    min_open_interest: int = 100
    min_volume: int = 0
    max_spread_pct: float = 0.15
    min_credit: float = 0.10
    min_pop: float = 0.0
    otm_only: bool = True
    exclude_earnings: bool = True
    fill: str = FILL_BID
    rate: float = pricing.RISK_FREE_RATE

    def __post_init__(self) -> None:
        self.rights = tuple(normalize_right(r) for r in self.rights)
        if self.fill not in FILLS:
            raise InvalidOrderError(f"fill must be one of {FILLS}, got {self.fill!r}")
        if self.min_dte < 0 or self.max_dte < self.min_dte:
            raise InvalidOrderError("Invalid DTE range")
        if not (0 <= self.min_delta <= self.max_delta <= 1):
            raise InvalidOrderError("Invalid delta range (expected 0 <= min <= max <= 1)")


@dataclass
class Candidate:
    """A short-option candidate with all the numbers a seller looks at."""

    underlying: str
    expiry: date
    strike: float
    right: str
    dte: int
    spot: float
    bid: float
    ask: float
    mid: float
    spread_pct: float
    credit: float
    breakeven: float
    otm_pct: float
    collateral: float
    collateral_basis: str
    roc: float
    annualized_roc: float
    open_interest: int
    volume: int
    earnings_in_window: bool
    score: float = 0.0
    iv: float | None = None
    delta: float | None = None
    theta_per_day: float | None = None
    pop: float | None = None
    prob_itm: float | None = None
    prob_touch: float | None = None
    expected_move: float | None = None
    iv_rank: float | None = None
    id: str | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.underlying} {self.expiry.isoformat()} {self.strike:g} {self.right}"

    @property
    def strategy(self) -> str:
        return "short_put" if self.right == PUT else "short_call"


@dataclass
class Sizing:
    """How many contracts fit the risk budget."""

    contracts: int
    collateral_per_contract: float
    total_collateral: float
    pct_of_account: float
    max_allocation: float
    limited_by: str
    note: str | None = None


def collateral_per_contract(strike: float, right: str) -> tuple[float, str]:
    """Capital reserved for one short contract: strike x 100.

    Puts are cash-secured. Short calls reserve the same notional (the 100
    shares that would cover them) so puts and calls rank and size on equal
    footing. A margin account reserves less for a naked call; sizing by
    notional keeps the risk budget honest instead.
    """
    basis = "cash_secured_put" if normalize_right(right) == PUT else "covered_call_notional"
    return strike * CONTRACT_MULTIPLIER, basis


def credit_for(quote: OptionQuote, fill: str) -> float:
    """Credit a seller can expect: the bid (default) or the mid."""
    if fill == FILL_MID:
        return quote.mid
    return quote.bid


def _resolve_iv(quote: OptionQuote, spot: float, t: float, rate: float, flags: list[str]) -> float | None:
    if quote.iv is not None:
        return quote.iv
    if t > 0 and quote.mid > 0:
        iv = pricing.implied_vol(quote.mid, spot, quote.strike, t, quote.right, rate)
        if iv is not None:
            flags.append("iv_from_mid")
            return iv
    flags.append("no_iv")
    return None


def _resolve_delta(
    quote: OptionQuote, spot: float, iv: float | None, t: float, rate: float, flags: list[str]
) -> tuple[float | None, float | None]:
    """Return (delta, theta_per_day), computing from IV when the chain has no delta."""
    theta = None
    if iv is not None and t > 0:
        g = pricing.greeks(spot, quote.strike, iv, t, quote.right, rate)
        theta = g.theta
        if quote.delta is None:
            flags.append("delta_computed")
            return g.delta, theta
    return quote.delta, theta


def analyze_quote(
    chain: Chain,
    quote: OptionQuote,
    fill: str = FILL_BID,
    rate: float = pricing.RISK_FREE_RATE,
) -> Candidate:
    """Compute every metric for selling one option from ``chain``."""
    if fill not in FILLS:
        raise InvalidOrderError(f"fill must be one of {FILLS}, got {fill!r}")
    flags: list[str] = []
    spot = chain.spot
    dte = pricing.days_to_expiry(quote.expiry, chain.as_of)
    t = pricing.years(dte)
    credit = credit_for(quote, fill)
    iv = _resolve_iv(quote, spot, t, rate, flags)
    delta, theta = _resolve_delta(quote, spot, iv, t, rate, flags)

    pop = prob_itm = prob_touch = exp_move = None
    if iv is not None:
        pop = pricing.short_pop(spot, quote.strike, credit, iv, t, quote.right, rate)
        prob_itm = pricing.prob_itm(spot, quote.strike, iv, t, quote.right, rate)
        prob_touch = pricing.prob_touch(spot, quote.strike, iv, t, quote.right, rate)
        exp_move = pricing.expected_move(spot, iv, t)

    collateral, basis = collateral_per_contract(quote.strike, quote.right)
    roc = (credit * CONTRACT_MULTIPLIER / collateral) if collateral > 0 else 0.0
    annualized = roc * pricing.DAYS_PER_YEAR / dte if dte > 0 else 0.0
    if quote.right == PUT:
        otm_pct = (spot - quote.strike) / spot
    else:
        otm_pct = (quote.strike - spot) / spot

    earnings_in_window = (
        chain.earnings_date is not None and chain.as_of <= chain.earnings_date <= quote.expiry
    )
    if earnings_in_window:
        flags.append("earnings_in_window")

    score = 0.0
    if pop is not None and annualized > 0:
        score = pop * annualized * (1.0 - min(quote.spread_pct, 0.5))
        if earnings_in_window:
            score *= EARNINGS_SCORE_PENALTY

    return Candidate(
        underlying=chain.underlying,
        expiry=quote.expiry,
        strike=quote.strike,
        right=quote.right,
        dte=dte,
        spot=spot,
        bid=quote.bid,
        ask=quote.ask,
        mid=quote.mid,
        spread_pct=quote.spread_pct,
        credit=credit,
        breakeven=pricing.breakeven(quote.strike, credit, quote.right),
        otm_pct=otm_pct,
        collateral=collateral,
        collateral_basis=basis,
        roc=roc,
        annualized_roc=annualized,
        open_interest=quote.open_interest,
        volume=quote.volume,
        earnings_in_window=earnings_in_window,
        score=score,
        iv=iv,
        delta=delta,
        theta_per_day=theta,
        pop=pop,
        prob_itm=prob_itm,
        prob_touch=prob_touch,
        expected_move=exp_move,
        iv_rank=chain.iv_rank,
        id=quote.id,
        flags=flags,
    )


def analyze_option(
    chain: Chain,
    expiry: date | str,
    strike: float,
    right: str,
    fill: str = FILL_BID,
    rate: float = pricing.RISK_FREE_RATE,
) -> Candidate:
    """Analyze one option identified by expiry/strike/right (raises OptionNotFoundError)."""
    quote = chain.get(expiry, strike, right)
    return analyze_quote(chain, quote, fill=fill, rate=rate)


def passes_filters(c: Candidate, f: ScanFilters) -> bool:
    """True when a candidate satisfies every filter."""
    if c.right not in f.rights:
        return False
    if not (f.min_dte <= c.dte <= f.max_dte):
        return False
    if f.otm_only and c.otm_pct <= 0:
        return False
    if c.delta is None or not (f.min_delta <= abs(c.delta) <= f.max_delta):
        return False
    if c.open_interest < f.min_open_interest or c.volume < f.min_volume:
        return False
    if c.spread_pct > f.max_spread_pct:
        return False
    if c.credit < f.min_credit:
        return False
    if f.min_pop > 0 and (c.pop is None or c.pop < f.min_pop):
        return False
    if f.exclude_earnings and c.earnings_in_window:
        return False
    return True


def _sort_key(sort: str):
    if sort not in SORT_KEYS:
        raise InvalidOrderError(f"sort must be one of {SORT_KEYS}, got {sort!r}")
    if sort == "dte":
        return lambda c: c.dte, False
    return lambda c: (getattr(c, sort) or 0.0), True


def scan(
    chains: list[Chain],
    filters: ScanFilters | None = None,
    limit: int = 20,
    sort: str = "score",
) -> list[Candidate]:
    """Rank every option across ``chains`` that passes ``filters``."""
    f = filters or ScanFilters()
    key, reverse = _sort_key(sort)
    out: list[Candidate] = []
    for chain in chains:
        for quote in chain.options:
            c = analyze_quote(chain, quote, fill=f.fill, rate=f.rate)
            if passes_filters(c, f):
                out.append(c)
    out.sort(key=key, reverse=reverse)
    return out[: max(limit, 0)]


def scan_report(
    chains: list[Chain],
    filters: ScanFilters | None = None,
    limit: int = 20,
    sort: str = "score",
) -> dict:
    """Scan and return candidates plus a summary of what was evaluated."""
    f = filters or ScanFilters()
    candidates = scan(chains, f, limit=limit, sort=sort)
    evaluated = sum(len(c.options) for c in chains)
    return {
        "as_of": max((c.as_of for c in chains), default=None),
        "underlyings": [c.underlying for c in chains],
        "evaluated": evaluated,
        "skipped_rows": sum(c.skipped for c in chains),
        "returned": len(candidates),
        "sort": sort,
        "filters": f,
        "candidates": candidates,
    }


def size_position(
    collateral: float,
    account_value: float,
    buying_power: float,
    max_pct_per_position: float = 0.05,
    max_contracts: int | None = None,
) -> Sizing:
    """Contracts that fit both the per-position budget and available buying power."""
    if collateral <= 0:
        raise InvalidOrderError("collateral must be positive")
    if not (0 < max_pct_per_position <= 1):
        raise InvalidOrderError("max_pct_per_position must be in (0, 1]")
    max_allocation = max(account_value, 0.0) * max_pct_per_position
    by_budget = math.floor(max_allocation / collateral)
    by_bp = math.floor(max(buying_power, 0.0) / collateral)
    contracts = min(by_budget, by_bp)
    limited_by = "per_position_limit" if by_budget <= by_bp else "buying_power"
    if max_contracts is not None and contracts > max_contracts:
        contracts = max_contracts
        limited_by = "max_contracts"
    note = None
    if contracts <= 0:
        contracts = 0
        note = (
            f"One contract needs ${collateral:,.2f} of collateral; the per-position "
            f"budget is ${max_allocation:,.2f} and buying power is ${max(buying_power, 0.0):,.2f}. "
            "Consider a smaller underlying, a defined-risk spread, or a bigger budget."
        )
    total = contracts * collateral
    pct = total / account_value if account_value > 0 else 0.0
    return Sizing(
        contracts=contracts,
        collateral_per_contract=collateral,
        total_collateral=total,
        pct_of_account=pct,
        max_allocation=max_allocation,
        limited_by=limited_by if contracts > 0 else "none_fit",
        note=note,
    )


__all__ = [
    "CALL",
    "PUT",
    "Candidate",
    "FILLS",
    "SORT_KEYS",
    "ScanFilters",
    "Sizing",
    "analyze_option",
    "analyze_quote",
    "collateral_per_contract",
    "credit_for",
    "passes_filters",
    "scan",
    "scan_report",
    "size_position",
]
