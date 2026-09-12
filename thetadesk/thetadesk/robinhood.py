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
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from thetadesk.models import InvalidChainError, parse_date, today_utc

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


__all__ = ["build_chain", "spot_from_equity_quotes"]
