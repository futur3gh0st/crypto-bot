from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FundingPrint:
    inst_id: str
    ts: datetime
    rate: float  # decimal, e.g. 0.0001 = 1 bp per interval
    venue: str = "okx"

    @property
    def rate_bps(self) -> float:
        return self.rate * 10_000.0


def trailing_window(rates: list[float], n: int = 3) -> list[float] | None:
    if n <= 0 or len(rates) < n:
        return None
    return list(rates[-n:])


def trailing_avg(rates: list[float], n: int = 3) -> float | None:
    w = trailing_window(rates, n)
    if w is None:
        return None
    return sum(w) / float(n)


def same_sign(rates: list[float], n: int = 3) -> bool:
    w = trailing_window(rates, n)
    if w is None:
        return False
    if any(r == 0.0 for r in w):
        return False
    return all(r > 0.0 for r in w) or all(r < 0.0 for r in w)


def harvest_side(rates: list[float], n: int = 3) -> int | None:
    """+1 = short perp / long spot (collect when funding > 0). -1 = opposite."""
    if not same_sign(rates, n):
        return None
    avg = trailing_avg(rates, n)
    if avg is None or avg == 0.0:
        return None
    return 1 if avg > 0.0 else -1


def should_enter(
    rates: list[float],
    min_funding: float = 0.0003,
    n: int = 3,
) -> bool:
    """Enter only on trailing n same-sign prints with |avg| >= min_funding."""
    if not same_sign(rates, n):
        return False
    avg = trailing_avg(rates, n)
    if avg is None:
        return False
    return abs(avg) >= min_funding


def should_exit(
    rates: list[float],
    exit_funding: float = 0.00005,
    n: int = 3,
) -> bool:
    """Exit when the trailing window flips sign or |avg| shrinks below exit_funding."""
    w = trailing_window(rates, n)
    if w is None:
        return False
    avg = trailing_avg(rates, n)
    if avg is None:
        return False
    flipped = not same_sign(rates, n)
    return flipped or abs(avg) < exit_funding


def funding_cash(notional: float, rate: float, side: int) -> float:
    """Cash received (positive) or paid (negative) at one funding print.

    side=+1 means short perp (receive when rate>0). side=-1 means long perp.
    """
    if side not in (-1, 1):
        raise ValueError("side must be +1 or -1")
    return float(notional) * float(rate) * float(side)


def open_close_cost_bps(
    spot_fee_bps: float,
    perp_fee_bps: float,
    half_spread_bps: float,
) -> float:
    """One-way (open OR close) cost in bps of notional: spot + perp + 2 half-spreads."""
    return float(spot_fee_bps) + float(perp_fee_bps) + 2.0 * float(half_spread_bps)


def round_trip_cost_bps(
    spot_fee_bps: float,
    perp_fee_bps: float,
    half_spread_bps: float,
) -> float:
    return 2.0 * open_close_cost_bps(spot_fee_bps, perp_fee_bps, half_spread_bps)


def cost_usd(notional: float, cost_bps: float) -> float:
    return float(notional) * float(cost_bps) / 10_000.0
