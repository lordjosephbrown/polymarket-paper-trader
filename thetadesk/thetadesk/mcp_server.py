"""MCP server exposing thetadesk as tools for AI agents.

Run with:
    thetadesk-mcp                    # stdio transport
    thetadesk mcp
    python -m thetadesk.mcp_server

The agent supplies option chains as JSON (for example, reshaped from its
broker connector); the desk scores, sizes, paper-trades, and manages them.
Works with both the v1 (``FastMCP``) and v2 (``MCPServer``) Python SDKs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from thetadesk.desk import DEFAULT_MAX_PCT_PER_POSITION, Desk
from thetadesk.models import (
    Chain,
    DeskError,
    parse_chain,
    parse_chains,
    to_jsonable,
)
from thetadesk.robinhood import build_chain
from thetadesk.rules import ManagementRules
from thetadesk.scanner import ScanFilters, size_position

NDIGITS = 4
INSTRUCTIONS = (
    "Paper desk for selling options premium. Fetch option chains from your broker "
    "tools, pass them as JSON to scan_chains / analyze_option, paper-sell with "
    "paper_sell, then run manage_positions with fresh chains to take profits, stop "
    "out, or roll. Call chain_format for the expected JSON layout."
)

mcp = _Server("thetadesk", instructions=INSTRUCTIONS)

# ---------------------------------------------------------------------------
# Desk lifecycle — one Desk per server session
# ---------------------------------------------------------------------------

_desk: Desk | None = None


def _validate_account_name(account: str) -> str:
    """Validate account name to prevent path traversal."""
    if not account or ".." in account or "/" in account or "\\" in account:
        raise ValueError(f"Invalid account name: {account!r}")
    if account != account.strip():
        raise ValueError(f"Invalid account name: {account!r}")
    return account


def _data_root() -> Path:
    env = os.environ.get("THETADESK_DATA_DIR")
    return Path(env) if env else Path.home() / ".thetadesk"


def _get_desk(account: str = "default") -> Desk:
    """Return the current Desk, creating (or switching account) if needed."""
    global _desk
    account = _validate_account_name(account)
    data_dir = _data_root() / account
    if _desk is None or _desk.ledger.data_dir != data_dir:
        if _desk is not None:
            _desk.close()
        _desk = Desk(data_dir)
    return _desk


def _ok(data: object) -> str:
    return json.dumps({"ok": True, "data": to_jsonable(data, NDIGITS)})


def _err(msg: str, code: str = "error") -> str:
    return json.dumps({"ok": False, "error": msg, "code": code})


def _err_from(e: Exception) -> str:
    """Error envelope from an exception, hiding internals of unexpected errors."""
    if isinstance(e, (DeskError, ValueError, TypeError)):
        return _err(str(e), getattr(e, "code", type(e).__name__))
    return _err("Internal error", "internal_error")


def _rights(rights: str) -> tuple[str, ...]:
    text = rights.strip().lower()
    if text in ("both", "all", ""):
        return ("put", "call")
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _chain(data: dict | str) -> Chain:
    return parse_chain(data)


def _chains(data: list[dict] | dict | str | None) -> list[Chain]:
    if data is None:
        return []
    return parse_chains(data)


# ---------------------------------------------------------------------------
# Account tools
# ---------------------------------------------------------------------------


@mcp.tool()
def init_account(cash: float = 100_000.0, account: str = "default") -> str:
    """Create (or reset) the paper account with starting cash in USD."""
    try:
        return _ok(_get_desk(account).init_account(cash))
    except Exception as e:
        return _err_from(e)


@mcp.tool()
def get_balance(account: str = "default") -> str:
    """Cash, collateral in use, buying power, and realized P&L."""
    try:
        return _ok(_get_desk(account).balance())
    except Exception as e:
        return _err_from(e)


@mcp.tool()
def reset_account(account: str = "default") -> str:
    """Delete all positions, trades, and the account."""
    try:
        _get_desk(account).reset()
        return _ok({"reset": True})
    except Exception as e:
        return _err_from(e)


# ---------------------------------------------------------------------------
# Research tools
# ---------------------------------------------------------------------------


@mcp.tool()
def chain_format() -> str:
    """The chain JSON layout accepted by every tool that takes a chain.

    Robinhood-style field names (chain_symbol, expiration_date, strike_price,
    type, bid_price, ask_price, implied_volatility, open_interest) are accepted too.
    """
    example = {
        "underlying": "NBIS",
        "spot": 61.4,
        "as_of": "2026-09-11",
        "earnings_date": "2026-11-12",
        "iv_rank": 58,
        "options": [
            {
                "expiry": "2026-10-16",
                "strike": 52.5,
                "right": "put",
                "bid": 3.05,
                "ask": 3.30,
                "iv": 0.97,
                "delta": -0.25,
                "open_interest": 2364,
                "volume": 425,
                "id": "optional broker instrument id",
            }
        ],
    }
    notes = [
        "spot, as_of and options are required (as_of defaults to today); the rest is optional.",
        "iv and delta are optional: missing IV is implied from the mid, missing delta is computed.",
        "Rows without any price (bid/ask/mark) are skipped and counted in skipped_rows.",
        "Pass one chain object, or a list of chain objects, or the same as a JSON string.",
        "Robinhood users: pass get_option_instruments and get_option_quotes output to "
        "chain_from_robinhood instead of reshaping by hand.",
    ]
    return _ok({"example": example, "notes": notes})


@mcp.tool()
def chain_from_robinhood(
    underlying: str,
    spot: float | dict | str,
    instruments: list[dict] | dict | str,
    quotes: list[dict] | dict | str,
    as_of: str | None = None,
    earnings_date: str | None = None,
    iv_rank: float | None = None,
) -> str:
    """Join Robinhood option payloads into a chain every other tool accepts.

    instruments: get_option_instruments output (one page, a list of pages, or
    the bare instrument rows). quotes: get_option_quotes output, same forms.
    spot: a number or the get_equity_quotes payload for the underlying.
    Contracts without a quote are dropped; counts are reported under unmatched.
    """
    try:
        return _ok(
            build_chain(
                underlying, spot, instruments, quotes,
                as_of=as_of, earnings_date=earnings_date, iv_rank=iv_rank,
            )
        )
    except Exception as e:
        return _err_from(e)


@mcp.tool()
def scan_chains(
    chains: list[dict] | dict | str,
    rights: str = "both",
    min_dte: int = 25,
    max_dte: int = 50,
    min_delta: float = 0.10,
    max_delta: float = 0.30,
    min_open_interest: int = 100,
    min_volume: int = 0,
    max_spread_pct: float = 0.15,
    min_credit: float = 0.10,
    min_pop: float = 0.0,
    otm_only: bool = True,
    exclude_earnings: bool = True,
    fill: str = "bid",
    limit: int = 15,
    sort: str = "score",
) -> str:
    """Rank short-option candidates (puts and/or calls) across one or more chains.

    Score = probability of profit x annualized return on collateral x liquidity
    factor. Sort by score, annualized_roc, pop, credit, or dte.
    """
    try:
        filters = ScanFilters(
            rights=_rights(rights),
            min_dte=min_dte,
            max_dte=max_dte,
            min_delta=min_delta,
            max_delta=max_delta,
            min_open_interest=min_open_interest,
            min_volume=min_volume,
            max_spread_pct=max_spread_pct,
            min_credit=min_credit,
            min_pop=min_pop,
            otm_only=otm_only,
            exclude_earnings=exclude_earnings,
            fill=fill,
        )
        desk = _get_desk()
        return _ok(desk.scan(_chains(chains), filters, limit=limit, sort=sort))
    except Exception as e:
        return _err_from(e)


@mcp.tool()
def analyze_option(
    chain: dict | str,
    expiry: str,
    strike: float,
    right: str,
    fill: str = "bid",
    max_pct_per_position: float = DEFAULT_MAX_PCT_PER_POSITION,
    account: str = "default",
) -> str:
    """Greeks, probability of profit, breakeven, return on collateral, and a size."""
    try:
        desk = _get_desk(account)
        return _ok(
            desk.analyze(
                _chain(chain), expiry, strike, right, fill=fill,
                max_pct_per_position=max_pct_per_position,
            )
        )
    except Exception as e:
        return _err_from(e)


@mcp.tool()
def size_for(
    collateral_per_contract: float,
    account_value: float,
    buying_power: float,
    max_pct_per_position: float = DEFAULT_MAX_PCT_PER_POSITION,
    max_contracts: int | None = None,
) -> str:
    """Contracts that fit a per-position budget and available buying power."""
    try:
        return _ok(
            size_position(
                collateral_per_contract, account_value, buying_power,
                max_pct_per_position, max_contracts,
            )
        )
    except Exception as e:
        return _err_from(e)


# ---------------------------------------------------------------------------
# Trading tools (paper)
# ---------------------------------------------------------------------------


@mcp.tool()
def paper_sell(
    chain: dict | str,
    expiry: str,
    strike: float,
    right: str,
    contracts: int,
    fill: str = "bid",
    price: float | None = None,
    notes: str | None = None,
    account: str = "default",
) -> str:
    """Sell to open (paper) at the chain's bid (default) or mid; collateral is reserved."""
    try:
        desk = _get_desk(account)
        return _ok(
            desk.sell(_chain(chain), expiry, strike, right, contracts, fill=fill, price=price, notes=notes)
        )
    except Exception as e:
        return _err_from(e)


