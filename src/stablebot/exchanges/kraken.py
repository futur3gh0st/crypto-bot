from __future__ import annotations

from datetime import datetime, timezone

import httpx

from stablebot.config import AppConfig
from stablebot.exchanges.base import ExchangeClient, is_watch_pair, split_concat_symbol
from stablebot.market.book import Quote

URL = "https://api.kraken.com/0/public/Ticker"

# Kraken sometimes prefixes fiat/crypto (ZUSD, XETH). Strip known wrappers.
_PREFIXES = ("X", "Z")


def _normalize_asset(raw: str, assets: set[str]) -> str | None:
    s = raw.upper()
    if s in assets:
        return s
    if len(s) > 3 and s[0] in _PREFIXES and s[1:] in assets:
        return s[1:]
    aliases = {"ZUSD": "USD", "ZEUR": "EUR", "USDT": "USDT", "USDC": "USDC"}
    if s in aliases and aliases[s] in assets:
        return aliases[s]
    return s if s in assets else None


def _split_kraken_pair(pair: str, assets: set[str]) -> tuple[str, str] | None:
    s = pair.upper().replace("/", "")
    # Prefer known assets over Kraken's XXBTZUSD-style names.
    parts = split_concat_symbol(s, assets)
    if parts:
        return parts
    # Try stripping X/Z prefixes from each half via longest quote match.
    ordered = sorted(assets, key=len, reverse=True)
    for quote in ordered:
        for qcand in (quote, "Z" + quote, "X" + quote):
            if s.endswith(qcand) and len(s) > len(qcand):
                raw_base = s[: -len(qcand)]
                base = _normalize_asset(raw_base, assets)
                qn = _normalize_asset(qcand, assets)
                if base and qn:
                    return base, qn
    return None


class KrakenClient(ExchangeClient):
    name = "kraken"

    async def fetch_quotes(self, client: httpx.AsyncClient, cfg: AppConfig) -> list[Quote]:
        resp = await client.get(URL)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            raise RuntimeError(f"kraken error: {payload['error']}")
        result = payload.get("result") or {}
        assets = set(cfg.watchlist.all_assets())
        now = datetime.now(timezone.utc)
        out: list[Quote] = []
        for pair, row in result.items():
            parts = _split_kraken_pair(str(pair), assets)
            if not parts:
                continue
            base, quote = parts
            if not is_watch_pair(base, quote, cfg):
                continue
            try:
                ask = float(row["a"][0])
                bid = float(row["b"][0])
                last = float(row["c"][0])
            except (KeyError, IndexError, TypeError, ValueError):
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
