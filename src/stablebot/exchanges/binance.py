from __future__ import annotations

from datetime import datetime, timezone

import httpx

from stablebot.config import AppConfig
from stablebot.exchanges.base import ExchangeClient, is_watch_pair, split_concat_symbol
from stablebot.market.book import Quote

BASES = (
    "https://api.binance.com",
    "https://data-api.binance.vision",
)


class BinanceClient(ExchangeClient):
    name = "binance"

    async def fetch_quotes(self, client: httpx.AsyncClient, cfg: AppConfig) -> list[Quote]:
        assets = cfg.watchlist.all_assets()
        last_err: Exception | None = None
        data = None
        for base in BASES:
            try:
                resp = await client.get(f"{base}/api/v3/ticker/bookTicker")
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        if data is None:
            raise last_err or RuntimeError("binance bookTicker failed")

        now = datetime.now(timezone.utc)
        out: list[Quote] = []
        if not isinstance(data, list):
            data = [data]
        for row in data:
            symbol = str(row.get("symbol") or "")
            parts = split_concat_symbol(symbol, assets)
            if not parts:
                continue
            base, quote = parts
            if not is_watch_pair(base, quote, cfg):
                continue
            try:
                bid = float(row["bidPrice"])
                ask = float(row["askPrice"])
            except (KeyError, TypeError, ValueError):
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
                    last=None,
                    ts=now,
                )
            )
        return out
