#!/usr/bin/env python3
"""Dense sampler: live book vs prices-history, every 15s across live windows.

Produces the paired observations needed to quantify how far the historical
price series sits from the ask a taker would actually pay, and in which
direction relative to the side a spot move favours.
"""
from __future__ import annotations
import asyncio, json, sys, time
from pathlib import Path
import httpx

H = {"User-Agent": "stablebot/0.1 (research paper-trading; no live orders)",
     "Accept": "application/json"}
COINS = ["btc", "eth", "sol", "xrp", "doge", "bnb"]
OUT = Path(__file__).resolve().parents[2] / "data" / "bt_cache" / "lag_dense.jsonl"
VISION = "https://data-api.binance.vision/api/v3/klines"
SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
       "xrp": "XRPUSDT", "doge": "DOGEUSDT", "bnb": "BNBUSDT"}


async def jget(c, url, params=None, tries=2):
    for _ in range(tries):
        try:
            r = await c.get(url, params=params)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return None


async def one(c, coin, cache):
    now = time.time()
    ws = int(now // 300) * 300
    slug = f"{coin}-updown-5m-{ws}"
    ent = cache.get(slug)
    if ent is None:
        ev = await jget(c, "https://gamma-api.polymarket.com/events", {"slug": slug})
        mk = (ev[0].get("markets") or [None])[0] if ev else None
        if not mk:
            return None
        ids = json.loads(mk.get("clobTokenIds") or "[]")
        outs = json.loads(mk.get("outcomes") or "[]")
        if len(ids) != 2 or len(outs) != len(ids):
            return None
        ent = {o.strip().lower(): t for o, t in zip(outs, ids, strict=True)}
        cache[slug] = ent
    # spot move so far this window (which side the desk would favour)
    kl = await jget(c, VISION, {"symbol": SYM[coin], "interval": "1m",
                                "startTime": int(ws) * 1000, "limit": 6})
    fav = None
    if kl and len(kl) >= 1:
        o = float(kl[0][1]); last = float(kl[-1][4])
        fav = "up" if last > o else "down"
    rec = {"coin": coin, "slug": slug, "t": now, "elapsed": now - ws, "fav": fav}
    for side in ("up", "down"):
        bk = await jget(c, "https://clob.polymarket.com/book", {"token_id": ent[side]})
        if not bk:
            continue
        asks = [float(x["price"]) for x in (bk.get("asks") or [])]
        bids = [float(x["price"]) for x in (bk.get("bids") or [])]
        h = await jget(c, "https://clob.polymarket.com/prices-history",
                       {"market": ent[side], "startTs": ws - 60,
                        "endTs": int(now), "fidelity": 1})
        pts = (h or {}).get("history", [])
        rec[side] = {"ask": min(asks) if asks else None,
                     "bid": max(bids) if bids else None,
                     "hist": pts[-1]["p"] if pts else None,
                     "hist_t": pts[-1]["t"] if pts else None}
    return rec


async def main(mins):
    end = time.time() + mins * 60
    cache = {}
    n = 0
    async with httpx.AsyncClient(headers=H, timeout=20,
                                 limits=httpx.Limits(max_connections=24)) as c:
        with OUT.open("a") as f:
            while time.time() < end:
                recs = await asyncio.gather(*(one(c, k, cache) for k in COINS),
                                            return_exceptions=True)
                for r in recs:
                    if isinstance(r, dict):
                        f.write(json.dumps(r, separators=(",", ":")) + "\n"); n += 1
                f.flush()
                if n % 60 < 6:
                    print(f"{n} samples ({time.strftime('%H:%M:%S')})", flush=True)
                await asyncio.sleep(15)
    print(f"done {n} samples")

asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 25))
