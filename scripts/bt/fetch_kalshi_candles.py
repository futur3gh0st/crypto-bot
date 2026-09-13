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


# Kalshi rate-limits this endpoint at roughly 5 requests/second. Measured: 4/s
# sustains 160/160 with zero 429s; 6/s loses 50 of 180; 10/s loses half. The
# original 64-way semaphore did not fetch faster, it simply converted the excess
# into 429s and backoff -- and, worse, into empty rows (see below).
RATE_PER_SEC = 4.0


class RateLimiter:
    """Token bucket. Serialises request starts so the venue is never flooded."""

    def __init__(self, per_sec: float):
        self._interval = 1.0 / per_sec
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            start = max(now, self._next)
            self._next = start + self._interval
        delay = start - now
        if delay > 0:
            await asyncio.sleep(delay)


async def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else CACHE / "kalshilag_candidates.jsonl"
    want = {}
    for l in src.open():
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
    limiter = RateLimiter(RATE_PER_SEC)
    t0 = time.time(); n = 0; unanswered = 0
    async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept": "application/json"},
                                 timeout=40, limits=httpx.Limits(max_connections=16)) as c:
        async def one(series, ticker, o, cl):
            """-> list of candlesticks, or None if we never got an answer.

            None and [] must stay distinct. [] means Kalshi served the market and
            it genuinely had no book; None means we gave up. Collapsing the two
            is what recorded ~40% of markets as bookless on the first pass and
            required a whole second script to undo -- so an exhausted budget now
            writes nothing at all, and the resume pass retries it.
            """
            for a in range(6):
                await limiter.wait()
                try:
                    r = await c.get(f"{H}/series/{series}/markets/{ticker}/candlesticks",
                                    params={"start_ts": int(o) - 60, "end_ts": int(cl) + 60,
                                            "period_interval": 1})
                except (httpx.HTTPError, OSError):
                    await asyncio.sleep(1.0 * (a + 1)); continue
                if r.status_code == 200:
                    return r.json().get("candlesticks", [])
                if r.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(1.5 * (a + 1)); continue
                return []          # a real "no", e.g. 404
            return None            # never answered -- do not record
        with OUT.open("a") as f:
            B = 400
            for i in range(0, len(todo), B):
                chunk = todo[i:i + B]
                res = await asyncio.gather(*(one(k[0], k[1], v[0], v[1]) for k, v in chunk))
                for (k, _v), cs in zip(chunk, res, strict=True):
                    if cs is None:
                        unanswered += 1
                        continue       # leave it absent so a rerun picks it up
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
                print(f"  {n}/{len(todo)}  {n/max(el,1e-9):.1f}/s  "
                      f"unanswered={unanswered}  "
                      f"eta {(len(todo)-n)/max(n/max(el,1e-9),1e-9)/60:.1f}m", flush=True)
    print(f"done -- {n - unanswered} recorded, {unanswered} unanswered (rerun to retry)")

asyncio.run(main())
