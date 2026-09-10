from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from stablebot.config import AppConfig
from stablebot.market.book import Quote, as_utc


@dataclass(frozen=True)
class DepegAlert:
    venue: str
    pair: str
    base: str
    quote: str
    mid: float
    peg: float
    deviation_bps: float
    ts: datetime


def deviation_bps(price: float, peg: float = 1.0) -> float:
    if peg == 0:
        return float("inf")
    return (price - peg) / peg * 10_000.0


def find_depegs(quotes: list[Quote], cfg: AppConfig) -> list[DepegAlert]:
    threshold = cfg.strategy.depeg_bps
    alerts: list[DepegAlert] = []
    for q in quotes:
        peg = cfg.watchlist.peg_target(q.base, q.quote)
        if peg is None:
            continue
        mid = q.mid
        if mid is None or mid <= 0:
            continue
        dev = deviation_bps(mid, peg)
        if abs(dev) < threshold:
            continue
        alerts.append(
            DepegAlert(
                venue=q.venue,
                pair=q.pair,
                base=q.base,
                quote=q.quote,
                mid=mid,
                peg=peg,
                deviation_bps=dev,
                ts=as_utc(q.ts),
            )
        )
    alerts.sort(key=lambda a: abs(a.deviation_bps), reverse=True)
    return alerts
