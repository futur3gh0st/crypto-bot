"""Pair-complete lock math and directional fade gates. No I/O."""

from __future__ import annotations

from stablebot.poly.markets import ScanRow


def lock_edge(
    ask_up: float | None,
    ask_down: float | None,
    taker_fee_bps: float = 0.0,
) -> float | None:
    """Locked edge per share pair: 1 - ask_up - ask_down - fee.

    fee is a flat pair-level haircut = taker_fee_bps / 10_000.
    Default 0 — do not invent a rebate. Official Polymarket crypto taker
    is a price-dependent curve (C x 0.07 x p x (1-p) as of 2026-08 docs)
    and is NOT applied here unless you encode it via taker_fee_bps.
    """
    if ask_up is None or ask_down is None:
        return None
    if ask_up <= 0 or ask_down <= 0:
        return None
    fee = taker_fee_bps / 10_000.0
    return 1.0 - (ask_up + ask_down) - fee


def quoted_ask(ask: float | None) -> bool:
    return ask is not None and 0.0 < ask < 1.0


def has_ask_size(ask: float | None, size: float | None) -> bool:
    """A live /price ask in (0, 1) counts; explicit size must be > 0 if given."""
    if not quoted_ask(ask):
        return False
    if size is None:
        return True
    return size > 0


def can_pair_complete(row: ScanRow, min_lock: float, taker_fee_bps: float) -> bool:
    edge = row.lock_edge
    if edge is None:
        edge = lock_edge(row.up_ask, row.down_ask, taker_fee_bps)
    if edge is None or edge <= min_lock:
        return False
    return has_ask_size(row.up_ask, row.up_ask_size) and has_ask_size(
        row.down_ask, row.down_ask_size
    )


def fade_side(row: ScanRow, threshold: float = 0.08) -> str | None:
    """Return 'up' or 'down' if that ask is >= threshold cheap vs crude fair.

    Directional. Off unless the caller enables fade. Requires an open print.
    """
    if row.fair_up is None:
        return None
    fair_up = row.fair_up
    fair_down = 1.0 - fair_up
    cheap_up = (
        quoted_ask(row.up_ask)
        and (fair_up - row.up_ask) >= threshold  # type: ignore[operator]
    )
    cheap_down = (
        quoted_ask(row.down_ask)
        and (fair_down - row.down_ask) >= threshold  # type: ignore[operator]
    )
    if cheap_up and not cheap_down:
        return "up"
    if cheap_down and not cheap_up:
        return "down"
    if cheap_up and cheap_down:
        # both cheap — pair-complete should have caught a lock; skip fade
        return None
    return None
