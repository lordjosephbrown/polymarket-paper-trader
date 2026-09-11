"""Test helpers shared across thetadesk test modules."""

from __future__ import annotations

from datetime import date, timedelta

from thetadesk import pricing
from thetadesk.models import Chain, OptionQuote

AS_OF = date(2026, 9, 11)


def make_chain(
    underlying: str = "XYZ",
    spot: float = 100.0,
    as_of: date = AS_OF,
    dtes: tuple[int, ...] = (10, 35, 49, 63),
    strikes: tuple[float, ...] = (70, 80, 85, 90, 95, 100, 105, 110, 115, 120, 130),
    iv: float = 0.40,
    open_interest: int = 500,
    volume: int = 50,
    earnings_date: date | None = None,
    iv_rank: float | None = 50.0,
    with_greeks: bool = True,
    spread: float = 0.04,
) -> Chain:
    """A synthetic, internally consistent chain priced with Black-Scholes."""
    options: list[OptionQuote] = []
    for dte in dtes:
        expiry = as_of + timedelta(days=dte)
        t = pricing.years(dte)
        for strike in strikes:
            for right in ("put", "call"):
                mid = pricing.bs_price(spot, strike, iv, t, right)
                if mid < 0.02:
                    continue
                half = max(0.01, mid * spread)
                delta = pricing.greeks(spot, strike, iv, t, right).delta
                options.append(
                    OptionQuote(
                        underlying=underlying,
                        expiry=expiry,
                        strike=float(strike),
                        right=right,
                        bid=round(mid - half, 2),
                        ask=round(mid + half, 2),
                        iv=iv if with_greeks else None,
                        delta=delta if with_greeks else None,
                        open_interest=open_interest,
                        volume=volume,
                        id=f"{underlying}-{expiry.isoformat()}-{strike:g}-{right}",
                    )
                )
    return Chain(
        underlying=underlying,
        spot=spot,
        options=options,
        as_of=as_of,
        earnings_date=earnings_date,
        iv_rank=iv_rank,
    )
