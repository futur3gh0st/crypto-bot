from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def iso(ts: datetime) -> str:
    return as_utc(ts).isoformat()


@dataclass(frozen=True)
class Quote:
    venue: str
    base: str
    quote: str
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    ts: datetime

    @property
    def pair(self) -> str:
        return f"{self.base}/{self.quote}"

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        if self.last is not None and self.last > 0:
            return self.last
        return None


def quotes_from_mid(
    venue: str,
    base: str,
    quote: str,
    mid: float,
    ts: datetime,
    half_spread_bps: float = 1.0,
) -> Quote:
    """Synthesize bid/ask around a mid (used for OHLC history)."""
    half = mid * (half_spread_bps / 10_000.0)
    return Quote(
        venue=venue,
        base=base,
        quote=quote,
        bid=mid - half,
        ask=mid + half,
        last=mid,
        ts=as_utc(ts),
    )
