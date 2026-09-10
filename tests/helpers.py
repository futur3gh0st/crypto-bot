from __future__ import annotations

from datetime import datetime, timezone

from stablebot.config import AppConfig, StrategyCfg, VenueCfg
from stablebot.market.book import Quote


def make_cfg(**kwargs) -> AppConfig:
    cfg = AppConfig()
    cfg.venues = {
        "binance": VenueCfg(taker_fee_bps=10.0),
        "bybit": VenueCfg(taker_fee_bps=10.0),
        "kraken": VenueCfg(taker_fee_bps=26.0),
        "coinbase": VenueCfg(taker_fee_bps=60.0),
    }
    cfg.strategy = StrategyCfg(min_edge_bps=8.0, depeg_bps=30.0, hist_half_spread_bps=1.0)
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def q(venue: str, base: str, quote: str, bid: float, ask: float) -> Quote:
    return Quote(
        venue=venue,
        base=base,
        quote=quote,
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        ts=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
