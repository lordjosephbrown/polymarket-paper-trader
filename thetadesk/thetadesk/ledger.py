"""SQLite paper ledger: cash, collateral, short-option positions, trade journal, stats.

Cash accounting for short options:

* sell to open   -> cash += credit x 100 x contracts; collateral is reserved
* buy to close   -> cash -= price  x 100 x contracts; collateral released
* expire         -> nothing changes; collateral released
* assignment     -> cash -= intrinsic x 100 x contracts (cash-settled at expiry)

Buying power is cash minus the collateral reserved by open positions.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from thetadesk.models import (
    CONTRACT_MULTIPLIER,
    STATUS_ASSIGNED,
    STATUS_CLOSED,
    STATUS_EXPIRED,
    STATUS_OPEN,
    Account,
    InsufficientBuyingPowerError,
    InvalidOrderError,
    NotInitializedError,
    Position,
    PositionClosedError,
    PositionNotFoundError,
    TradeRecord,
    normalize_right,
    parse_date,
    to_jsonable,
    utc_now,
)
from thetadesk.pricing import intrinsic

ACTION_SELL_TO_OPEN = "sell_to_open"
ACTION_BUY_TO_CLOSE = "buy_to_close"
ACTION_EXPIRE = "expire"
ACTION_ASSIGN = "assign"
CLOSED_STATUSES = (STATUS_CLOSED, STATUS_EXPIRED, STATUS_ASSIGNED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL,
    starting_cash REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    underlying TEXT NOT NULL,
    right TEXT NOT NULL,
    strike REAL NOT NULL,
    expiry TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    credit REAL NOT NULL,
    collateral REAL NOT NULL,
    opened_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    spot_at_open REAL,
    delta_at_open REAL,
    iv_at_open REAL,
    close_price REAL,
    closed_at TEXT,
    close_reason TEXT,
    realized_pnl REAL,
    rolled_from INTEGER,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    price REAL NOT NULL,
    contracts INTEGER NOT NULL,
    cash_delta REAL NOT NULL,
    ts TEXT NOT NULL,
    note TEXT
);
"""


def _ts(value: datetime | None) -> str:
    return (value or utc_now()).isoformat()


def _row_to_position(row: sqlite3.Row) -> Position:
    return Position(
        id=row["id"],
        underlying=row["underlying"],
        right=row["right"],
        strike=row["strike"],
        expiry=parse_date(row["expiry"]),
        contracts=row["contracts"],
        credit=row["credit"],
        collateral=row["collateral"],
        opened_at=row["opened_at"],
        status=row["status"],
        spot_at_open=row["spot_at_open"],
        delta_at_open=row["delta_at_open"],
        iv_at_open=row["iv_at_open"],
        close_price=row["close_price"],
        closed_at=row["closed_at"],
        close_reason=row["close_reason"],
        realized_pnl=row["realized_pnl"],
        rolled_from=row["rolled_from"],
        notes=row["notes"],
    )


def _row_to_trade(row: sqlite3.Row) -> TradeRecord:
    return TradeRecord(
        id=row["id"],
        position_id=row["position_id"],
        action=row["action"],
        price=row["price"],
        contracts=row["contracts"],
        cash_delta=row["cash_delta"],
        ts=row["ts"],
        note=row["note"],
    )


def _days_between(start: str, end: str) -> float:
    a = datetime.fromisoformat(start)
    b = datetime.fromisoformat(end)
    return (b - a).total_seconds() / 86400.0


