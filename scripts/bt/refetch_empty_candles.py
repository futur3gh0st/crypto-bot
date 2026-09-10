#!/usr/bin/env python3
"""Re-fetch markets whose candlestick pull came back empty.

The first pass treated an exhausted retry budget the same as a genuinely
untraded market, so ~40% of markets were recorded as having no book. This
retries only those, patiently, and rewrites the cache.
"""
from __future__ import annotations
import asyncio, json, time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache"
SRC = CACHE / "kalshilag_candles.jsonl"
H = "https://api.elections.kalshi.com/trade-api/v2"
UA = "stablebot/0.1 (research paper-trading; no live orders)"


async def main():
    recs = [json.loads(l) for l in SRC.open()]
    bounds = {}
    for l in (CACHE / "kalshilag_candidates.jsonl").open():
        r = json.loads(l)
        bounds.setdefault(r["ticker"], (r["open_ts"], r["close_ts"]))
    todo = [r for r in recs if not (r.get("c") or [])]
    print(f"total={len(recs)}  empty={len(todo)}  refetching", flush=True)
    sem = asyncio.Semaphore(20)
    fixed = 0
    t0 = time.time()

    async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept": "application/json"},
                                 timeout=45, limits=httpx.Limits(max_connections=32)) as c:
        async def one(rec):
            nonlocal fixed
            b = bounds.get(rec["ticker"])
            if not b:
                return
            o, cl = int(b[0]), int(b[1])
            async with sem:
                for a in range(10):
                    try:
                        r = await c.get(
                            f"{H}/series/{rec['series']}/markets/{rec['ticker']}/candlesticks",
                            params={"start_ts": o - 60, "end_ts": cl + 60,
                                    "period_interval": 1})
                    except (httpx.HTTPError, OSError):
                        await asyncio.sleep(1.0 + 0.7 * a); continue
                    if r.status_code == 200:
                        rows = []
                        for x in r.json().get("candlesticks", []):
                            ya = (x.get("yes_ask") or {}).get("close_dollars")
                            yb = (x.get("yes_bid") or {}).get("close_dollars")
                            if ya is None or yb is None:
                                continue
                            rows.append([int(x["end_period_ts"]), float(ya), float(yb)])
                        if rows:
                            rec["c"] = rows; fixed += 1
                        return
                    if r.status_code in (429, 500, 502, 503, 504):
                        await asyncio.sleep(1.2 + 0.8 * a); continue
                    return
        B = 500
        for i in range(0, len(todo), B):
            await asyncio.gather(*(one(r) for r in todo[i:i + B]))
            el = time.time() - t0
            dn = min(i + B, len(todo))
            print(f"  {dn}/{len(todo)} fixed={fixed} "
                  f"eta {(len(todo)-dn)/max(dn/max(el,1e-9),1e-9)/60:.1f}m", flush=True)

    with SRC.open("w") as f:
        for r in recs:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    still = sum(1 for r in recs if not (r.get("c") or []))
    print(f"done: recovered {fixed}, still empty {still}")

asyncio.run(main())
