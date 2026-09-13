#!/usr/bin/env python3
"""Snapshot forward prices for every token a watched wallet touched.

Copy P&L needs the price path after the signal, not just at it, so this polls
DexScreener for each seen mint and appends a timestamped snapshot. Evaluation
then measures a copy entered at detection and exited at fixed horizons.
"""
from __future__ import annotations
import asyncio, json, time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache"
TRADES = CACHE / "kol_trades.jsonl"
SNAPS = CACHE / "kol_prices.jsonl"
DEX = "https://api.dexscreener.com/latest/dex/tokens/"
MAX_AGE_H = 26.0        # stop tracking a mint a day after it was last seen


def mints_of_interest():
    out = {}
    if not TRADES.exists():
        return out
    for l in TRADES.open():
        try:
            r = json.loads(l)
        except Exception:
            continue
        m = r.get("mint")
        if m:
            out[m] = max(out.get(m, 0), r.get("detect_ts") or 0)
    now = time.time()
    return {m: t for m, t in out.items() if now - t < MAX_AGE_H * 3600}


async def snap(c, mint):
    try:
        r = await c.get(DEX + mint, timeout=15)
        if r.status_code != 200:
            return None
        pairs = [p for p in (r.json() or {}).get("pairs") or []
                 if p.get("chainId") == "solana"]
        if not pairs:
            return None
        p = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
        return {"mint": mint, "ts": time.time(),
                "price_usd": float(p.get("priceUsd") or 0) or None,
                "liq_usd": float((p.get("liquidity") or {}).get("usd") or 0),
                "symbol": (p.get("baseToken") or {}).get("symbol")}
    except Exception:
        return None


async def main(cycle=120.0):
    print("price tracker started", flush=True)
    async with httpx.AsyncClient(timeout=20,
                                 limits=httpx.Limits(max_connections=8)) as c:
        # One semaphore for the process: it only caps concurrency, and rebuilding
        # it per cycle made every closure below capture a loop variable.
        sem = asyncio.Semaphore(5)

        async def one(m):
            async with sem:
                return await snap(c, m)

        while True:
            mints = list(mints_of_interest())
            if mints:
                res = await asyncio.gather(*(one(m) for m in mints))
                n = 0
                with SNAPS.open("a") as f:
                    for r in res:
                        if r:
                            f.write(json.dumps(r, separators=(",", ":")) + "\n"); n += 1
                print(f"[snapped {n}/{len(mints)} mints]", flush=True)
            await asyncio.sleep(cycle)

asyncio.run(main())