class Ledger:
    """Paper account stored in ``<data_dir>/paper.db``."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "paper.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def init_account(self, cash: float, ts: datetime | None = None) -> Account:
        """Create (or reset) the account with ``cash`` dollars."""
        if cash <= 0:
            raise InvalidOrderError("Starting cash must be positive")
        created = _ts(ts)
        with self.conn:
            self.conn.execute("DELETE FROM trades")
            self.conn.execute("DELETE FROM positions")
            self.conn.execute("DELETE FROM account")
            self.conn.execute(
                "INSERT INTO account (id, cash, starting_cash, created_at) VALUES (1, ?, ?, ?)",
                (float(cash), float(cash), created),
            )
        return Account(cash=float(cash), starting_cash=float(cash), created_at=created)

    def is_initialized(self) -> bool:
        row = self.conn.execute("SELECT 1 FROM account WHERE id = 1").fetchone()
        return row is not None

    def account(self) -> Account:
        row = self.conn.execute(
            "SELECT cash, starting_cash, created_at FROM account WHERE id = 1"
        ).fetchone()
        if row is None:
            raise NotInitializedError()
        return Account(cash=row["cash"], starting_cash=row["starting_cash"], created_at=row["created_at"])

    def reset(self) -> None:
        """Delete everything, including the account."""
        with self.conn:
            self.conn.execute("DELETE FROM trades")
            self.conn.execute("DELETE FROM positions")
            self.conn.execute("DELETE FROM account")

    def collateral_in_use(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(collateral * contracts), 0) AS c FROM positions WHERE status = ?",
            (STATUS_OPEN,),
        ).fetchone()
        return float(row["c"])

    def buying_power(self) -> float:
        return self.account().cash - self.collateral_in_use()

    def balance(self) -> dict:
        """Cash, reserved collateral, buying power, and realized P&L."""
        acct = self.account()
        open_positions = self.positions(STATUS_OPEN)
        collateral = sum(p.total_collateral for p in open_positions)
        premium_open = sum(p.premium_received for p in open_positions)
        realized = self._realized_total()
        return {
            "cash": acct.cash,
            "starting_cash": acct.starting_cash,
            "collateral_in_use": collateral,
            "buying_power": acct.cash - collateral,
            "open_positions": len(open_positions),
            "premium_at_risk": premium_open,
            "realized_pnl": realized,
            "net_liq_if_closed_at_entry": acct.cash - premium_open,
            "return_pct": realized / acct.starting_cash if acct.starting_cash else 0.0,
        }

    def _realized_total(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS r FROM positions WHERE status != ?",
            (STATUS_OPEN,),
        ).fetchone()
        return float(row["r"])

    def _adjust_cash(self, delta: float) -> None:
        self.conn.execute("UPDATE account SET cash = cash + ? WHERE id = 1", (delta,))

    def _record_trade(
        self,
        position_id: int,
        action: str,
        price: float,
        contracts: int,
        cash_delta: float,
        ts: str,
        note: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO trades (position_id, action, price, contracts, cash_delta, ts, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (position_id, action, price, contracts, cash_delta, ts, note),
        )

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def sell_to_open(
        self,
        underlying: str,
        right: str,
        strike: float,
        expiry: date | str,
        contracts: int,
        credit: float,
        collateral: float,
        spot: float | None = None,
        delta: float | None = None,
        iv: float | None = None,
        notes: str | None = None,
        rolled_from: int | None = None,
        ts: datetime | None = None,
    ) -> Position:
        """Sell ``contracts`` of an option, collecting ``credit`` per share."""
        acct = self.account()
        right = normalize_right(right)
        exp = parse_date(expiry)
        if contracts <= 0:
            raise InvalidOrderError("contracts must be a positive integer")
        if credit <= 0:
            raise InvalidOrderError("credit must be positive (no bid to sell into)")
        if strike <= 0 or collateral <= 0:
            raise InvalidOrderError("strike and collateral must be positive")
        required = collateral * contracts
        available = acct.cash - self.collateral_in_use()
        if required > available + 1e-9:
            raise InsufficientBuyingPowerError(required, available)
        when = _ts(ts)
        premium = round(credit * CONTRACT_MULTIPLIER * contracts, 2)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO positions (underlying, right, strike, expiry, contracts, credit,"
                " collateral, opened_at, status, spot_at_open, delta_at_open, iv_at_open,"
                " rolled_from, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    underlying.upper(),
                    right,
                    float(strike),
                    exp.isoformat(),
                    int(contracts),
                    float(credit),
                    float(collateral),
                    when,
                    STATUS_OPEN,
                    spot,
                    delta,
                    iv,
                    rolled_from,
                    notes,
                ),
            )
            position_id = int(cur.lastrowid)
            self._adjust_cash(premium)
            self._record_trade(
                position_id, ACTION_SELL_TO_OPEN, float(credit), int(contracts), premium, when, notes
            )
        return self.get_position(position_id)

    def _close(
        self,
        position: Position,
        price: float,
        status: str,
        reason: str,
        action: str,
        ts: str,
        note: str | None = None,
    ) -> Position:
        cost = round(price * CONTRACT_MULTIPLIER * position.contracts, 2)
        realized = round((position.credit - price) * CONTRACT_MULTIPLIER * position.contracts, 2)
        with self.conn:
            self.conn.execute(
                "UPDATE positions SET status = ?, close_price = ?, closed_at = ?, close_reason = ?,"
                " realized_pnl = ? WHERE id = ?",
                (status, float(price), ts, reason, realized, position.id),
            )
            if cost:
                self._adjust_cash(-cost)
            self._record_trade(position.id, action, float(price), position.contracts, -cost, ts, note)
        return self.get_position(position.id)

    def buy_to_close(
        self,
        position_id: int,
        price: float,
        reason: str = "manual",
        ts: datetime | None = None,
        note: str | None = None,
    ) -> Position:
        """Buy back an open position at ``price`` per share."""
        position = self._open_position(position_id)
        if price < 0:
            raise InvalidOrderError("close price cannot be negative")
        return self._close(
            position, price, STATUS_CLOSED, reason, ACTION_BUY_TO_CLOSE, _ts(ts), note
        )

    def settle(
        self,
        position_id: int,
        spot: float,
        ts: datetime | None = None,
        today: date | None = None,
    ) -> Position:
        """Settle an expired position: worthless expiry, or cash-settled assignment.

        ``today`` is the date used to decide whether the option has expired
        (defaults to the date of ``ts``); ``ts`` stamps the journal entry.
        """
        position = self._open_position(position_id)
        when = ts or utc_now()
        if position.expiry > (today or when.date()):
            raise InvalidOrderError(
                f"Position {position_id} expires {position.expiry.isoformat()}; not expired yet"
            )
        value = intrinsic(spot, position.strike, position.right)
        if value > 0:
            return self._close(
                position,
                value,
                STATUS_ASSIGNED,
                "assigned",
                ACTION_ASSIGN,
                when.isoformat(),
                f"cash-settled at intrinsic with spot {spot:g}",
            )
        return self._close(
            position, 0.0, STATUS_EXPIRED, "expired_worthless", ACTION_EXPIRE, when.isoformat()
        )

    def roll(
        self,
        position_id: int,
        close_price: float,
        new_strike: float,
        new_expiry: date | str,
        new_credit: float,
        new_collateral: float,
        spot: float | None = None,
        delta: float | None = None,
        iv: float | None = None,
        ts: datetime | None = None,
        notes: str | None = None,
    ) -> tuple[Position, Position]:
        """Close a position and open a later-dated one in a single step."""
        old = self._open_position(position_id)
        if close_price < 0 or new_credit <= 0 or new_collateral <= 0:
            raise InvalidOrderError("roll needs close_price >= 0, new_credit > 0, new_collateral > 0")
        when = ts or utc_now()
        # Check buying power as it will be after the close, before changing anything.
        cash_after_close = self.account().cash - close_price * CONTRACT_MULTIPLIER * old.contracts
        collateral_after_close = self.collateral_in_use() - old.total_collateral
        required = new_collateral * old.contracts
        available = cash_after_close - collateral_after_close
        if required > available + 1e-9:
            raise InsufficientBuyingPowerError(required, available)
        closed = self.buy_to_close(position_id, close_price, reason="roll", ts=when)
        opened = self.sell_to_open(
            old.underlying,
            old.right,
            new_strike,
            new_expiry,
            old.contracts,
            new_credit,
            new_collateral,
            spot=spot,
            delta=delta,
            iv=iv,
            notes=notes or f"rolled from #{old.id}",
            rolled_from=old.id,
            ts=when,
        )
        return closed, opened

    def _open_position(self, position_id: int) -> Position:
        position = self.get_position(position_id)
        if position.status != STATUS_OPEN:
            raise PositionClosedError(position_id, position.status)
        return position

    def get_position(self, position_id: int) -> Position:
        row = self.conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
        if row is None:
            raise PositionNotFoundError(position_id)
        return _row_to_position(row)

    def positions(self, status: str | None = None, underlying: str | None = None) -> list[Position]:
        """Positions, newest first; filter by status and/or underlying."""
        sql = "SELECT * FROM positions"
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if underlying is not None:
            clauses.append("underlying = ?")
            params.append(underlying.upper())
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        return [_row_to_position(r) for r in self.conn.execute(sql, params).fetchall()]

    def open_positions(self) -> list[Position]:
        return self.positions(STATUS_OPEN)

    def closed_positions(self) -> list[Position]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status != ? ORDER BY closed_at ASC, id ASC",
            (STATUS_OPEN,),
        ).fetchall()
        return [_row_to_position(r) for r in rows]

    def trades(self, limit: int = 50, position_id: int | None = None) -> list[TradeRecord]:
        """Trade journal, newest first."""
        if position_id is None:
            rows = self.conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (max(limit, 0),)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE position_id = ? ORDER BY id DESC LIMIT ?",
                (position_id, max(limit, 0)),
            ).fetchall()
        return [_row_to_trade(r) for r in rows]

    # ------------------------------------------------------------------
    # Stats & export
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Journal statistics: win rate, P&L, drawdown, and breakdowns."""
        acct = self.account()
        closed = self.closed_positions()
        open_positions = self.open_positions()
        realized = [p.realized_pnl or 0.0 for p in closed]
        wins = [x for x in realized if x > 0]
        losses = [x for x in realized if x < 0]
        gross_profit = sum(wins)
        gross_loss = -sum(losses)
        total = sum(realized)

        peak = 0.0
        cumulative = 0.0
        max_dd = 0.0
        for x in realized:
            cumulative += x
            peak = max(peak, cumulative)
            max_dd = max(max_dd, peak - cumulative)

        days = [
            _days_between(p.opened_at, p.closed_at) for p in closed if p.closed_at is not None
        ]
        premium_all = sum(p.premium_received for p in closed + open_positions)

        def _bucket(items: list[Position]) -> dict:
            pnl = [p.realized_pnl or 0.0 for p in items]
            n = len(items)
            w = sum(1 for x in pnl if x > 0)
            return {
                "trades": n,
                "wins": w,
                "win_rate": (w / n) if n else 0.0,
                "realized_pnl": sum(pnl),
                "avg_pnl": (sum(pnl) / n) if n else 0.0,
            }

        by_underlying: dict[str, dict] = {}
        for sym in sorted({p.underlying for p in closed}):
            by_underlying[sym] = _bucket([p for p in closed if p.underlying == sym])
        by_strategy: dict[str, dict] = {}
        for strat in sorted({p.strategy for p in closed}):
            by_strategy[strat] = _bucket([p for p in closed if p.strategy == strat])
        by_reason: dict[str, int] = {}
        for p in closed:
            by_reason[p.close_reason or "unknown"] = by_reason.get(p.close_reason or "unknown", 0) + 1

        n = len(closed)
        return {
            "starting_cash": acct.starting_cash,
            "cash": acct.cash,
            "closed_trades": n,
            "open_trades": len(open_positions),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / n) if n else 0.0,
            "realized_pnl": total,
            "return_pct": (total / acct.starting_cash) if acct.starting_cash else 0.0,
            "avg_pnl": (total / n) if n else 0.0,
            "avg_credit": (sum(p.credit for p in closed) / n) if n else 0.0,
            "avg_days_held": (sum(days) / len(days)) if days else 0.0,
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
            "largest_win": max(wins) if wins else 0.0,
            "largest_loss": min(losses) if losses else 0.0,
            "max_drawdown": max_dd,
            "premium_collected": premium_all,
            "collateral_in_use": sum(p.total_collateral for p in open_positions),
            "by_underlying": by_underlying,
            "by_strategy": by_strategy,
            "by_close_reason": by_reason,
        }

    def export(self, kind: str = "trades", fmt: str = "json") -> str:
        """Export trades or positions as JSON or CSV text."""
        if kind == "trades":
            rows = [to_jsonable(t) for t in self.trades(limit=1_000_000)]
        elif kind == "positions":
            rows = [to_jsonable(p) for p in self.positions()]
        else:
            raise InvalidOrderError(f"kind must be 'trades' or 'positions', got {kind!r}")
        if fmt == "json":
            return json.dumps(rows, indent=2)
        if fmt == "csv":
            buf = io.StringIO()
            if rows:
                writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            return buf.getvalue()
        raise InvalidOrderError(f"fmt must be 'json' or 'csv', got {fmt!r}")


__all__ = [
    "ACTION_ASSIGN",
    "ACTION_BUY_TO_CLOSE",
    "ACTION_EXPIRE",
    "ACTION_SELL_TO_OPEN",
    "CLOSED_STATUSES",
    "Ledger",
]
