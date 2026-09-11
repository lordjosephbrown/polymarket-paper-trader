"""Dataclasses, error types, and option-chain normalization for thetadesk.

The canonical chain format (also produced by ``Chain.to_dict``)::

    {
      "underlying": "NBIS",
      "spot": 61.4,
      "as_of": "2026-09-11",            # optional, defaults to today (UTC)
      "earnings_date": "2026-11-12",    # optional
      "iv_rank": 55,                    # optional, 0-100
      "options": [
        {"expiry": "2026-10-17", "strike": 55, "right": "put",
         "bid": 2.10, "ask": 2.25, "iv": 0.92, "delta": -0.24,
         "open_interest": 2100, "volume": 340, "id": "optional-instrument-id"}
      ]
    }

``parse_chain`` also accepts the field names used by Robinhood's option
instrument and market-data payloads (``chain_symbol``, ``expiration_date``,
``strike_price``, ``type``, ``bid_price``, ``ask_price``,
``implied_volatility``, ``open_interest`` ...) so an agent can hand over
broker output with little or no reshaping.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

CONTRACT_MULTIPLIER = 100
PUT = "put"
CALL = "call"
RIGHTS: tuple[str, str] = (PUT, CALL)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class DeskError(Exception):
    """Base error for all thetadesk errors."""

    code: str = "DESK_ERROR"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class NotInitializedError(DeskError):
    code = "NOT_INITIALIZED"

    def __init__(
        self,
        message: str = "Account not initialized. Run 'thetadesk init' first.",
    ) -> None:
        super().__init__(message)


class InsufficientBuyingPowerError(DeskError):
    code = "INSUFFICIENT_BUYING_POWER"

    def __init__(self, required: float, available: float) -> None:
        super().__init__(
            f"Insufficient buying power: need ${required:,.2f}, have ${available:,.2f}"
        )
        self.required = required
        self.available = available


class PositionNotFoundError(DeskError):
    code = "POSITION_NOT_FOUND"

    def __init__(self, position_id: int) -> None:
        super().__init__(f"Position not found: {position_id}")
        self.position_id = position_id


class PositionClosedError(DeskError):
    code = "POSITION_CLOSED"

    def __init__(self, position_id: int, status: str) -> None:
        super().__init__(f"Position {position_id} is not open (status: {status})")
        self.position_id = position_id
        self.status = status


class InvalidChainError(DeskError):
    code = "INVALID_CHAIN"


class OptionNotFoundError(DeskError):
    code = "OPTION_NOT_FOUND"

    def __init__(self, underlying: str, expiry: str, strike: float, right: str) -> None:
        super().__init__(
            f"Option not found in chain: {underlying} {expiry} {strike:g} {right}"
        )


class InvalidOrderError(DeskError):
    code = "INVALID_ORDER"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def today_utc() -> date:
    """Today's date in UTC."""
    return utc_now().date()


def parse_date(value: Any) -> date:
    """Parse a date from a date/datetime or an ISO string (date or datetime)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            pass
        try:
            return date.fromisoformat(text[:10])
        except ValueError as e:
            raise InvalidChainError(f"Invalid date: {value!r}") from e
    raise InvalidChainError(f"Invalid date: {value!r}")


def normalize_right(value: Any) -> str:
    """Normalize an option right to ``"put"`` or ``"call"``."""
    text = str(value).strip().lower()
    if text in ("put", "p", "puts"):
        return PUT
    if text in ("call", "c", "calls"):
        return CALL
    raise InvalidChainError(f"Invalid option right: {value!r} (expected put/call)")


def _first(row: dict, *names: str) -> Any:
    """Return the first present, non-null value among the given keys."""
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _to_float(value: Any, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise InvalidChainError(f"Invalid {label}: {value!r}") from e


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def to_jsonable(obj: Any, ndigits: int | None = None) -> Any:
    """Recursively convert dataclasses, dates, and containers to JSON-safe values.

    When ``ndigits`` is given, floats are rounded for display.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v, ndigits) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: to_jsonable(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v, ndigits) for v in obj]
    if isinstance(obj, float) and ndigits is not None:
        return round(obj, ndigits)
    return obj


# ---------------------------------------------------------------------------
# Option quotes and chains
# ---------------------------------------------------------------------------


