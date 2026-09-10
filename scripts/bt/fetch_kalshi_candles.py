#!/usr/bin/env python3
"""Fetch per-minute candlesticks (real yes_bid/yes_ask) for signal-firing markets."""
from __future__ import annotations
import asyncio, json, sys, time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache"
OUT = CACHE / "kalshilag_candles.jsonl"
H = "https://api.elections.kalshi.com/trade-api/v2"
UA = "stablebot/0.1 (research paper-trading; no live orders)"


async def main():
    want = {}
    for l in (CACHE / "kalshilag_candidates.jsonl").open():
        r = json.loads(l)
        k = (r["series"], r["ticker"])
        if k not in want:
            want[k] = (r["open_ts"], r["close_ts"])
    done = set()
    if OUT.exists():
        for l in OUT.open():
            try:
                d = json.loads(l); done.add((d["series"], d["ticker"]))
            except Exception:
                pass
    todo = [(k, v) for k, v in want.items() if k not in done]
    print(f"unique markets={len(want)} cached={len(done)} todo={len(todo)}", flush=True)
    sem = asyncio.Semaphore(64)
    t0 = time.time(); n = 0
    async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept": "application/json"},
                                 timeout=40, limits=httpx.Limits(max_connections=80)) as c:
        async def one(series, ticker, o, cl):
            async with sem:
                for a in range(5):
                    try:
                        r = await c.get(f"{H}/series/{series}/markets/{ticker}/candlesticks",
                                        params={"start_ts": int(o) - 60, "end_ts": int(cl) + 60,
                                                "period_interval": 1})
                    except (httpx.HTTPError, OSError):
                        await asyncio.sleep(1.0 * (a + 1)); continue
                    if r.status_code == 200:
                        return r.json().get("candlesticks", [])
                    if r.status_code in (429, 500, 502, 503, 504):
                        await asyncio.sleep(1.2 * (a + 1)); continue
                    return []
                return []
        with OUT.open("a") as f:
            B = 400
            for i in range(0, len(todo), B):
                chunk = todo[i:i + B]
                res = await asyncio.gather(*(one(k[0], k[1], v[0], v[1]) for k, v in chunk))
                for (k, _v), cs in zip(chunk, res):
                    rows = []
                    for x in cs:
                        ya = (x.get("yes_ask") or {}).get("close_dollars")
                        yb = (x.get("yes_bid") or {}).get("close_dollars")
                        if ya is None or yb is None:
                            continue
                        rows.append([int(x["end_period_ts"]), float(ya), float(yb)])
                    f.write(json.dumps({"series": k[0], "ticker": k[1], "c": rows},
                                       separators=(",", ":")) + "\n")
                f.flush(); n += len(chunk)
                el = time.time() - t0
                print(f"  {n}/{len(todo)}  {n/max(el,1e-9):.0f}/s  "
                      f"eta {(len(todo)-n)/max(n/max(el,1e-9),1e-9)/60:.1f}m", flush=True)
    print("done")

asyncio.run(main())
