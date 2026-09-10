from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from stablebot.config import AppConfig
from stablebot.exchanges.base import is_watch_pair
from stablebot.market.book import Quote

PRODUCTS_URL = "https://api.exchange.coinbase.com/products"
TICKER_URL = "https://api.exchange.coinbase.com/products/{pid}/ticker"


class CoinbaseClient:
    name = "coinbase"

    async def fetch_quotes(self, client: httpx.AsyncClient, cfg: AppConfig) -> list[Quote]:
        assets = {a.upper() for a in cfg.watchlist.all_assets()}
        resp = await client.get(PRODUCTS_URL)
        resp.raise_for_status()
        products = resp.json()
        wanted: list[tuple[str, str, str]] = []
        for p in products:
            if p.get("status") not in (None, "online"):
                # still allow trading_disabled=false online products
                if p.get("status") != "online":
                    continue
            base = str(p.get("base_currency") or "").upper()
            quote = str(p.get("quote_currency") or "").upper()
            pid = str(p.get("id") or "")
            if is_watch_pair(base, quote, cfg):
                wanted.append((pid, base, quote))

        sem = asyncio.Semaphore(8)

        async def one(pid: str, base: str, quote: str) -> Quote | None:
            async with sem:
                try:
                    r = await client.get(TICKER_URL.format(pid=pid))
                    if r.status_code != 200:
                        return None
                    row = r.json()
                    bid = float(row.get("bid") or 0)
                    ask = float(row.get("ask") or 0)
                    last = float(row.get("price") or 0) or None
                except Exception:  # noqa: BLE001
                    return None
            if bid <= 0 or ask <= 0:
                return None
            return Quote(
                venue=self.name,
                base=base,
                quote=quote,
                bid=bid,
                ask=ask,
                last=last,
                ts=datetime.now(timezone.utc),
            )

        results = await asyncio.gather(*(one(*w) for w in wanted))
        return [q for q in results if q is not None]