@mcp.tool()
def paper_close(
    position_id: int,
    price: float | None = None,
    chain: dict | str | None = None,
    reason: str = "manual",
    account: str = "default",
) -> str:
    """Buy to close a position at a price, or at the ask from a fresh chain."""
    try:
        desk = _get_desk(account)
        parsed = _chain(chain) if chain is not None else None
        return _ok(desk.close_position(position_id, price=price, chain=parsed, reason=reason))
    except Exception as e:
        return _err_from(e)


@mcp.tool()
def paper_roll(
    position_id: int,
    chain: dict | str,
    new_expiry: str,
    new_strike: float | None = None,
    fill: str = "bid",
    account: str = "default",
) -> str:
    """Roll a position to a later expiry (same strike unless new_strike is given)."""
    try:
        desk = _get_desk(account)
        return _ok(desk.roll(position_id, _chain(chain), new_expiry, new_strike=new_strike, fill=fill))
    except Exception as e:
        return _err_from(e)


@mcp.tool()
def paper_settle(position_id: int, spot: float, account: str = "default") -> str:
    """Settle an expired position: worthless expiry or cash-settled assignment."""
    try:
        return _ok(_get_desk(account).settle(position_id, spot))
    except Exception as e:
        return _err_from(e)


# ---------------------------------------------------------------------------
# Monitoring tools
# ---------------------------------------------------------------------------


