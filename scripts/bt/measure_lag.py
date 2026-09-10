#!/usr/bin/env python3
"""Measure how stale CLOB prices-history is versus the live order book.

At ~60-90s into a live 5m window (exactly when the spot_lag sleeve decides),
record the live best ask, then re-read prices-history for that same instant.
The gap is the error the backtest inherits when it fills from history.
"""
from __future__ import annotations
import asyncio, json, sys, time
from pathlib import Path
import httpx

H = {"User-Agent": "stablebot/0.1 (research paper-trading; no live orders)",
     "Accept": "application/json"}
COINS = ["btc", "eth", "sol", "xrp", "doge", "bnb"]
OUT = Path(__file__).resolve().parents[2] / "data" / "bt_cache" / "lag_samples.jsonl"


async def jget(c, url, params=None, tries=3):
    for a in range(tries):
        try:
            r = await c.get(url, params=params)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        await asyncio.sleep(0.6)
    return None


async def sample(c, coin, minutes=5):
    now = time.time()
    ws = int(now // (minutes * 60)) * (minutes * 60)
    slug = f"{coin}-updown-{minutes}m-{ws}"
    ev = await jget(c, "https://gamma-api.polymarket.com/events", {"slug": slug})
    if not ev:
        return None
    mk = (ev[0].get("markets") or [None])[0]
    if not mk:
        return None
    ids = json.loads(mk.get("clobTokenIds") or "[]")
    outs = json.loads(mk.get("outcomes") or "[]")
    if len(ids) != 2:
        return None
    tok = {o.strip().lower(): t for o, t in zip(outs, ids)}
    rec = {"coin": coin, "slug": slug, "ws": ws, "t": now, "elapsed": now - ws}
    for side in ("up", "down"):
        bk = await jget(c, "https://clob.polymarket.com/book", {"token_id": tok[side]})
        if not bk:
            continue
        asks = [float(x["price"]) for x in (bk.get("asks") or [])]
        bids = [float(x["price"]) for x in (bk.get("bids") or [])]
        rec[f"{side}_ask"] = min(asks) if asks else None
        rec[f"{side}_bid"] = max(bids) if bids else None
        h = await jget(c, "https://clob.polymarket.com/prices-history",
                       {"market": tok[side], "startTs": ws - 60,
                        "endTs": int(now), "fidelity": 1})
        pts = (h or {}).get("history", [])
        rec[f"{side}_hist_last"] = pts[-1]["p"] if pts else None
        rec[f"{side}_hist_t"] = pts[-1]["t"] if pts else None
    return rec


async def main(minutes_to_run=16):
    end = time.time() + minutes_to_run * 60
    async with httpx.AsyncClient(headers=H, timeout=25,
                                 limits=httpx.Limits(max_connections=12)) as c:
        with OUT.open("a") as f:
            while time.time() < end:
                now = time.time()
                ws = int(now // 300) * 300
                target = ws + 75          # ~75s in: the sleeve's decision moment
                if target < now:
                    target = ws + 300 + 75
                await asyncio.sleep(max(0, target - time.time()))
                recs = await asyncio.gather(*(sample(c, k) for k in COINS))
                n = 0
                for r in recs:
                    if r:
                        f.write(json.dumps(r, separators=(",", ":")) + "\n"); n += 1
                f.flush()
                print(f"sampled {n} at elapsed~75s  ({time.strftime('%H:%M:%S')})", flush=True)
asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 16))
