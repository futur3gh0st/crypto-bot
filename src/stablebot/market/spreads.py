from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import combinations, permutations

from stablebot.config import AppConfig
from stablebot.market.book import Quote, as_utc


@dataclass(frozen=True)
class SpreadOpportunity:
    kind: str  # cross_venue | cross_pair
    pair: str
    buy_venue: str
    sell_venue: str
    buy_base: str
    buy_quote: str
    sell_base: str
    sell_quote: str
    buy_px: float
    sell_px: float
    buy_fee_bps: float
    sell_fee_bps: float
    gross_bps: float
    fee_bps: float
    net_bps: float
    ts: datetime

    @property
    def label(self) -> str:
        if self.kind == "cross_venue":
            return f"{self.pair} buy {self.buy_venue} @ {self.buy_px:.6f} / sell {self.sell_venue} @ {self.sell_px:.6f}"
        return (
            f"buy {self.buy_base}/{self.buy_quote}@{self.buy_venue} {self.buy_px:.6f} / "
            f"sell {self.sell_base}/{self.sell_quote}@{self.sell_venue} {self.sell_px:.6f}"
        )


def gross_edge_bps(buy_ask: float, sell_bid: float) -> float:
    if buy_ask <= 0:
        return float("-inf")
    return (sell_bid - buy_ask) / buy_ask * 10_000.0


def net_edge_bps(
    buy_ask: float,
    sell_bid: float,
    buy_fee_bps: float,
    sell_fee_bps: float,
) -> float:
    """Taker-buy on one book, taker-sell on the other. Fees in bps of notional."""
    if buy_ask <= 0:
        return float("-inf")
    cost = buy_ask * (1.0 + buy_fee_bps / 10_000.0)
    proceeds = sell_bid * (1.0 - sell_fee_bps / 10_000.0)
    return (proceeds - cost) / cost * 10_000.0


def fee_drag_bps(buy_fee_bps: float, sell_fee_bps: float) -> float:
    """Approximate round-trip fee in bps of mid (informational)."""
    return buy_fee_bps + sell_fee_bps


def _usable(q: Quote) -> bool:
    return q.bid is not None and q.ask is not None and q.bid > 0 and q.ask > 0 and q.ask >= q.bid


def find_cross_venue(quotes: list[Quote], cfg: AppConfig) -> list[SpreadOpportunity]:
    by_pair: dict[str, list[Quote]] = {}
    for q in quotes:
        if _usable(q):
            by_pair.setdefault(q.pair, []).append(q)

    min_edge = cfg.strategy.min_edge_bps
    found: list[SpreadOpportunity] = []
    for pair, rows in by_pair.items():
        if len(rows) < 2:
            continue
        for buy, sell in permutations(rows, 2):
            if buy.venue == sell.venue:
                continue
            bf = cfg.fee_bps(buy.venue)
            sf = cfg.fee_bps(sell.venue)
            assert buy.ask is not None and sell.bid is not None
            net = net_edge_bps(buy.ask, sell.bid, bf, sf)
            if net < min_edge:
                continue
            found.append(
                SpreadOpportunity(
                    kind="cross_venue",
                    pair=pair,
                    buy_venue=buy.venue,
                    sell_venue=sell.venue,
                    buy_base=buy.base,
                    buy_quote=buy.quote,
                    sell_base=sell.base,
                    sell_quote=sell.quote,
                    buy_px=buy.ask,
                    sell_px=sell.bid,
                    buy_fee_bps=bf,
                    sell_fee_bps=sf,
                    gross_bps=gross_edge_bps(buy.ask, sell.bid),
                    fee_bps=fee_drag_bps(bf, sf),
                    net_bps=net,
                    ts=as_utc(buy.ts),
                )
            )
    found.sort(key=lambda x: x.net_bps, reverse=True)
    return found


def find_cross_pair(quotes: list[Quote], cfg: AppConfig) -> list[SpreadOpportunity]:
    """Buy a cheap USD-pegged stable, sell a rich one, same quote currency."""
    usd_pegged = {x.upper() for x in cfg.watchlist.usd_pegged}
    usable = [q for q in quotes if _usable(q) and q.base in usd_pegged]
    by_quote: dict[str, list[Quote]] = {}
    for q in usable:
        by_quote.setdefault(q.quote.upper(), []).append(q)

    min_edge = cfg.strategy.min_edge_bps
    found: list[SpreadOpportunity] = []
    for quote, rows in by_quote.items():
        if len(rows) < 2:
            continue
        for buy, sell in permutations(rows, 2):
            if buy.base == sell.base and buy.venue == sell.venue:
                continue
            if buy.base == sell.base:
                continue  # same asset is cross-venue
            bf = cfg.fee_bps(buy.venue)
            sf = cfg.fee_bps(sell.venue)
            assert buy.ask is not None and sell.bid is not None
            net = net_edge_bps(buy.ask, sell.bid, bf, sf)
            if net < min_edge:
                continue
            found.append(
                SpreadOpportunity(
                    kind="cross_pair",
                    pair=f"{buy.base}-{sell.base}/{quote}",
                    buy_venue=buy.venue,
                    sell_venue=sell.venue,
                    buy_base=buy.base,
                    buy_quote=buy.quote,
                    sell_base=sell.base,
                    sell_quote=sell.quote,
                    buy_px=buy.ask,
                    sell_px=sell.bid,
                    buy_fee_bps=bf,
                    sell_fee_bps=sf,
                    gross_bps=gross_edge_bps(buy.ask, sell.bid),
                    fee_bps=fee_drag_bps(bf, sf),
                    net_bps=net,
                    ts=as_utc(buy.ts),
                )
            )
    found.sort(key=lambda x: x.net_bps, reverse=True)
    return found


def find_opportunities(quotes: list[Quote], cfg: AppConfig) -> list[SpreadOpportunity]:
    seen: set[tuple] = set()
    out: list[SpreadOpportunity] = []
    for opp in [*find_cross_venue(quotes, cfg), *find_cross_pair(quotes, cfg)]:
        key = (
            opp.kind,
            opp.pair,
            opp.buy_venue,
            opp.sell_venue,
            opp.buy_base,
            opp.sell_base,
            round(opp.net_bps, 4),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(opp)
    out.sort(key=lambda x: x.net_bps, reverse=True)
    return out


# silence unused import if combinations is unused — keep for future
_ = combinations
