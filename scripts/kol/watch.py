#!/usr/bin/env python3
"""Forward paper test: follow the frozen KOL roster and record copyable trades.

The wallet list is fixed in advance (data/bt_cache/kol_wallets.json, pulled
2026-09-10), so nothing here is chosen with hindsight — dormant wallets and
wallets that blow up stay in the sample.

For every detected swap this records two clocks:

  blockTime    when their fill actually landed on chain
  detect_ts    when a follower polling public RPC could first have seen it

The gap between them is the latency wall, and it is the whole question for
copy trading. Entry price is taken at detection, never at their price.

Public RPC + DexScreener only. No keys, no orders, paper only.
"""
from __future__ import annotations
import asyncio, json, time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache"
TRADES = CACHE / "kol_trades.jsonl"
RPC = "https://api.mainnet-beta.solana.com"
DEX = "https://api.dexscreener.com/latest/dex/tokens/"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


async def rpc(c, method, params, tries=5):
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
            await asyncio.sleep(1.5 + a); continue
        return j.get("result")
    return None


def decode_swap(tx, owner):
    """Infer a swap from token-balance deltas. DEX-agnostic, so it survives
    whichever router they used."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return None
    keys = [k["pubkey"] if isinstance(k, dict) else k
            for k in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys", [])]
    try:
        idx = keys.index(owner)
    except ValueError:
        idx = None
    # SOL delta for the signer
    sol = 0.0
    if idx is not None:
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if idx < len(pre) and idx < len(post):
            sol = (post[idx] - pre[idx]) / 1e9
    # token deltas owned by this wallet
    def bal(entries):
        out = {}
        for b in entries or []:
            if b.get("owner") != owner:
                continue
            amt = ((b.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0
            out[b["mint"]] = out.get(b["mint"], 0.0) + float(amt)
        return out
    pre_t, post_t = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
    deltas = {}
    for m in set(pre_t) | set(post_t):
        d = post_t.get(m, 0.0) - pre_t.get(m, 0.0)
        if abs(d) > 1e-12 and m != WSOL and m not in STABLES:
            deltas[m] = d
    if not deltas:
        return None
    mint, d = max(deltas.items(), key=lambda kv: abs(kv[1]))
    # a buy costs SOL and gains the token
    if d > 0 and sol < -0.001:
        return {"mint": mint, "side": "buy", "token_amt": d, "sol_amt": -sol}
    if d < 0 and sol > 0.001:
        return {"mint": mint, "side": "sell", "token_amt": -d, "sol_amt": sol}
    return None


async def dex_price(c, mint):
    try:
        r = await c.get(DEX + mint, timeout=15)
        if r.status_code != 200:
            return None
        pairs = (r.json() or {}).get("pairs") or []
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol_pairs:
            return None
        p = max(sol_pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
        return {"price_usd": float(p.get("priceUsd") or 0) or None,
                "liq_usd": float((p.get("liquidity") or {}).get("usd") or 0),
                "pair_created": p.get("pairCreatedAt"),
                "symbol": (p.get("baseToken") or {}).get("symbol")}
    except Exception:
        return None


async def main(top_n=40, cycle=45.0, minutes=0):
    roster = json.loads((CACHE / "kol_wallets.json").read_text())
    fpath = CACHE / "kol_followable.json"
    if fpath.exists():
        wallets = json.loads(fpath.read_text())[:top_n]
    else:
        wallets = list(roster)[:top_n]
    names = {w: roster.get(w, {}).get("name", w[:6]) for w in wallets}
    print(f"watching {len(wallets)} wallets, cycle {cycle:.0f}s", flush=True)
    last: dict[str, str] = {}
    end = time.time() + minutes * 60 if minutes else None
    n_trades = 0
    sem = asyncio.Semaphore(5)

    async with httpx.AsyncClient(timeout=40,
                                 limits=httpx.Limits(max_connections=10)) as c:
        # prime: remember the current tip so we only record NEW activity
        for w in wallets:
            async with sem:
                s = await rpc(c, "getSignaturesForAddress", [w, {"limit": 1}])
            if s:
                last[w] = s[0]["signature"]
        print("primed; recording new trades only", flush=True)

        while end is None or time.time() < end:
            t0 = time.time()
            for w in wallets:
                params = {"limit": 25}
                if w in last:
                    params["until"] = last[w]
                async with sem:
                    sigs = await rpc(c, "getSignaturesForAddress", [w, params])
                if not sigs:
                    continue
                last[w] = sigs[0]["signature"]
                for s in reversed(sigs):
                    if s.get("err"):
                        continue
                    async with sem:
                        tx = await rpc(c, "getTransaction",
                                       [s["signature"],
                                        {"maxSupportedTransactionVersion": 0,
                                         "encoding": "jsonParsed"}])
                    sw = decode_swap(tx, w)
                    if not sw:
                        continue
                    detect = time.time()
                    px = await dex_price(c, sw["mint"])
                    rec = {"wallet": w, "name": names.get(w),
                           "sig": s["signature"], "block_time": s.get("blockTime"),
                           "detect_ts": detect,
                           "detect_lag_s": detect - (s.get("blockTime") or detect),
                           **sw, "dex": px}
                    with TRADES.open("a") as f:
                        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
                    n_trades += 1
                    if px and px.get("price_usd"):
                        print(f"  {names.get(w,'')[:14]:<14} {sw['side']:<4} "
                              f"{(px.get('symbol') or '?')[:10]:<10} "
                              f"{sw['sol_amt']:.3f} SOL  lag {rec['detect_lag_s']:.0f}s  "
                              f"liq ${px['liq_usd']:,.0f}", flush=True)
            el = time.time() - t0
            print(f"[cycle {el:.0f}s, {n_trades} trades so far]", flush=True)
            await asyncio.sleep(max(0.0, cycle - el))

if __name__ == "__main__":
    import sys
    top = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    mins = float(sys.argv[2]) if len(sys.argv) > 2 else 0
    asyncio.run(main(top_n=top, minutes=mins))
