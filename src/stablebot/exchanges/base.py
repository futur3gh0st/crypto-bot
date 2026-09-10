from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

import httpx

from stablebot.config import AppConfig
from stablebot.market.book import Quote

USER_AGENT = "stablebot/0.1 (research paper-trading; no live orders)"
DEFAULT_TIMEOUT = 12.0


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(DEFAULT_TIMEOUT),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


def split_concat_symbol(symbol: str, assets: Iterable[str]) -> tuple[str, str] | None:
    """Split concatenated symbols. Prefer the longest base (USDT/USD not USD/TUSD)."""
    s = symbol.upper().replace("-", "").replace("_", "").replace("/", "")
    known = {a.upper() for a in assets}
    matches: list[tuple[str, str]] = []
    for quote in known:
        if s.endswith(quote) and len(s) > len(quote):
            base = s[: -len(quote)]
            if base in known:
                matches.append((base, quote))
    if not matches:
        return None
    matches.sort(key=lambda p: (len(p[0]), len(p[1])), reverse=True)
    return matches[0]


def is_watch_pair(base: str, quote: str, cfg: AppConfig) -> bool:
    """Keep STABLE/STABLE and STABLE/fiat. Drop EUR/USDT-style FX-only rows."""
    stables = {s.upper() for s in cfg.watchlist.stables}
    quotes = {s.upper() for s in cfg.watchlist.all_assets()}
    return base.upper() in stables and quote.upper() in quotes


class ExchangeClient(ABC):
    name: str

    @abstractmethod
    async def fetch_quotes(self, client: httpx.AsyncClient, cfg: AppConfig) -> list[Quote]:
        """Return quotes for watchlist pairs. Must not raise on venue errors."""


async def fetch_all_quotes(
    cfg: AppConfig,
    clients: list[ExchangeClient] | None = None,
) -> tuple[list[Quote], list[str]]:
    """Fetch every enabled venue. Returns (quotes, per-venue error notes)."""
    from stablebot.exchanges.binance import BinanceClient
    from stablebot.exchanges.bybit import BybitClient
    from stablebot.exchanges.coinbase import CoinbaseClient
    from stablebot.exchanges.kraken import KrakenClient

    if clients is None:
        clients = [BinanceClient(), CoinbaseClient(), KrakenClient(), BybitClient()]

    quotes: list[Quote] = []
    errors: list[str] = []
    async with new_client() as http:
        for ex in clients:
            if not cfg.venue_enabled(ex.name):
                continue
            try:
                batch = await ex.fetch_quotes(http, cfg)
                quotes.extend(batch)
            except Exception as exc:  # noqa: BLE001 — isolate venues
                errors.append(f"{ex.name}: {type(exc).__name__}: {exc}")
    return quotes, errors
