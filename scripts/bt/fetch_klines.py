#!/usr/bin/env python3
"""Bulk-download Binance 1m klines for the desk backtest. Cache to parquet-ish JSONL.

Public data-api.binance.vision only. No keys, no orders.
"""
from __future__ import annotations

import asyncio, json, sys, time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache" / "klines"
CACHE.mkdir(parents=True, exist_ok=True)

VISION = "https://data-api.binance.vision/api/v3/klines"
UA = "stablebot/0.1 (research paper-trading; no live orders)"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT"]


def ms(dt: str) -> int:
    return int(datetime.fromisoformat(dt).replace(tzinfo=timezone.utc).timestamp() * 1000)


async def fetch_symbol(c: httpx.AsyncClient, sym: str, start_ms: int, end_ms: int) -> int:
    out = CACHE / f"{sym}_1m.jsonl"
    have_last = 0
    if out.exists():
        with out.open() as f:
            for line in f:
                pass
            try:
                have_last = json.loads(line)[0]
            except Exception:
                have_last = 0
    cur = max(start_ms, have_last + 60_000) if have_last else start_ms
    if cur >= end_ms:
        print(f"  {sym}: cached through {datetime.fromtimestamp(have_last/1000, timezone.utc)}", flush=True)
        return 0
    n = 0
    mode = "a" if have_last else "w"
    with out.open(mode) as f:
        while cur < end_ms:
            r = None
            for attempt in range(8):
                try:
                    r = await c.get(VISION, params={"symbol": sym, "interval": "1m",
                                                    "startTime": cur, "endTime": end_ms, "limit": 1000})
                except (httpx.HTTPError, OSError) as exc:
                    r = None
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                if r.status_code == 200:
                    break
                await asyncio.sleep(2.0 * (attempt + 1))
            if r is None or r.status_code != 200:
                raise RuntimeError(f"{sym} stalled at {cur}")
            rows = r.json()
            if not rows:
                break
            for k in rows:
                # [openTime, open, high, low, close, vol, closeTime, ...]
                f.write(json.dumps([int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                                    float(k[4]), int(k[6])], separators=(",", ":")) + "\n")
            n += len(rows)
            cur = int(rows[-1][0]) + 60_000
            if len(rows) < 1000:
                break
    print(f"  {sym}: +{n} bars", flush=True)
    return n


async def main():
    start_ms, end_ms = ms(sys.argv[1]), ms(sys.argv[2])
    t0 = time.time()
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=40,
                                 limits=httpx.Limits(max_connections=6)) as c:
        total = 0
        for sym in SYMBOLS:
            total += await fetch_symbol(c, sym, start_ms, end_ms)
    print(f"done: {total} bars in {time.time()-t0:.0f}s")

asyncio.run(main())
