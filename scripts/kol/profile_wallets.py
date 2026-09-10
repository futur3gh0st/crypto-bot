#!/usr/bin/env python3
"""Profile the frozen KOL roster: who is actually followable?

A wallet firing tens of thousands of mostly-reverting transactions an hour is a
sniper bot, not a trader you can copy — by the time its fill is visible on
chain you are behind by the edge it is extracting. This measures transaction
rate, failure rate and recency per wallet so the forward test tracks only
wallets a human-speed copier could plausibly follow.

Public RPC only. No keys, no orders.
"""
from __future__ import annotations
import asyncio, json, time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache"
OUT = CACHE / "kol_profile.json"
RPC = "https://api.mainnet-beta.solana.com"


async def rpc(c, method, params, tries=6):
    for a in range(tries):
        try:
            r = await c.post(RPC, json={"jsonrpc": "2.0", "id": 1,
                                        "method": method, "params": params})
        except (httpx.HTTPError, OSError):
            await asyncio.sleep(1.0 + a); continue
        if r.status_code == 429:
            await asyncio.sleep(2.0 + 2 * a); continue
        if r.status_code != 200:
            await asyncio.sleep(1.0 + a); continue
        j = r.json()
        if "error" in j:
            msg = str(j["error"])
            if "limit" in msg.lower() or "429" in msg:
                await asyncio.sleep(2.0 + 2 * a); continue
            return None
        return j.get("result")
    return None


async def profile(c, sem, wallet, meta):
    async with sem:
        sigs = await rpc(c, "getSignaturesForAddress", [wallet, {"limit": 200}])
    if not sigs:
        return {"wallet": wallet, "name": meta["name"], "ok": False}
    ok = [s for s in sigs if not s.get("err")]
    times = [s["blockTime"] for s in sigs if s.get("blockTime")]
    now = time.time()
    span_h = (max(times) - min(times)) / 3600.0 if len(times) > 1 else 0.0
    rate = (len(sigs) / span_h) if span_h > 0 else 0.0
    return {"wallet": wallet, "name": meta["name"], "twitter": meta.get("twitter"),
            "ok": True, "n": len(sigs), "fail_rate": 1 - len(ok) / len(sigs),
            "tx_per_hour": rate,
            "last_seen_h_ago": (now - max(times)) / 3600.0 if times else None,
            "span_hours": span_h}


async def main():
    roster = json.loads((CACHE / "kol_wallets.json").read_text())
    print(f"profiling {len(roster)} wallets on public RPC (this is rate-limited)…",
          flush=True)
    sem = asyncio.Semaphore(6)
    out = []
    async with httpx.AsyncClient(timeout=40,
                                 limits=httpx.Limits(max_connections=12)) as c:
        items = list(roster.items())
        B = 60
        for i in range(0, len(items), B):
            batch = items[i:i + B]
            res = await asyncio.gather(*(profile(c, sem, w, m) for w, m in batch))
            out += [r for r in res if r]
            print(f"  {min(i+B, len(items))}/{len(items)}", flush=True)
    OUT.write_text(json.dumps(out, indent=1))

    good = [r for r in out if r.get("ok")]
    print(f"\nresponded: {len(good)}/{len(out)}")
    if not good:
        return
    bots = [r for r in good if r["tx_per_hour"] > 500 or r["fail_rate"] > 0.5]
    quiet = [r for r in good if r.get("last_seen_h_ago") is not None
             and r["last_seen_h_ago"] > 72]
    followable = [r for r in good if r not in bots and r not in quiet]
    print(f"  bot-like (>500 tx/h or >50% reverts): {len(bots)}")
    print(f"  dormant (>72h since last tx)        : {len(quiet)}")
    print(f"  FOLLOWABLE                          : {len(followable)}")
    followable.sort(key=lambda r: -r["tx_per_hour"])
    print("\n  most active followable wallets:")
    for r in followable[:12]:
        print(f"    {r['name'][:20]:<20} {r['tx_per_hour']:>8.1f} tx/h  "
              f"fail {100*r['fail_rate']:>4.0f}%  last {r['last_seen_h_ago']:.1f}h ago")
    json.dump([r["wallet"] for r in followable],
              (CACHE / "kol_followable.json").open("w"), indent=1)
    print(f"\n[saved {len(followable)} followable wallets to kol_followable.json]")

asyncio.run(main())
