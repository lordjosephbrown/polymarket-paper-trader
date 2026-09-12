"""The Desk: orchestration layer used by both the CLI and the MCP server.

It wires the paper ledger, the scanner, and the management rules together.
Chains are always supplied by the caller (a file, or JSON an agent fetched
from its broker connector); the desk never talks to a broker itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from thetadesk import rules as rules_mod
from thetadesk import scanner
from thetadesk.ledger import Ledger
from thetadesk.models import (
    STATUS_OPEN,
    Chain,
    InvalidOrderError,
    Position,
    PositionClosedError,
    parse_date,
)
from thetadesk.rules import (
    ACTION_ASSIGN,
    ACTION_CLOSE,
    ACTION_EXPIRE,
    ACTION_ROLL,
    Evaluation,
    ManagementRules,
)
from thetadesk.scanner import Candidate, ScanFilters, Sizing

DEFAULT_MAX_PCT_PER_POSITION = 0.05


@dataclass
class PositionView:
    """An open position together with its current mark, when a chain is available."""

    position: Position
    evaluation: Evaluation | None = None


@dataclass
class ManageResult:
    evaluations: list[Evaluation] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)


def _chain_map(chains: list[Chain] | None) -> dict[str, Chain]:
    return {c.underlying: c for c in (chains or [])}


class Desk:
    """Paper premium-selling desk backed by a SQLite ledger in ``data_dir``."""

    def __init__(self, data_dir: str | Path) -> None:
        self.ledger = Ledger(data_dir)

    def close(self) -> None:
        self.ledger.close()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def init_account(self, cash: float) -> dict:
        acct = self.ledger.init_account(cash)
        return {"cash": acct.cash, "starting_cash": acct.starting_cash, "created_at": acct.created_at}

    def balance(self) -> dict:
        return self.ledger.balance()

    def reset(self) -> None:
        self.ledger.reset()

    # ------------------------------------------------------------------
    # Research
    # ------------------------------------------------------------------

    def scan(
        self,
        chains: list[Chain],
        filters: ScanFilters | None = None,
        limit: int = 20,
        sort: str = "score",
    ) -> dict:
        return scanner.scan_report(chains, filters, limit=limit, sort=sort)

    def analyze(
        self,
        chain: Chain,
        expiry: date | str,
        strike: float,
        right: str,
        fill: str = scanner.FILL_BID,
        max_pct_per_position: float = DEFAULT_MAX_PCT_PER_POSITION,
        account_value: float | None = None,
        buying_power: float | None = None,
    ) -> dict:
        """Full metrics for one option plus a position size for this account."""
        candidate = scanner.analyze_option(chain, expiry, strike, right, fill=fill)
        sizing = self._size(candidate, max_pct_per_position, account_value, buying_power)
        return {"candidate": candidate, "sizing": sizing}

    def _size(
        self,
        candidate: Candidate,
        max_pct_per_position: float,
        account_value: float | None,
        buying_power: float | None,
    ) -> Sizing | None:
        if account_value is None or buying_power is None:
            if not self.ledger.is_initialized():
                return None
            bal = self.ledger.balance()
            account_value = bal["cash"] if account_value is None else account_value
            buying_power = bal["buying_power"] if buying_power is None else buying_power
        return scanner.size_position(
            candidate.collateral, account_value, buying_power, max_pct_per_position
        )

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    def sell(
        self,
        chain: Chain,
        expiry: date | str,
        strike: float,
        right: str,
        contracts: int,
        fill: str = scanner.FILL_BID,
        price: float | None = None,
        notes: str | None = None,
    ) -> dict:
        """Sell to open ``contracts`` of an option priced from ``chain``."""
        candidate = scanner.analyze_option(chain, expiry, strike, right, fill=fill)
        credit = candidate.credit if price is None else float(price)
        if candidate.dte <= 0:
            raise InvalidOrderError("Cannot sell an option that has already expired")
        collateral, _ = scanner.collateral_per_contract(candidate.strike, candidate.right)
        position = self.ledger.sell_to_open(
            chain.underlying,
            candidate.right,
            candidate.strike,
            candidate.expiry,
            contracts,
            credit,
            collateral,
            spot=chain.spot,
            delta=candidate.delta,
            iv=candidate.iv,
            notes=notes,
        )
        return {"position": position, "candidate": candidate}

    def close_position(
        self,
        position_id: int,
        price: float | None = None,
        chain: Chain | None = None,
        reason: str = "manual",
    ) -> Position:
        """Buy to close at ``price``, or at the chain's ask when a chain is given."""
        if price is None:
            if chain is None:
                raise InvalidOrderError("close needs a price or a chain to price from")
            position = self.ledger.get_position(position_id)
            quote = chain.get(position.expiry, position.strike, position.right)
            price = quote.ask
        return self.ledger.buy_to_close(position_id, price, reason=reason)

    def settle(self, position_id: int, spot: float, today: date | None = None) -> Position:
        return self.ledger.settle(position_id, spot, today=today)

    def roll(
        self,
        position_id: int,
        chain: Chain,
        new_expiry: date | str,
        new_strike: float | None = None,
        fill: str = scanner.FILL_BID,
    ) -> dict:
        """Roll a position to a later expiry (same strike unless ``new_strike``)."""
        position = self.ledger.get_position(position_id)
        if position.status != STATUS_OPEN:
            raise PositionClosedError(position_id, position.status)
        exp = parse_date(new_expiry)
        if exp <= position.expiry:
            raise InvalidOrderError("A roll must move to a later expiry")
        strike = position.strike if new_strike is None else float(new_strike)
        current = chain.get(position.expiry, position.strike, position.right)
        target = scanner.analyze_option(chain, exp, strike, position.right, fill=fill)
        close_price = current.ask
        collateral, _ = scanner.collateral_per_contract(strike, position.right)
        closed, opened = self.ledger.roll(
            position_id,
            close_price,
            strike,
            exp,
            target.credit,
            collateral,
            spot=chain.spot,
            delta=target.delta,
            iv=target.iv,
        )
        return {
            "closed": closed,
            "opened": opened,
            "net_credit": target.credit - close_price,
            "net_credit_total": (target.credit - close_price) * 100 * opened.contracts,
        }

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def positions(
        self,
        chains: list[Chain] | None = None,
        rules: ManagementRules | None = None,
        today: date | None = None,
    ) -> list[PositionView]:
        """Open positions, marked against any chains supplied."""
        by_symbol = _chain_map(chains)
        views: list[PositionView] = []
        for p in self.ledger.open_positions():
            chain = by_symbol.get(p.underlying)
            ev = rules_mod.evaluate(p, chain, rules, today=today) if chain is not None else None
            views.append(PositionView(position=p, evaluation=ev))
        return views

    def manage(
        self,
        chains: list[Chain],
        rules: ManagementRules | None = None,
        apply: bool = False,
        today: date | None = None,
    ) -> ManageResult:
        """Evaluate every open position; optionally execute closes and settlements.

        Rolls are never executed automatically: the evaluation lists roll
        candidates and the caller (human or agent) picks one with ``roll``.
        """
        by_symbol = _chain_map(chains)
        result = ManageResult()
        for p in self.ledger.open_positions():
            chain = by_symbol.get(p.underlying)
            ev = rules_mod.evaluate(p, chain, rules, today=today)
            result.evaluations.append(ev)
            result.summary[ev.action] = result.summary.get(ev.action, 0) + 1
            if not apply or chain is None:
                continue
            as_of = today or chain.as_of
            if ev.action == ACTION_CLOSE and ev.close_cost is not None:
                closed = self.ledger.buy_to_close(p.id, ev.close_cost, reason=ev.reason)
                result.applied.append(
                    {"position_id": p.id, "action": ACTION_CLOSE, "price": ev.close_cost,
                     "realized_pnl": closed.realized_pnl, "reason": ev.reason}
                )
            elif ev.action in (ACTION_EXPIRE, ACTION_ASSIGN):
                settled = self.ledger.settle(p.id, chain.spot, today=as_of)
                result.applied.append(
                    {"position_id": p.id, "action": ev.action, "price": settled.close_price,
                     "realized_pnl": settled.realized_pnl, "reason": settled.close_reason}
                )
            elif ev.action == ACTION_ROLL:
                result.applied.append(
                    {"position_id": p.id, "action": ACTION_ROLL, "executed": False,
                     "note": "pick a roll candidate and call roll()"}
                )
        return result

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        return self.ledger.stats()

    def journal(self, limit: int = 50) -> list:
        return self.ledger.trades(limit=limit)

    def export(self, kind: str = "trades", fmt: str = "json") -> str:
        return self.ledger.export(kind, fmt)


__all__ = ["Desk", "ManageResult", "PositionView", "DEFAULT_MAX_PCT_PER_POSITION"]
