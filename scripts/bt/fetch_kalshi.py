#!/usr/bin/env python3
"""Fetch settled Kalshi 15m crypto markets + per-minute candlesticks for the backtest.

Candlesticks carry real yes_bid / yes_ask, and Kalshi derives the other side's
ask from the opposite bid (no_ask = 1 - yes_bid), so this is a real book, not a
mid proxy.
"""
from __future__ import annotations

import asyncio, json, sys, time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache" / "kalshi"
CACHE.mkdir(parents=True, exist_ok=True)
H = "https://api.elections.kalshi.com/trade-api/v2"
UA = "stablebot/0.1 (research paper-trading; no live orders)"

COIN_SERIES = {
    "btc": "KXBTC15M", "eth": "KXETH15M", "sol": "KXSOL15M", "xrp": "KXXRP15M",
    "doge": "KXDOGE15M", "bnb": "KXBNB15M", "ada": "KXADA15M", "bch": "KXBCH15M",
    "near": "KXNEAR15M", "ton": "KXTON15M", "zec": "KXZEC15M",
}


def ts(d: str) -> int:
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp())


async def getj(c, path, params, tries=6):
    for a in range(tries):
        try:
            r = await c.get(H + path, params=params)
        except (httpx.HTTPError, OSError):
            await asyncio.sleep(1.5 * (a + 1)); continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            await asyncio.sleep(1.5 * (a + 1)); continue
        return None
    return None


async def list_markets(c, series: str, lo: int, hi: int) -> list[dict]:
    out, cursor = [], None
    while True:
        p = {"series_ticker": series, "min_close_ts": lo, "max_close_ts": hi,
             "status": "settled", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        d = await getj(c, "/markets", p)
        if not d:
            break
        ms = d.get("markets") or []
        out += ms
        cursor = d.get("cursor")
        if not cursor or not ms:
            break
    return out


async def main():
    lo, hi = ts(sys.argv[1]), ts(sys.argv[2])
    async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept": "application/json"},
                                 timeout=45, limits=httpx.Limits(max_connections=24)) as c:
        for coin, series in COIN_SERIES.items():
            mpath = CACHE / f"{series}_markets.jsonl"
            if mpath.exists() and mpath.stat().st_size > 0:
                n = sum(1 for _ in mpath.open())
                print(f"{series}: cached {n} markets", flush=True)
                continue
            t0 = time.time()
            ms = await list_markets(c, series, lo, hi)
            with mpath.open("w") as f:
                for m in ms:
                    f.write(json.dumps(m, separators=(",", ":")) + "\n")
            print(f"{series}: {len(ms)} settled markets in {time.time()-t0:.0f}s", flush=True)


asyncio.run(main())
