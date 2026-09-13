#!/usr/bin/env python3
"""Fetch Polymarket event + price history for each spot_lag signal candidate.

No lookahead: we keep the whole window's prints but the replay may only use
prints at t >= signal_ts to price the entry, and outcomePrices for resolution.
"""
from __future__ import annotations

import asyncio, json, time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache"
CAND = CACHE / "spotlag_candidates.jsonl"
OUT = CACHE / "spotlag_poly.jsonl"

H = {"User-Agent": "stablebot/0.1 (research paper-trading; no live orders)",
     "Accept": "application/json"}
GAMMA = "https://gamma-api.polymarket.com/events"
HIST = "https://clob.polymarket.com/prices-history"


async def gget(c, params, tries=6):
    for a in range(tries):
        try:
            r = await c.get(GAMMA, params=params)
        except (httpx.HTTPError, OSError):
            await asyncio.sleep(1.5 * (a + 1)); continue
        if r.status_code == 200:
            return r.json()
        await asyncio.sleep(1.5 * (a + 1))
    return None


async def main():
    cands = [json.loads(l) for l in CAND.open()]
    done = set()
    if OUT.exists():
        for l in OUT.open():
            try:
                done.add(json.loads(l)["slug"])
            except Exception:
                pass
    todo = [c for c in cands if c["slug"] not in done]
    print(f"candidates={len(cands)} cached={len(done)} todo={len(todo)}", flush=True)

    async with httpx.AsyncClient(headers=H, timeout=40,
                                 limits=httpx.Limits(max_connections=64)) as c:
        sem = asyncio.Semaphore(40)

        async def hist(tok, s, e):
            async with sem:
                for a in range(5):
                    try:
                        r = await c.get(HIST, params={"market": tok, "startTs": s - 60,
                                                      "endTs": e + 60, "fidelity": 1})
                    except (httpx.HTTPError, OSError):
                        await asyncio.sleep(1.0 * (a + 1)); continue
                    if r.status_code == 200:
                        return r.json().get("history", [])
                    if r.status_code == 429:
                        await asyncio.sleep(1.5 * (a + 1)); continue
                    return []
                return []

        t0 = time.time()
        with OUT.open("a") as f:
            for i in range(0, len(todo), 100):
                chunk = todo[i:i + 100]
                params = [("slug", x["slug"]) for x in chunk] + [("limit", "500")]
                evs = await gget(c, params) or []
                by_slug = {e.get("slug"): e for e in evs}
                tasks, meta = [], []
                for x in chunk:
                    e = by_slug.get(x["slug"])
                    mk = (e.get("markets") or [None])[0] if e else None
                    if not mk:
                        f.write(json.dumps({"slug": x["slug"], "ok": False,
                                            "why": "no event"}) + "\n")
                        continue
                    ids = json.loads(mk.get("clobTokenIds") or "[]")
                    outs = json.loads(mk.get("outcomes") or "[]")
                    if len(ids) != 2 or len(outs) != 2:
                        f.write(json.dumps({"slug": x["slug"], "ok": False,
                                            "why": "bad tokens"}) + "\n")
                        continue
                    if len(outs) != len(ids):
                        f.write(json.dumps({"slug": x["slug"], "ok": False,
                                            "why": "outcome/token length mismatch"}) + "\n")
                        continue
                    tok = {o.strip().lower(): t for o, t in zip(outs, ids, strict=True)}
                    side_tok = tok.get(x["side"])
                    if not side_tok:
                        f.write(json.dumps({"slug": x["slug"], "ok": False,
                                            "why": "no side token"}) + "\n")
                        continue
                    meta.append((x, mk))
                    tasks.append(hist(side_tok, x["window_start"], x["window_end"]))
                res = await asyncio.gather(*tasks) if tasks else []
                for (x, mk), h in zip(meta, res, strict=True):
                    f.write(json.dumps({
                        "slug": x["slug"], "ok": True,
                        "outcomes": mk.get("outcomes"),
                        "outcomePrices": mk.get("outcomePrices"),
                        "side": x["side"],
                        "hist": [[int(p["t"]), float(p["p"])] for p in h],
                    }, separators=(",", ":")) + "\n")
                f.flush()
                el = time.time() - t0
                dn = i + len(chunk)
                rate = dn / max(el, 1e-9)
                print(f"  {dn}/{len(todo)}  {rate:.0f}/s  eta {(len(todo)-dn)/max(rate,1e-9)/60:.1f}m",
                      flush=True)
    print("done")

asyncio.run(main())