@dataclass
class OptionQuote:
    """A single option contract with its current market."""

    underlying: str
    expiry: date
    strike: float
    right: str
    bid: float
    ask: float
    iv: float | None = None
    delta: float | None = None
    open_interest: int = 0
    volume: int = 0
    id: str | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        """Bid-ask spread as a fraction of the mid (1.0 when there is no mid)."""
        mid = self.mid
        return self.spread / mid if mid > 0 else 1.0

    @property
    def key(self) -> tuple[str, float, str]:
        return (self.expiry.isoformat(), float(self.strike), self.right)

    @property
    def label(self) -> str:
        return f"{self.underlying} {self.expiry.isoformat()} {self.strike:g} {self.right}"


@dataclass
class Chain:
    """An option chain snapshot for one underlying."""

    underlying: str
    spot: float
    options: list[OptionQuote] = field(default_factory=list)
    as_of: date = field(default_factory=today_utc)
    earnings_date: date | None = None
    iv_rank: float | None = None
    skipped: int = 0

    def find(self, expiry: date | str, strike: float, right: str) -> OptionQuote | None:
        """Return the matching quote, or None."""
        exp = parse_date(expiry)
        r = normalize_right(right)
        target = float(strike)
        for q in self.options:
            if q.expiry == exp and q.right == r and abs(q.strike - target) < 1e-9:
                return q
        return None

    def get(self, expiry: date | str, strike: float, right: str) -> OptionQuote:
        """Return the matching quote or raise OptionNotFoundError."""
        q = self.find(expiry, strike, right)
        if q is None:
            raise OptionNotFoundError(
                self.underlying, parse_date(expiry).isoformat(), float(strike), normalize_right(right)
            )
        return q

    def expiries(self) -> list[date]:
        return sorted({q.expiry for q in self.options})

    def to_dict(self) -> dict:
        return to_jsonable(self)


_UNDERLYING_KEYS = ("underlying", "symbol", "chain_symbol", "ticker")
_SPOT_KEYS = ("spot", "underlying_price", "last", "price", "last_trade_price")
_AS_OF_KEYS = ("as_of", "asof", "timestamp", "updated_at", "date")
_EARNINGS_KEYS = ("earnings_date", "earnings", "next_earnings_date")
_OPTIONS_KEYS = ("options", "contracts", "quotes", "instruments", "chain")
_EXPIRY_KEYS = ("expiry", "expiration", "expiration_date", "exp")
_STRIKE_KEYS = ("strike", "strike_price")
_RIGHT_KEYS = ("right", "type", "option_type", "put_call", "contract_type")
_BID_KEYS = ("bid", "bid_price")
_ASK_KEYS = ("ask", "ask_price")
_MARK_KEYS = ("mark", "mark_price", "last", "last_trade_price", "adjusted_mark_price")
_IV_KEYS = ("iv", "implied_volatility", "implied_vol")
_OI_KEYS = ("open_interest", "oi", "openInterest")
_VOLUME_KEYS = ("volume", "vol")
_ID_KEYS = ("id", "instrument_id", "occ_symbol", "contract_symbol")


def _parse_option(row: Any, underlying: str, index: int) -> OptionQuote | None:
    """Parse one option row; return None when it has no usable price."""
    if not isinstance(row, dict):
        raise InvalidChainError(f"Option #{index} must be an object")
    expiry_raw = _first(row, *_EXPIRY_KEYS)
    strike_raw = _first(row, *_STRIKE_KEYS)
    right_raw = _first(row, *_RIGHT_KEYS)
    if expiry_raw is None or strike_raw is None or right_raw is None:
        raise InvalidChainError(f"Option #{index} needs expiry, strike, and right")
    expiry = parse_date(expiry_raw)
    strike = _to_float(strike_raw, "strike")
    if strike <= 0:
        raise InvalidChainError(f"Option #{index} has non-positive strike")
    right = normalize_right(right_raw)

    bid_raw = _first(row, *_BID_KEYS)
    ask_raw = _first(row, *_ASK_KEYS)
    if bid_raw is None and ask_raw is None:
        mark_raw = _first(row, *_MARK_KEYS)
        if mark_raw is None:
            return None
        bid_raw = ask_raw = mark_raw
    bid = _to_float(bid_raw if bid_raw is not None else ask_raw, "bid")
    ask = _to_float(ask_raw if ask_raw is not None else bid_raw, "ask")
    if bid < 0 or ask < 0:
        raise InvalidChainError(f"Option #{index} has a negative price")
    if ask < bid:
        bid, ask = ask, bid

    iv_raw = _first(row, *_IV_KEYS)
    iv: float | None = None
    if iv_raw is not None:
        iv = _to_float(iv_raw, "iv")
        if iv > 5.0:  # given in percent, e.g. 92.3
            iv /= 100.0
        if iv <= 0:
            iv = None

    delta_raw = _first(row, "delta")
    delta: float | None = None
    if delta_raw is not None:
        delta = _to_float(delta_raw, "delta")
        delta = -abs(delta) if right == PUT else abs(delta)

    id_raw = _first(row, *_ID_KEYS)
    return OptionQuote(
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        right=right,
        bid=bid,
        ask=ask,
        iv=iv,
        delta=delta,
        open_interest=_to_int(_first(row, *_OI_KEYS)),
        volume=_to_int(_first(row, *_VOLUME_KEYS)),
        id=str(id_raw) if id_raw is not None else None,
    )


