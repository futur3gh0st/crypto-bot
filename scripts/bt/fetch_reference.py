#!/usr/bin/env python3
"""Coinbase + Bitstamp 1m closes: the CF-style composite reference the
kalshi_lag sleeve prices against (MIN_SOURCES=2, so two venues = 'composite').

Kraken only serves ~720 recent 1m bars and Gemini little more, so neither can
be reconstructed historically; the live desk would poll up to four.
"""
from __future__ import annotations
import asyncio, json, sys, time
from datetime import datetime, timezone
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache" / "reference"
CACHE.mkdir(parents=True, exist_ok=True)
UA = "stablebot/0.1 (research paper-trading; no live orders)"

CB = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD", "xrp": "XRP-USD",
      "doge": "DOGE-USD", "bnb": "BNB-USD", "near": "NEAR-USD", "zec": "ZEC-USD"}
BS = {"btc": "btcusd", "eth": "ethusd", "sol": "solusd", "xrp": "xrpusd",
      "doge": "dogeusd", "bnb": "bnbusd", "near": "nearusd", "zec": "zecusd"}


def ep(d):
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp())


async def get(c, url, params, tries=6):
    for a in range(tries):
        try:
            r = await c.get(url, params=params)
        except (httpx.HTTPError, OSError):
            await asyncio.sleep(1.0 * (a + 1)); continue
        if r.status_code == 200:
            return r.json()
        await asyncio.sleep(1.0 * (a + 1))
    return None


async def coinbase(c, coin, lo, hi):
    out = {}
    sym = CB[coin]
    step = 300 * 60
    t = lo
    while t < hi:
        end = min(t + step, hi)
        d = await get(c, f"https://api.exchange.coinbase.com/products/{sym}/candles",
                      {"granularity": 60, "start": t, "end": end})
        if isinstance(d, list):
            for row in d:
                out[int(row[0])] = float(row[4])   # [t, low, high, open, close, vol]
        t = end
        await asyncio.sleep(0.12)
    return out


async def bitstamp(c, coin, lo, hi):
    out = {}
    sym = BS[coin]
    t = lo
    while t < hi:
        d = await get(c, f"https://www.bitstamp.net/api/v2/ohlc/{sym}/",
                      {"step": 60, "limit": 1000, "start": t})
        rows = ((d or {}).get("data") or {}).get("ohlc") or []
        if not rows:
            break
        for r in rows:
            out[int(r["timestamp"])] = float(r["close"])
        last = int(rows[-1]["timestamp"])
        if last <= t:
            break
        t = last + 60
        await asyncio.sleep(0.12)
    return out


async def main():
    lo, hi = ep(sys.argv[1]), ep(sys.argv[2])
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=40,
                                 limits=httpx.Limits(max_connections=8)) as c:
        for coin in CB:
            p = CACHE / f"{coin}.json"
            if p.exists():
                print(f"{coin}: cached"); continue
            t0 = time.time()
            cb, bs = await asyncio.gather(coinbase(c, coin, lo, hi),
                                          bitstamp(c, coin, lo, hi))
            p.write_text(json.dumps({"coinbase": cb, "bitstamp": bs}))
            print(f"{coin}: coinbase={len(cb)} bitstamp={len(bs)} "
                  f"in {time.time()-t0:.0f}s", flush=True)

asyncio.run(main())
