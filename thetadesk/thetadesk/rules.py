"""Management rules for open short options, and roll candidates.

Defaults follow the mechanics most premium sellers use:

* take profit once 50% of the credit has decayed,
* stop out when the loss reaches 2x the credit received,
* manage (close in profit, otherwise roll) at 21 days to expiry,
* roll when the short strike is tested (spot through the strike, or |delta| >= 0.40).

``evaluate`` never mutates anything; it returns an ``Evaluation`` with the
recommended action and, for rolls, ranked roll candidates from the chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from thetadesk import pricing
from thetadesk.models import (
    CALL,
    CONTRACT_MULTIPLIER,
    PUT,
    Chain,
    InvalidOrderError,
    OptionQuote,
    Position,
    today_utc,
)
from thetadesk.scanner import FILLS, FILL_BID, credit_for

ACTION_HOLD = "HOLD"
ACTION_CLOSE = "CLOSE"
ACTION_ROLL = "ROLL"
ACTION_EXPIRE = "EXPIRE"
ACTION_ASSIGN = "ASSIGN"
ACTION_NO_DATA = "NO_DATA"
DEFAULT_ROLL_TARGET_DELTA = 0.25


@dataclass
class ManagementRules:
    """Thresholds for managing open short options."""

    take_profit_pct: float = 0.50
    stop_loss_multiple: float = 2.0
    manage_dte: int = 21
    tested_delta: float = 0.40
    roll_min_days: int = 7
    roll_max_dte: int = 60
    max_roll_candidates: int = 6
    fill: str = FILL_BID
    rate: float = pricing.RISK_FREE_RATE

    def __post_init__(self) -> None:
        if not (0 < self.take_profit_pct <= 1):
            raise InvalidOrderError("take_profit_pct must be in (0, 1]")
        if self.stop_loss_multiple <= 0:
            raise InvalidOrderError("stop_loss_multiple must be positive")
        if self.fill not in FILLS:
            raise InvalidOrderError(f"fill must be one of {FILLS}, got {self.fill!r}")


@dataclass
class RollCandidate:
    """A later-dated option to roll into, with the net credit of the roll."""

    expiry: date
    strike: float
    dte: int
    bid: float
    ask: float
    net_credit: float
    kind: str
    delta: float | None = None
    pop: float | None = None


@dataclass
class Evaluation:
    """Rule evaluation for one open position."""

    position_id: int
    underlying: str
    right: str
    strike: float
    expiry: date
    contracts: int
    dte: int
    credit: float
    action: str
    reason: str
    spot: float | None = None
    mark: float | None = None
    close_cost: float | None = None
    unrealized: float | None = None
    pct_of_max_profit: float | None = None
    delta: float | None = None
    tested: bool = False
    roll_candidates: list[RollCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _iv_for(quote: OptionQuote, spot: float, t: float, rate: float) -> float | None:
    if quote.iv is not None:
        return quote.iv
    if t > 0 and quote.mid > 0:
        return pricing.implied_vol(quote.mid, spot, quote.strike, t, quote.right, rate)
    return None


def _delta_for(quote: OptionQuote, spot: float, t: float, rate: float) -> float | None:
    if quote.delta is not None:
        return quote.delta
    iv = _iv_for(quote, spot, t, rate)
    if iv is None or t <= 0:
        return None
    return pricing.greeks(spot, quote.strike, iv, t, quote.right, rate).delta


def is_tested(position: Position, spot: float, delta: float | None, tested_delta: float) -> bool:
    """True when spot is through the short strike or the option's |delta| is high."""
    if position.right == PUT and spot <= position.strike:
        return True
    if position.right == CALL and spot >= position.strike:
        return True
    return delta is not None and abs(delta) >= tested_delta


