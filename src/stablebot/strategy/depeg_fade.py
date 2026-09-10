from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


def deviation_bps(price: float, peg: float = 1.0) -> float:
    if peg == 0:
        return float("inf")
    return (price - peg) / peg * 10_000.0


def cheap_enough(price: float, entry_bps: float = 35.0, peg: float = 1.0) -> bool:
    """True when the stable is at least `entry_bps` cheap vs peg (dev <= -entry)."""
    return deviation_bps(price, peg) <= -abs(entry_bps)


def consecutive_cheap(
    closes: list[float],
    n: int = 2,
    entry_bps: float = 35.0,
    peg: float = 1.0,
) -> bool:
    if n <= 0 or len(closes) < n:
        return False
    return all(cheap_enough(c, entry_bps, peg) for c in closes[-n:])


def near_peg(price: float, band_bps: float = 15.0, peg: float = 1.0) -> bool:
    """True when |deviation| is inside the peg band (default ±15 bps)."""
    return abs(deviation_bps(price, peg)) <= abs(band_bps)


def acute_entry(
    closes: list[float],
    n: int = 2,
    entry_bps: float = 35.0,
    band_bps: float = 15.0,
    lookback_hours: int = 24,
    peg: float = 1.0,
) -> bool:
    """Acute depeg only: near peg in the prior lookback, THEN last n bars cheap.

    Chronic discounts (TUSD sitting at −38 bps for weeks) never clear the
    near-peg lookback, so they do not enter.
    """
    if lookback_hours <= 0 or n <= 0:
        return False
    if len(closes) < lookback_hours:
        return False
    if not consecutive_cheap(closes, n, entry_bps, peg):
        return False
    window = closes[-lookback_hours:]
    return any(near_peg(c, band_bps, peg) for c in window)


def recovered(price: float, exit_bps: float = 10.0, peg: float = 1.0) -> bool:
    """True when discount has tightened to -exit_bps or better (including rich)."""
    return deviation_bps(price, peg) >= -abs(exit_bps)


def stopped_out(price: float, stop_bps: float = 150.0, peg: float = 1.0) -> bool:
    return deviation_bps(price, peg) <= -abs(stop_bps)


def time_stopped(entry_ts: datetime, now: datetime, max_hold_hours: int = 72) -> bool:
    return now - entry_ts >= timedelta(hours=max_hold_hours)


def exit_reason(
    price: float,
    entry_ts: datetime,
    now: datetime,
    exit_bps: float = 10.0,
    stop_bps: float = 150.0,
    max_hold_hours: int = 72,
    peg: float = 1.0,
) -> str | None:
    if stopped_out(price, stop_bps, peg):
        return "stop"
    if recovered(price, exit_bps, peg):
        return "repeg"
    if time_stopped(entry_ts, now, max_hold_hours):
        return "time"
    return None


def should_rearm(price: float, entry_bps: float = 35.0, peg: float = 1.0) -> bool:
    """After an exit, require the discount to first go *above* the entry threshold."""
    return deviation_bps(price, peg) > -abs(entry_bps)


@dataclass
class DepegPosition:
    asset: str
    pair: str
    venue: str
    entry_ts: datetime
    entry_px: float
    notional: float
    units: float
    entry_fee: float


def depeg_pnl(pos: DepegPosition, exit_px: float, exit_fee_bps: float) -> tuple[float, float]:
    """Mark-to-market + exit taker. Returns (pnl, exit_fee_usd)."""
    if pos.entry_px <= 0 or exit_px <= 0:
        return 0.0, 0.0
    exit_fee = pos.units * exit_px * (exit_fee_bps / 10_000.0)
    proceeds = pos.units * exit_px - exit_fee
    cost = pos.units * pos.entry_px + pos.entry_fee
    return proceeds - cost, exit_fee
