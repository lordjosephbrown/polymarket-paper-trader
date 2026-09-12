"""Click CLI for thetadesk — options premium-selling paper desk.

Every command prints a JSON envelope: ``{"ok": true, "data": ...}`` or
``{"ok": false, "error": "...", "code": "..."}``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from thetadesk.desk import DEFAULT_MAX_PCT_PER_POSITION, Desk
from thetadesk.models import (
    Chain,
    DeskError,
    load_chain_file,
    parse_date,
    to_jsonable,
)
from thetadesk.robinhood import build_chain
from thetadesk.rules import ManagementRules
from thetadesk.scanner import FILLS, SORT_KEYS, ScanFilters

DEFAULT_DATA_DIR = Path.home() / ".thetadesk"
DEFAULT_ACCOUNT = "default"
NDIGITS = 4


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _ok(data: object) -> str:
    return json.dumps({"ok": True, "data": to_jsonable(data, NDIGITS)}, indent=2)


def _err(error: DeskError) -> str:
    return json.dumps({"ok": False, "error": error.message, "code": error.code}, indent=2)


def _fail(error: DeskError) -> None:
    click.echo(_err(error))
    sys.exit(1)


def _validate_account(account: str) -> str:
    if not account or ".." in account or "/" in account or "\\" in account or account != account.strip():
        raise click.BadParameter(f"Invalid account name: {account!r}")
    return account


def _desk(ctx: click.Context) -> Desk:
    base: Path = ctx.obj["data_dir"]
    account = _validate_account(ctx.obj["account"])
    return Desk(base / account)


def _load_chains(paths: tuple[str, ...]) -> list[Chain]:
    return [load_chain_file(p) for p in paths]


def _run(ctx: click.Context, fn, raw: bool = False) -> None:
    """Open the desk, run ``fn(desk)``, print the result, close the desk.

    Results are wrapped in the JSON envelope unless ``raw`` is set.
    """
    desk = _desk(ctx)
    try:
        result = fn(desk)
        click.echo(result, nl=False) if raw else click.echo(_ok(result))
    except DeskError as e:
        _fail(e)
    finally:
        desk.close()


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.option(
    "--data-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_DATA_DIR,
    envvar="THETADESK_DATA_DIR",
    help="Data directory for the paper ledger.",
)
@click.option(
    "--account",
    default=DEFAULT_ACCOUNT,
    envvar="THETADESK_ACCOUNT",
    help="Account name (each account gets its own database).",
)
@click.pass_context
def main(ctx: click.Context, data_dir: Path, account: str) -> None:
    """thetadesk — scan, size, paper-sell, and manage short options."""
    ctx.ensure_object(dict)
    ctx.obj["data_dir"] = data_dir
    ctx.obj["account"] = account


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


@main.command()
@click.option("--cash", default=100_000.0, show_default=True, help="Starting cash in USD.")
@click.pass_context
def init(ctx: click.Context, cash: float) -> None:
    """Create (or reset) the paper account."""
    _run(ctx, lambda d: d.init_account(cash))


@main.command()
@click.pass_context
def balance(ctx: click.Context) -> None:
    """Cash, collateral in use, buying power, realized P&L."""
    _run(ctx, lambda d: d.balance())


@main.command()
@click.option("--confirm", is_flag=True, help="Required to actually wipe the account.")
@click.pass_context
def reset(ctx: click.Context, confirm: bool) -> None:
    """Delete all positions, trades, and the account."""
    if not confirm:
        click.echo(_err(DeskError("Pass --confirm to reset the account")))
        sys.exit(1)

    def _do(d: Desk) -> dict:
        d.reset()
        return {"reset": True}

    _run(ctx, _do)


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------


def _scan_options(fn):
    opts = [
        click.option("--right", type=click.Choice(["put", "call", "both"]), default="both", show_default=True),
        click.option("--min-dte", type=int, default=25, show_default=True),
        click.option("--max-dte", type=int, default=50, show_default=True),
        click.option("--min-delta", type=float, default=0.10, show_default=True),
        click.option("--max-delta", type=float, default=0.30, show_default=True),
        click.option("--min-oi", type=int, default=100, show_default=True, help="Minimum open interest."),
        click.option("--min-volume", type=int, default=0, show_default=True),
        click.option("--max-spread", type=float, default=0.15, show_default=True, help="Max bid-ask spread as a fraction of mid."),
        click.option("--min-credit", type=float, default=0.10, show_default=True),
        click.option("--min-pop", type=float, default=0.0, show_default=True),
        click.option("--include-itm", is_flag=True, help="Also consider in-the-money strikes."),
        click.option("--include-earnings", is_flag=True, help="Keep options with earnings before expiry."),
        click.option("--fill", type=click.Choice(list(FILLS)), default="bid", show_default=True),
    ]
    for opt in reversed(opts):
        fn = opt(fn)
    return fn


def _filters_from(kwargs: dict) -> ScanFilters:
    right = kwargs["right"]
    return ScanFilters(
        rights=("put", "call") if right == "both" else (right,),
        min_dte=kwargs["min_dte"],
        max_dte=kwargs["max_dte"],
        min_delta=kwargs["min_delta"],
        max_delta=kwargs["max_delta"],
        min_open_interest=kwargs["min_oi"],
        min_volume=kwargs["min_volume"],
        max_spread_pct=kwargs["max_spread"],
        min_credit=kwargs["min_credit"],
        min_pop=kwargs["min_pop"],
        otm_only=not kwargs["include_itm"],
        exclude_earnings=not kwargs["include_earnings"],
        fill=kwargs["fill"],
    )


@main.command()
@click.argument("chains", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False))
@_scan_options
@click.option("--limit", type=int, default=15, show_default=True)
@click.option("--sort", type=click.Choice(list(SORT_KEYS)), default="score", show_default=True)
@click.pass_context
def scan(ctx: click.Context, chains: tuple[str, ...], limit: int, sort: str, **kwargs) -> None:
    """Rank short-option candidates across one or more chain files."""

    def _do(d: Desk) -> dict:
        return d.scan(_load_chains(chains), _filters_from(kwargs), limit=limit, sort=sort)

    _run(ctx, _do)


@main.command("chain-from-robinhood")
@click.option("--underlying", required=True, help="Ticker, e.g. NBIS")
@click.option("--spot", required=True, help="Spot price, or a JSON file of get_equity_quotes output.")
@click.option("--instruments", "instrument_files", required=True, multiple=True, type=click.Path(exists=True, dir_okay=False), help="get_option_instruments page(s) as JSON files.")
@click.option("--quotes", "quote_files", required=True, multiple=True, type=click.Path(exists=True, dir_okay=False), help="get_option_quotes page(s) as JSON files.")
@click.option("--as-of", default=None, help="Quote date YYYY-MM-DD (default: today).")
@click.option("--earnings", default=None, help="Next earnings date YYYY-MM-DD.")
@click.option("--iv-rank", type=float, default=None)
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path), default=None, help="Write the chain here instead of stdout.")
def chain_from_robinhood(underlying: str, spot: str, instrument_files: tuple[str, ...], quote_files: tuple[str, ...], as_of: str | None, earnings: str | None, iv_rank: float | None, out: Path | None) -> None:
    """Join Robinhood contract and quote payloads into a chain file."""
    try:
        spot_value: object = Path(spot).read_text() if Path(spot).is_file() else spot
        chain = build_chain(
            underlying,
            spot_value,
            [Path(f).read_text() for f in instrument_files],
            [Path(f).read_text() for f in quote_files],
            as_of=as_of,
            earnings_date=earnings,
            iv_rank=iv_rank,
        )
    except DeskError as e:
        _fail(e)
    text = json.dumps(chain, indent=1)
    if out is not None:
        out.write_text(text + "\n")
        click.echo(_ok({"written": str(out), "options": len(chain["options"]), "unmatched": chain["unmatched"]}))
    else:
        click.echo(text)


@main.command()
@click.argument("chain", type=click.Path(exists=True, dir_okay=False))
@click.option("--expiry", required=True, help="YYYY-MM-DD")
@click.option("--strike", required=True, type=float)
@click.option("--right", required=True, type=click.Choice(["put", "call"]))
@click.option("--fill", type=click.Choice(list(FILLS)), default="bid", show_default=True)
@click.option("--max-pct", type=float, default=DEFAULT_MAX_PCT_PER_POSITION, show_default=True, help="Max fraction of the account per position.")
@click.pass_context
def analyze(ctx: click.Context, chain: str, expiry: str, strike: float, right: str, fill: str, max_pct: float) -> None:
    """Greeks, probabilities, return on collateral, and a position size for one option."""
    _run(ctx, lambda d: d.analyze(load_chain_file(chain), parse_date(expiry), strike, right, fill=fill, max_pct_per_position=max_pct))


# ---------------------------------------------------------------------------
# Trading
# ---------------------------------------------------------------------------


@main.command()
@click.argument("chain", type=click.Path(exists=True, dir_okay=False))
@click.option("--expiry", required=True, help="YYYY-MM-DD")
@click.option("--strike", required=True, type=float)
@click.option("--right", required=True, type=click.Choice(["put", "call"]))
@click.option("--contracts", required=True, type=int)
@click.option("--fill", type=click.Choice(list(FILLS)), default="bid", show_default=True)
@click.option("--price", type=float, default=None, help="Override the credit per share.")
@click.option("--notes", default=None, help="Thesis or notes for the journal.")
@click.pass_context
def sell(ctx: click.Context, chain: str, expiry: str, strike: float, right: str, contracts: int, fill: str, price: float | None, notes: str | None) -> None:
    """Sell to open (paper) at the chain's bid or mid."""
    _run(ctx, lambda d: d.sell(load_chain_file(chain), parse_date(expiry), strike, right, contracts, fill=fill, price=price, notes=notes))