def roll_candidates(
    position: Position,
    chain: Chain,
    rules: ManagementRules,
    close_cost: float,
    today: date | None = None,
    target_delta: float | None = None,
) -> list[RollCandidate]:
    """Later expiries to roll into: same strike ("out") and same-delta strikes."""
    as_of = today or chain.as_of
    current_dte = pricing.days_to_expiry(position.expiry, as_of)
    min_dte = max(current_dte, 0) + rules.roll_min_days
    if target_delta is None:
        target_delta = (
            abs(position.delta_at_open) if position.delta_at_open else DEFAULT_ROLL_TARGET_DELTA
        )

    by_expiry: dict[date, list[OptionQuote]] = {}
    for q in chain.options:
        if q.right != position.right or q.expiry <= position.expiry:
            continue
        dte = pricing.days_to_expiry(q.expiry, as_of)
        if dte < min_dte or dte > rules.roll_max_dte:
            continue
        by_expiry.setdefault(q.expiry, []).append(q)

    seen: set[tuple[date, float]] = set()
    out: list[RollCandidate] = []
    for expiry in sorted(by_expiry):
        quotes = by_expiry[expiry]
        dte = pricing.days_to_expiry(expiry, as_of)
        t = pricing.years(dte)
        picks: list[OptionQuote] = []
        same_strike = [q for q in quotes if abs(q.strike - position.strike) < 1e-9]
        picks.extend(same_strike)
        scored = []
        for q in quotes:
            d = _delta_for(q, chain.spot, t, rules.rate)
            if d is not None:
                scored.append((abs(abs(d) - target_delta), q))
        if scored:
            scored.sort(key=lambda item: item[0])
            picks.append(scored[0][1])
        for q in picks:
            key = (q.expiry, q.strike)
            if key in seen:
                continue
            seen.add(key)
            credit = credit_for(q, rules.fill)
            if abs(q.strike - position.strike) < 1e-9:
                kind = "out"
            elif q.strike < position.strike:
                kind = "out_and_down"
            else:
                kind = "out_and_up"
            iv = _iv_for(q, chain.spot, t, rules.rate)
            pop = (
                pricing.short_pop(chain.spot, q.strike, credit, iv, t, q.right, rules.rate)
                if iv is not None
                else None
            )
            out.append(
                RollCandidate(
                    expiry=q.expiry,
                    strike=q.strike,
                    dte=dte,
                    bid=q.bid,
                    ask=q.ask,
                    net_credit=credit - close_cost,
                    kind=kind,
                    delta=_delta_for(q, chain.spot, t, rules.rate),
                    pop=pop,
                )
            )
    out.sort(key=lambda c: c.net_credit, reverse=True)
    return out[: rules.max_roll_candidates]


def evaluate(
    position: Position,
    chain: Chain | None,
    rules: ManagementRules | None = None,
    today: date | None = None,
) -> Evaluation:
    """Apply the management rules to one open position using the latest chain."""
    r = rules or ManagementRules()
    as_of = today or (chain.as_of if chain is not None else today_utc())
    dte = pricing.days_to_expiry(position.expiry, as_of)
    ev = Evaluation(
        position_id=position.id,
        underlying=position.underlying,
        right=position.right,
        strike=position.strike,
        expiry=position.expiry,
        contracts=position.contracts,
        dte=dte,
        credit=position.credit,
        action=ACTION_NO_DATA,
        reason="no_chain",
    )
    if chain is None:
        ev.notes.append("No chain supplied for this underlying; cannot price the position.")
        return ev
    if chain.underlying != position.underlying:
        ev.notes.append(f"Chain is for {chain.underlying}, position is {position.underlying}.")
        return ev

    spot = chain.spot
    ev.spot = spot
    multiplier = CONTRACT_MULTIPLIER * position.contracts

    if dte <= 0:
        value = pricing.intrinsic(spot, position.strike, position.right)
        ev.mark = value
        ev.close_cost = value
        ev.unrealized = (position.credit - value) * multiplier
        ev.pct_of_max_profit = (
            (position.credit - value) / position.credit if position.credit > 0 else 0.0
        )
        ev.tested = value > 0
        if value > 0:
            ev.action, ev.reason = ACTION_ASSIGN, "expired_in_the_money"
        else:
            ev.action, ev.reason = ACTION_EXPIRE, "expired_worthless"
        return ev

    quote = chain.find(position.expiry, position.strike, position.right)
    t = pricing.years(dte)
    if quote is None:
        ev.reason = "quote_missing"
        ev.tested = is_tested(position, spot, None, r.tested_delta)
        ev.notes.append("Chain has no quote for this contract.")
        return ev

    mark = quote.mid
    close_cost = quote.ask
    delta = _delta_for(quote, spot, t, r.rate)
    ev.mark = mark
    ev.close_cost = close_cost
    ev.delta = delta
    ev.unrealized = (position.credit - mark) * multiplier
    pct = (position.credit - mark) / position.credit if position.credit > 0 else 0.0
    ev.pct_of_max_profit = pct
    ev.tested = is_tested(position, spot, delta, r.tested_delta)

    if pct >= r.take_profit_pct:
        ev.action, ev.reason = ACTION_CLOSE, "take_profit"
    elif (mark - position.credit) >= r.stop_loss_multiple * position.credit:
        ev.action, ev.reason = ACTION_CLOSE, "stop_loss"
    elif ev.tested:
        ev.action, ev.reason = ACTION_ROLL, "tested"
    elif dte <= r.manage_dte:
        if ev.unrealized > 0:
            ev.action, ev.reason = ACTION_CLOSE, "manage_dte"
        else:
            ev.action, ev.reason = ACTION_ROLL, "manage_dte"
    else:
        ev.action, ev.reason = ACTION_HOLD, "within_rules"

    if ev.action == ACTION_ROLL:
        ev.roll_candidates = roll_candidates(position, chain, r, close_cost, today=as_of)
        if not ev.roll_candidates:
            ev.notes.append("No roll candidates in the chain; consider closing instead.")
    return ev


__all__ = [
    "ACTION_ASSIGN",
    "ACTION_CLOSE",
    "ACTION_EXPIRE",
    "ACTION_HOLD",
    "ACTION_NO_DATA",
    "ACTION_ROLL",
    "Evaluation",
    "ManagementRules",
    "RollCandidate",
    "evaluate",
    "is_tested",
    "roll_candidates",
]
