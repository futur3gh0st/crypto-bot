"""Pair-complete lock math for Kalshi binary YES/NO. No I/O. No fade."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stablebot.kalshi.client import ScanRow


def kalshi_taker_fee(p: float) -> float:
    """Per-contract taker haircut: 0.07 * p * (1-p). Same as poly paper.

    Official Kalshi July 2026 schedule is
    ``round_up(0.07 * C * P * (1-P))`` per side. Paper uses the unrounded
    per-contract curve so lock math matches the poly sleeve. Fee is applied
    *before* accepting a lock. No rebate is invented.
    """
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return 0.07 * p * (1.0 - p)


def pair_curve_fee(yes_ask: float, no_ask: float) -> float:
    return kalshi_taker_fee(yes_ask) + kalshi_taker_fee(no_ask)


def lock_edge(
    yes_ask: float | None,
    no_ask: float | None,
    apply_curve_fee: bool = True,
) -> float | None:
    """Locked edge per contract pair: 1 - yes_ask - no_ask - fees.

    Fees are the per-side curve when ``apply_curve_fee`` is true.
    """
    if yes_ask is None or no_ask is None:
        return None
    if yes_ask <= 0 or no_ask <= 0:
        return None
    fee = pair_curve_fee(yes_ask, no_ask) if apply_curve_fee else 0.0
    return 1.0 - (yes_ask + no_ask) - fee


def quoted_ask(ask: float | None) -> bool:
    return ask is not None and 0.0 < ask < 1.0


def has_ask_size(ask: float | None, size: float | None) -> bool:
    if not quoted_ask(ask):
        return False
    if size is None:
        return True
    return size > 0


def can_pair_complete(row: "ScanRow", min_lock: float, apply_curve_fee: bool) -> bool:
    """True only when post-fee lock_edge > min_lock and both asks have size."""
    edge = row.lock_edge
    if edge is None:
        edge = lock_edge(row.yes_ask, row.no_ask, apply_curve_fee)
    if edge is None or edge <= min_lock:
        return False
    return has_ask_size(row.yes_ask, row.yes_ask_size) and has_ask_size(
        row.no_ask, row.no_ask_size
    )