@main.command()
@click.argument("position_id", type=int)
@click.option("--price", type=float, default=None, help="Buy-to-close price per share.")
@click.option("--chain", "chain_path", type=click.Path(exists=True, dir_okay=False), default=None, help="Price the close at this chain's ask.")
@click.option("--reason", default="manual", show_default=True)
@click.pass_context
def close(ctx: click.Context, position_id: int, price: float | None, chain_path: str | None, reason: str) -> None:
    """Buy to close a position at a price or from a chain."""
    chain = load_chain_file(chain_path) if chain_path else None
    _run(ctx, lambda d: d.close_position(position_id, price=price, chain=chain, reason=reason))


@main.command()
@click.argument("position_id", type=int)
@click.option("--chain", "chain_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--expiry", required=True, help="New expiry YYYY-MM-DD")
@click.option("--strike", type=float, default=None, help="New strike (default: same strike).")
@click.option("--fill", type=click.Choice(list(FILLS)), default="bid", show_default=True)
@click.pass_context
def roll(ctx: click.Context, position_id: int, chain_path: str, expiry: str, strike: float | None, fill: str) -> None:
    """Roll a position out (and optionally to a new strike)."""
    _run(ctx, lambda d: d.roll(position_id, load_chain_file(chain_path), parse_date(expiry), new_strike=strike, fill=fill))


