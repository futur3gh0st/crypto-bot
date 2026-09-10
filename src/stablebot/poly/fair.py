"""Crude Up probability from spot vs window-open. Labeled crude — not a model."""

from __future__ import annotations


def crude_fair_up(spot: float, open_px: float, scale: float = 25.0) -> float:
    """Fair Up ≈ 0.50 at window open; clipped linear of return.

    scale=25 maps a 2% spot move to the 0.02/0.98 clip. This is a sketch,
    not a vol-aware or time-to-expiry model.
    """
    if open_px <= 0 or spot <= 0:
        raise ValueError("spot and open must be positive")
    ret = (spot - open_px) / open_px
    raw = 0.50 + scale * ret
    return min(0.98, max(0.02, raw))


def dislocation_bps(quoted: float, fair: float) -> float:
    """Positive => quoted Up is rich vs crude fair (in probability-bps)."""
    return (quoted - fair) * 10_000.0
