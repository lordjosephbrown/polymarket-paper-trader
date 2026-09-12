"""Build a thetadesk chain from Robinhood option payloads.

Robinhood's option data comes in two halves keyed by instrument id:

* contracts (``get_option_instruments``): ``{"data": {"instruments": [
  {"id", "expiration_date", "strike_price", "type", ...}], "next": ...}}``
* quotes (``get_option_quotes``): ``{"data": {"results": [{"quote": {
  "instrument_id", "bid_price", "ask_price", "implied_volatility", "delta",
  "open_interest", "volume", ...}, "close": {...}}]}}``

``build_chain`` joins them into the canonical chain JSON that every desk
tool accepts. Both inputs may be a single page, a list of pages, a bare
list of rows, or JSON text for any of those, so an agent can paste tool
output straight through, one page after another.

``chain_from_csv`` does the same join from two compact CSV tables
(``id,expiry,strike,right`` and ``id,bid,ask,iv,delta,open_interest,volume``).
That is the cheapest hand-off for an agent that has to re-type the data:
a few dozen characters per contract instead of the full payload objects.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from typing import Any

from thetadesk.models import InvalidChainError, normalize_right, parse_date, today_utc

_INSTRUMENT_LIST_KEYS = ("instruments", "results", "items")
_QUOTE_LIST_KEYS = ("results", "quotes", "items")


def _load(data: Any) -> Any:
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError as e:
            raise InvalidChainError(f"Payload is not valid JSON: {e.msg}") from e
    return data


def _rows(data: Any, list_keys: tuple[str, ...]) -> list[dict]:
    """Flatten pages / lists / bare rows into a list of row dicts."""
    data = _load(data)
    if data is None:
        return []
    if isinstance(data, list):
        out: list[dict] = []
        for item in data:
            out.extend(_rows(item, list_keys))
        return out
    if not isinstance(data, dict):
        raise InvalidChainError("Expected a JSON object, list, or string payload")
    if "data" in data and isinstance(data["data"], (dict, list)):
        return _rows(data["data"], list_keys)
    for key in list_keys:
        if key in data and isinstance(data[key], list):
            return _rows(data[key], list_keys)
    if "quote" in data and isinstance(data["quote"], dict):
        return [data["quote"]]
    return [data]


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def spot_from_equity_quotes(payload: Any, symbol: str) -> float:
    """Extract the last trade price for ``symbol`` from ``get_equity_quotes`` output."""
    rows = _rows(payload, ("results",))
    for row in rows:
        quote = row.get("quote", row)
        if str(quote.get("symbol", "")).upper() != symbol.upper():
            continue
        for key in ("last_trade_price", "last_non_reg_trade_price", "mark_price", "adjusted_previous_close"):
            price = _to_float(quote.get(key))
            if price:
                return price
    raise InvalidChainError(f"No quote for {symbol.upper()} in the equity quotes payload")


def build_chain(
    underlying: str,
    spot: Any,
    instruments: Any,
    quotes: Any,
    as_of: date | str | None = None,
    earnings_date: date | str | None = None,
    iv_rank: float | None = None,
) -> dict:
    """Join Robinhood contracts and quotes into canonical chain JSON.

    ``spot`` may be a number or the raw ``get_equity_quotes`` payload.
    Contracts without a quote are left out; quotes without a contract are
    ignored. The result reports both counts under ``unmatched``.
    """
    symbol = str(underlying).strip().upper()
    if not symbol:
        raise InvalidChainError("underlying is required")
    spot_value = _to_float(spot) if not isinstance(spot, (dict, list)) else None
    if spot_value is None:
        spot_value = spot_from_equity_quotes(spot, symbol)
    if spot_value <= 0:
        raise InvalidChainError("spot must be positive")

    contracts: dict[str, dict] = {}
    for row in _rows(instruments, _INSTRUMENT_LIST_KEYS):
        ident = row.get("id") or row.get("instrument_id")
        if not ident:
            continue
        chain_symbol = str(row.get("chain_symbol", symbol)).upper()
        if chain_symbol != symbol:
            continue
        state = str(row.get("state", "active")).lower()
        if state != "active":
            continue
        contracts[str(ident)] = row

    options: list[dict] = []
    unmatched_quotes = 0
    for quote in _rows(quotes, _QUOTE_LIST_KEYS):
        ident = quote.get("instrument_id") or quote.get("id")
        contract = contracts.get(str(ident)) if ident else None
        if contract is None:
            unmatched_quotes += 1
            continue
        options.append(
            {
                "id": str(ident),
                "expiry": contract.get("expiration_date"),
                "strike": _to_float(contract.get("strike_price")),
                "right": contract.get("type"),
                "bid": _to_float(quote.get("bid_price")),
                "ask": _to_float(quote.get("ask_price")),
                "mark": _to_float(quote.get("mark_price")),
                "iv": _to_float(quote.get("implied_volatility")),
                "delta": _to_float(quote.get("delta")),
                "open_interest": quote.get("open_interest") or 0,
                "volume": quote.get("volume") or 0,
            }
        )
    matched_ids = {o["id"] for o in options}
    chain = {
        "underlying": symbol,
        "spot": spot_value,
        "as_of": (parse_date(as_of) if as_of is not None else today_utc()).isoformat(),
        "options": options,
        "unmatched": {
            "contracts_without_quotes": len([c for c in contracts if c not in matched_ids]),
            "quotes_without_contracts": unmatched_quotes,
        },
        "source": "robinhood",
    }
    if earnings_date is not None:
        chain["earnings_date"] = parse_date(earnings_date).isoformat()
    if iv_rank is not None:
        chain["iv_rank"] = float(iv_rank)
    return chain


# ---------------------------------------------------------------------------
# Compact CSV input
# ---------------------------------------------------------------------------

_CSV_COLUMNS: dict[str, tuple[str, ...]] = {
    "id": ("id", "instrument_id", "prefix"),
    "expiry": ("expiry", "expiration_date", "expiration"),
    "strike": ("strike", "strike_price"),
    "right": ("right", "type"),
    "bid": ("bid", "bid_price"),
    "ask": ("ask", "ask_price"),
    "mark": ("mark", "mark_price"),
    "iv": ("iv", "implied_volatility"),
    "delta": ("delta",),
    "open_interest": ("open_interest", "oi"),
    "volume": ("volume", "vol"),
}
_CONTRACT_COLUMNS = ("id", "expiry", "strike", "right")
_QUOTE_COLUMNS = ("id", "bid", "ask")


def _to_int(value: Any) -> int:
    number = _to_float(value)
    return int(number) if number is not None else 0


def _csv_rows(text: str, required: tuple[str, ...], what: str) -> list[dict[str, str]]:
    """Parse CSV text into rows keyed by canonical column names.

    Header names are matched case-insensitively against the canonical names
    and their Robinhood aliases. Rows without an id are skipped.
    """
    reader = csv.DictReader(io.StringIO(str(text).strip()))
    header = {str(name).strip().lower(): name for name in (reader.fieldnames or []) if name}
    columns: dict[str, str] = {}
    for canonical, names in _CSV_COLUMNS.items():
        for name in names:
            if name in header:
                columns[canonical] = header[name]
                break
    missing = [c for c in required if c not in columns]
    if missing:
        raise InvalidChainError(f"{what} CSV is missing columns: {', '.join(missing)}")
    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {canonical: str(raw.get(col) or "").strip() for canonical, col in columns.items()}
        if row["id"]:
            rows.append(row)
    return rows


def _resolve_id(ident: str, ids: list[str]) -> str:
    """Map a quote id (a full id or a unique prefix) to a contract id."""
    if ident in ids:
        return ident
    matches = [full for full in ids if full.startswith(ident)]
    if len(matches) > 1:
        raise InvalidChainError(f"Quote id {ident!r} matches {len(matches)} contracts; use a longer prefix")
    return matches[0] if matches else ident


def chain_from_csv(
    underlying: str,
    spot: Any,
    contracts_csv: str,
    quotes_csv: str,
    as_of: date | str | None = None,
    earnings_date: date | str | None = None,
    iv_rank: float | None = None,
) -> dict:
    """Build a chain from two compact CSV tables instead of raw payloads.

    ``contracts_csv`` columns: ``id,expiry,strike,right``.
    ``quotes_csv`` columns: ``id,bid,ask[,iv,delta,open_interest,volume,mark]``,
    where ``id`` may be a unique prefix of the contract id (the first eight
    characters of a Robinhood instrument id are enough in practice).
    Robinhood field names are accepted as column aliases. ``spot`` is a
    number or the ``get_equity_quotes`` payload, as for ``build_chain``.
    """
    instruments: list[dict] = []
    for row in _csv_rows(contracts_csv, _CONTRACT_COLUMNS, "contracts"):
        strike = _to_float(row["strike"])
        if strike is None or strike <= 0:
            raise InvalidChainError(f"Invalid strike {row['strike']!r} for contract {row['id']}")
        instruments.append(
            {
                "id": row["id"],
                "expiration_date": parse_date(row["expiry"]).isoformat(),
                "strike_price": strike,
                "type": normalize_right(row["right"]),
            }
        )
    ids = [row["id"] for row in instruments]
    quotes = [
        {
            "instrument_id": _resolve_id(row["id"], ids),
            "bid_price": row["bid"],
            "ask_price": row["ask"],
            "mark_price": row.get("mark") or None,
            "implied_volatility": row.get("iv") or None,
            "delta": row.get("delta") or None,
            "open_interest": _to_int(row.get("open_interest")),
            "volume": _to_int(row.get("volume")),
        }
        for row in _csv_rows(quotes_csv, _QUOTE_COLUMNS, "quotes")
    ]
    return build_chain(
        underlying, spot, instruments, quotes,
        as_of=as_of, earnings_date=earnings_date, iv_rank=iv_rank,
    )


__all__ = ["build_chain", "chain_from_csv", "spot_from_equity_quotes"]