@main.command()
@click.argument("position_id", type=int)
@click.option("--spot", required=True, type=float, help="Underlying price at expiry.")
@click.pass_context
def settle(ctx: click.Context, position_id: int, spot: float) -> None:
    """Settle an expired position (worthless, or cash-settled assignment)."""
    _run(ctx, lambda d: d.settle(position_id, spot))


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------


def _rules_options(fn):
    opts = [
        click.option("--take-profit", type=float, default=0.50, show_default=True, help="Close at this fraction of max profit."),
        click.option("--stop-loss", type=float, default=2.0, show_default=True, help="Close when the loss reaches N x credit."),
        click.option("--manage-dte", type=int, default=21, show_default=True),
        click.option("--tested-delta", type=float, default=0.40, show_default=True),
        click.option("--fill", type=click.Choice(list(FILLS)), default="bid", show_default=True),
    ]
    for opt in reversed(opts):
        fn = opt(fn)
    return fn


def _rules_from(kwargs: dict) -> ManagementRules:
    return ManagementRules(
        take_profit_pct=kwargs["take_profit"],
        stop_loss_multiple=kwargs["stop_loss"],
        manage_dte=kwargs["manage_dte"],
        tested_delta=kwargs["tested_delta"],
        fill=kwargs["fill"],
    )


@main.command()
@click.argument("chains", nargs=-1, type=click.Path(exists=True, dir_okay=False))
@_rules_options
@click.pass_context
def positions(ctx: click.Context, chains: tuple[str, ...], **kwargs) -> None:
    """Open positions, marked to market when chain files are given."""
    _run(ctx, lambda d: d.positions(_load_chains(chains), _rules_from(kwargs)))


@main.command()
@click.argument("chains", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False))
@_rules_options
@click.option("--apply", is_flag=True, help="Execute the recommended closes and settlements.")
@click.pass_context
def manage(ctx: click.Context, chains: tuple[str, ...], apply: bool, **kwargs) -> None:
    """Apply the management rules to open positions (rolls are never automatic)."""
    _run(ctx, lambda d: d.manage(_load_chains(chains), _rules_from(kwargs), apply=apply))


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


@main.command()
@click.pass_context
def stats(ctx: click.Context) -> None:
    """Win rate, realized P&L, profit factor, drawdown, breakdowns."""
    _run(ctx, lambda d: d.stats())


@main.command()
@click.option("--limit", type=int, default=50, show_default=True)
@click.pass_context
def journal(ctx: click.Context, limit: int) -> None:
    """Trade journal, newest first."""
    _run(ctx, lambda d: d.journal(limit=limit))


@main.command()
@click.argument("kind", type=click.Choice(["trades", "positions"]))
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), default="json", show_default=True)
@click.pass_context
def export(ctx: click.Context, kind: str, fmt: str) -> None:
    """Export trades or positions as JSON or CSV (raw, not enveloped)."""
    _run(ctx, lambda d: d.export(kind, fmt), raw=True)


@main.command()
def mcp() -> None:
    """Start the MCP server on stdio."""
    from thetadesk.mcp_server import main as mcp_main

    mcp_main()


if __name__ == "__main__":  # pragma: no cover
    main()