def parse_chain(data: dict | str) -> Chain:
    """Build a Chain from a dict or JSON string in canonical or Robinhood-like form."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            raise InvalidChainError(f"Chain is not valid JSON: {e.msg}") from e
    if not isinstance(data, dict):
        raise InvalidChainError("Chain must be a JSON object")

    underlying_raw = _first(data, *_UNDERLYING_KEYS)
    if not underlying_raw or not str(underlying_raw).strip():
        raise InvalidChainError("Chain needs an 'underlying' symbol")
    underlying = str(underlying_raw).strip().upper()

    spot_raw = _first(data, *_SPOT_KEYS)
    if spot_raw is None:
        raise InvalidChainError("Chain needs a 'spot' price for the underlying")
    spot = _to_float(spot_raw, "spot")
    if spot <= 0:
        raise InvalidChainError("Chain 'spot' must be positive")

    as_of_raw = _first(data, *_AS_OF_KEYS)
    as_of = parse_date(as_of_raw) if as_of_raw is not None else today_utc()
    earnings_raw = _first(data, *_EARNINGS_KEYS)
    earnings = parse_date(earnings_raw) if earnings_raw is not None else None
    iv_rank_raw = _first(data, "iv_rank", "ivr")
    iv_rank = _to_float(iv_rank_raw, "iv_rank") if iv_rank_raw is not None else None

    rows = _first(data, *_OPTIONS_KEYS)
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise InvalidChainError("Chain 'options' must be a list")

    options: list[OptionQuote] = []
    skipped = 0
    for i, row in enumerate(rows):
        q = _parse_option(row, underlying, i)
        if q is None:
            skipped += 1
            continue
        options.append(q)

    return Chain(
        underlying=underlying,
        spot=spot,
        options=options,
        as_of=as_of,
        earnings_date=earnings,
        iv_rank=iv_rank,
        skipped=skipped,
    )


def parse_chains(data: Any) -> list[Chain]:
    """Parse one chain or a list of chains (dicts, JSON strings, or a JSON array)."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            raise InvalidChainError(f"Chain is not valid JSON: {e.msg}") from e
    if isinstance(data, dict):
        return [parse_chain(data)]
    if isinstance(data, list):
        return [parse_chain(item) for item in data]
    raise InvalidChainError("Expected a chain object or a list of chain objects")


def load_chain_file(path: str | Path) -> Chain:
    """Load a chain from a JSON file."""
    p = Path(path)
    try:
        text = p.read_text()
    except OSError as e:
        raise InvalidChainError(f"Cannot read chain file {p}: {e.strerror}") from e
    return parse_chain(text)


# ---------------------------------------------------------------------------
# Ledger records
# ---------------------------------------------------------------------------

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_EXPIRED = "expired"
STATUS_ASSIGNED = "assigned"


@dataclass
class Account:
    cash: float
    starting_cash: float
    created_at: str


@dataclass
class Position:
    """A short option position (one contract series, one or more contracts)."""

    id: int
    underlying: str
    right: str
    strike: float
    expiry: date
    contracts: int
    credit: float
    collateral: float
    opened_at: str
    status: str = STATUS_OPEN
    spot_at_open: float | None = None
    delta_at_open: float | None = None
    iv_at_open: float | None = None
    close_price: float | None = None
    closed_at: str | None = None
    close_reason: str | None = None
    realized_pnl: float | None = None
    rolled_from: int | None = None
    notes: str | None = None

    @property
    def strategy(self) -> str:
        return "short_put" if self.right == PUT else "short_call"

    @property
    def label(self) -> str:
        return f"{self.underlying} {self.expiry.isoformat()} {self.strike:g} {self.right}"

    @property
    def premium_received(self) -> float:
        return self.credit * CONTRACT_MULTIPLIER * self.contracts

    @property
    def total_collateral(self) -> float:
        return self.collateral * self.contracts


@dataclass
class TradeRecord:
    id: int
    position_id: int
    action: str
    price: float
    contracts: int
    cash_delta: float
    ts: str
    note: str | None = None
