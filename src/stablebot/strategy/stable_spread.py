from __future__ import annotations

from dataclasses import dataclass, field

from stablebot.config import AppConfig
from stablebot.exchanges.base import fetch_all_quotes
from stablebot.market.book import Quote
from stablebot.market.depeg import DepegAlert, find_depegs
from stablebot.market.spreads import SpreadOpportunity, find_opportunities


def opp_key(opp: SpreadOpportunity) -> tuple:
    return (opp.kind, opp.pair, opp.buy_venue, opp.sell_venue, opp.buy_base, opp.sell_base)


def is_fillable(opp: SpreadOpportunity, cfg: AppConfig) -> bool:
    if opp.kind == "cross_pair" and not cfg.strategy.trade_cross_pair:
        return False
    return True


@dataclass
class ScanResult:
    quotes: list[Quote]
    opportunities: list[SpreadOpportunity]
    depegs: list[DepegAlert]
    errors: list[str] = field(default_factory=list)


async def run_scan(cfg: AppConfig) -> ScanResult:
    quotes, errors = await fetch_all_quotes(cfg)
    return ScanResult(
        quotes=quotes,
        opportunities=find_opportunities(quotes, cfg),
        depegs=find_depegs(quotes, cfg),
        errors=errors,
    )
