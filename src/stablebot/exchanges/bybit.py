from __future__ import annotations

from datetime import datetime, timezone

import httpx

from stablebot.config import AppConfig
from stablebot.exchanges.base import is_watch_pair, split_concat_symbol
from stablebot.market.book import Quote

URLS = (
    "https://api.bybit.com/v5/market/tickers",
    "https://api.bytick.com/v5/market/tickers",
)


class BybitClient:
    name = "bybit"

    async def fetch_quotes(self, client: httpx.AsyncClient, cfg: AppConfig) -> list[Quote]:
        last_err: Exception | None = None
        payload = None
        for url in URLS:
            try:
                resp = await client.get(url, params={"category": "spot"})
                resp.raise_for_status()
                payload = resp.json()
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        if payload is None:
            raise last_err or RuntimeError("bybit tickers failed")
        rows = ((payload.get("result") or {}).get("list")) or []
        assets = cfg.watchlist.all_assets()
        now = datetime.now(timezone.utc)
        out: list[Quote] = []
        for row in rows:
            symbol = str(row.get("symbol") or "")
            parts = split_concat_symbol(symbol, assets)
            if not parts:
                continue
            base, quote = parts
            if not is_watch_pair(base, quote, cfg):
                continue
            try:
                bid = float(row.get("bid1Price") or 0)
                ask = float(row.get("ask1Price") or 0)
                last = float(row.get("lastPrice") or 0) or None
            except (TypeError, ValueError):
                continue
            if bid <= 0 or ask <= 0:
                continue
            out.append(
                Quote(
                    venue=self.name,
                    base=base,
                    quote=quote,
                    bid=bid,
                    ask=ask,
                    last=last,
                    ts=now,
                )
            )
        return out
