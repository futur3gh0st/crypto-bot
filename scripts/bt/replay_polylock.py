#!/usr/bin/env python3
"""Poly pair-complete (lock) assessment over a stratified sample of the period.

Three fill rules, strictest last:
  naive   the shipped replay: aligned prints within 15s (what poly-backtest does)
  strict  both prints share a timestamp, so no stale side manufactures the gap
  real    strict, plus the measured per-side premium between the mid/last
          series and the ask a taker actually pays
"""
from __future__ import annotations
import asyncio, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from stablebot.poly.replay import (aligned_pairs, filter_window_history,
                                   poly_taker_fee, winning_side)

H = {"User-Agent": "stablebot/0.1 (research paper-trading; no live orders)",
     "Accept": "application/json"}
COINS = ["btc", "eth", "sol", "xrp", "doge", "bnb", "hype"]
MIN_LOCK = 0.03          # config.yaml poly.min_lock
OUT = ROOT / "data" / "bt_cache" / "polylock_sample.json"


async def jget(c, url, params, tries=5):
    for a in range(tries):
        try:
            r = await c.get(url, params=params)
        except (httpx.HTTPError, OSError):
            await asyncio.sleep(1.0 * (a + 1)); continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            await asyncio.sleep(1.2 * (a + 1)); continue
        return None
    return None


async def day(c, sem, d: datetime, minutes: int):
    base = int(d.replace(tzinfo=timezone.utc).timestamp())
    iv = minutes * 60
    starts = [base + iv * i for i in range(86400 // iv)]
    evs = []
    for coin in COINS:
        for i in range(0, len(starts), 100):
            q = [("slug", f"{coin}-updown-{minutes}m-{s}") for s in starts[i:i + 100]]
            q.append(("limit", "500"))
            j = await jget(c, "https://gamma-api.polymarket.com/events", q)
            if j:
                evs += [(coin, e) for e in j]

    async def hist(tok, s, e):
        async with sem:
            j = await jget(c, "https://clob.polymarket.com/prices-history",
                           {"market": tok, "startTs": s - 60, "endTs": e + 60,
                            "fidelity": 1})
            return (j or {}).get("history", [])

    meta, tasks = [], []
    for coin, e in evs:
        mk = (e.get("markets") or [None])[0]
        if not mk:
            continue
        ids = json.loads(mk.get("clobTokenIds") or "[]")
        if len(ids) != 2:
            continue
        s = int(e["slug"].rsplit("-", 1)[1])
        meta.append((coin, e["slug"], s, s + iv, mk))
        tasks += [hist(ids[0], s, s + iv), hist(ids[1], s, s + iv)]
    res = await asyncio.gather(*tasks) if tasks else []

    out = {"windows": 0, "naive": 0, "strict": 0, "real": 0,
           "naive_pnl": 0.0, "strict_pnl": 0.0, "real_pnl": 0.0}
    shares = 20.0
    for i, (coin, slug, s, e, mk) in enumerate(meta):
        if winning_side(mk.get("outcomes"), mk.get("outcomePrices")) is None:
            continue
        out["windows"] += 1
        up = filter_window_history(res[2 * i], s, e)
        dn = filter_window_history(res[2 * i + 1], s, e)
        prs = [p for p in aligned_pairs(up, dn) if 0 < p.p_up < 1 and 0 < p.p_down < 1]
        for label, sel, prem in (("naive", prs, 0.0),
                                 ("strict", [p for p in prs if p.t_up == p.t_down], 0.0),
                                 ("real", [p for p in prs if p.t_up == p.t_down], 0.005)):
            hit = None; hit_t = None
            for p in sel:
                a_up, a_dn = p.p_up + prem, p.p_down + prem
                edge = 1.0 - a_up - a_dn - poly_taker_fee(a_up) - poly_taker_fee(a_dn)
                if edge > MIN_LOCK:
                    hit = edge; hit_t = p.t; break
            if hit is not None:
                out[label] += 1
                out[label + "_pnl"] += shares * hit
                if label == "real":
                    out.setdefault("stream", []).append(
                        {"in": hit_t, "out": e, "edge": hit})
    return out


async def main():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    span = (datetime(2026, 9, 9, tzinfo=timezone.utc) - start).days
    if len(sys.argv) > 1 and sys.argv[1] == "full":
        days = [start + timedelta(days=i) for i in range(span + 1)]
    else:
        n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 20
        days = [start + timedelta(days=int(round(i * span / (n_days - 1)))) for i in range(n_days)]
    n_days = len(days)
    tot = {"windows": 0, "naive": 0, "strict": 0, "real": 0,
           "naive_pnl": 0.0, "strict_pnl": 0.0, "real_pnl": 0.0}
    async with httpx.AsyncClient(headers=H, timeout=40,
                                 limits=httpx.Limits(max_connections=48)) as c:
        sem = asyncio.Semaphore(32)
        stream = []
        B = 8
        for i in range(0, len(days), B):
            batch = days[i:i + B]
            rs = await asyncio.gather(*(day(c, sem, d, 15) for d in batch))
            for d, r in zip(batch, rs, strict=True):
                for k in tot:
                    tot[k] += r.get(k, 0) if not isinstance(tot[k], list) else 0
                stream += r.get("stream", [])
                print(f"  {d:%Y-%m-%d}: windows={r['windows']:4d} "
                      f"naive={r['naive']:3d} strict={r['strict']:3d} real={r['real']:3d}",
                      flush=True)
        with (ROOT / "data" / "bt_cache" / "polylock_stream.jsonl").open("w") as fh:
            for x in sorted(stream, key=lambda z: z["in"]):
                fh.write(json.dumps(x, separators=(",", ":")) + "\n")
        print(f"[wrote {len(stream)} lock fills to polylock_stream.jsonl]")
    w = max(tot["windows"], 1)
    print(f"\nSAMPLED {tot['windows']} resolved 15m windows across {n_days} days "
          f"(Jan 1 - Sep 9), {len(COINS)} coins")
    for k in ("naive", "strict", "real"):
        print(f"  {k:6}: {tot[k]:5d} locks  ({100*tot[k]/w:5.2f}% of windows)  "
              f"sample PnL={tot[k+'_pnl']:+,.2f}")
    OUT.write_text(json.dumps({"days": n_days, **tot}, indent=2))

asyncio.run(main())