def _rules(
    take_profit_pct: float, stop_loss_multiple: float, manage_dte: int, tested_delta: float, fill: str
) -> ManagementRules:
    return ManagementRules(
        take_profit_pct=take_profit_pct,
        stop_loss_multiple=stop_loss_multiple,
        manage_dte=manage_dte,
        tested_delta=tested_delta,
        fill=fill,
    )


@mcp.tool()
def paper_positions(
    chains: list[dict] | dict | str | None = None,
    take_profit_pct: float = 0.50,
    stop_loss_multiple: float = 2.0,
    manage_dte: int = 21,
    tested_delta: float = 0.40,
    fill: str = "bid",
    account: str = "default",
) -> str:
    """Open positions; marked to market and rule-checked when chains are supplied."""
    try:
        desk = _get_desk(account)
        rules = _rules(take_profit_pct, stop_loss_multiple, manage_dte, tested_delta, fill)
        return _ok(desk.positions(_chains(chains), rules))
    except Exception as e:
        return _err_from(e)


@mcp.tool()
def manage_positions(
    chains: list[dict] | dict | str,
    apply: bool = False,
    take_profit_pct: float = 0.50,
    stop_loss_multiple: float = 2.0,
    manage_dte: int = 21,
    tested_delta: float = 0.40,
    fill: str = "bid",
    account: str = "default",
) -> str:
    """Evaluate open positions against the rules (take profit, stop loss, 21-DTE, tested).

    With apply=true, closes and settlements are executed; rolls are only
    proposed (use paper_roll to execute one).
    """
    try:
        desk = _get_desk(account)
        rules = _rules(take_profit_pct, stop_loss_multiple, manage_dte, tested_delta, fill)
        return _ok(desk.manage(_chains(chains), rules, apply=apply))
    except Exception as e:
        return _err_from(e)


# ---------------------------------------------------------------------------
# Journal tools
# ---------------------------------------------------------------------------


@mcp.tool()
def stats(account: str = "default") -> str:
    """Win rate, realized P&L, profit factor, max drawdown, and breakdowns."""
    try:
        return _ok(_get_desk(account).stats())
    except Exception as e:
        return _err_from(e)


@mcp.tool()
def journal(limit: int = 50, account: str = "default") -> str:
    """Trade journal, newest first."""
    try:
        return _ok(_get_desk(account).journal(limit=limit))
    except Exception as e:
        return _err_from(e)


@mcp.tool()
def export_journal(kind: str = "trades", fmt: str = "json", account: str = "default") -> str:
    """Export trades or positions as JSON or CSV text."""
    try:
        return _ok({"kind": kind, "format": fmt, "content": _get_desk(account).export(kind, fmt)})
    except Exception as e:
        return _err_from(e)


def main() -> None:
    """Entry point for ``thetadesk-mcp``."""
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
