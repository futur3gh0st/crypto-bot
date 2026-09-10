#!/usr/bin/env python3
"""Score the copy-trade forward test so far.

A copy is entered at the price available when the trade was DETECTED, never at
the price the KOL got, and exited at fixed horizons from the tracked price
path. Solana DEX taker cost is charged both ways.

Run it any time; it reports on whatever has accumulated.
"""
from __future__ import annotations
import json, statistics as st, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache"
FEE = 0.0125          # ~1% priority+platform, ~0.25% pool fee, per side
HORIZONS = (300, 900, 3600, 14400)
MIN_LIQ = 20_000.0    # below this a $100 copy is a meaningful fraction of the pool


def load():
    trades = []
    p = CACHE / "kol_trades.jsonl"
    if p.exists():
        for l in p.open():
            try:
                trades.append(json.loads(l))
            except Exception:
                pass
    prices = defaultdict(list)
    p = CACHE / "kol_prices.jsonl"
    if p.exists():
        for l in p.open():
            try:
                r = json.loads(l)
            except Exception:
                continue
            if r.get("price_usd"):
                prices[r["mint"]].append((r["ts"], r["price_usd"]))
    for m in prices:
        prices[m].sort()
    return trades, prices


def price_at(series, t):
    """Last snapshot at or before t (never look forward for an entry)."""
    best = None
    for ts, px in series:
        if ts <= t:
            best = px
        else:
            break
    return best


def price_after(series, t):
    for ts, px in series:
        if ts >= t:
            return px
    return None


def main():
    trades, prices = load()
    buys = [t for t in trades if t.get("side") == "buy"
            and (t.get("dex") or {}).get("price_usd")]
    print(f"recorded trades: {len(trades)}  (buys with a price: {len(buys)})")
    if not trades:
        print("nothing yet — the watcher needs to run longer"); return

    lags = [t["detect_lag_s"] for t in trades if t.get("detect_lag_s") is not None]
    if lags:
        lags.sort()
        print(f"\nDETECTION LAG (their fill lands -> a follower can see it)")
        print(f"  median {st.median(lags):.0f}s   p25 {lags[int(.25*len(lags))]:.0f}s"
              f"   p75 {lags[int(.75*len(lags))]:.0f}s   max {lags[-1]:.0f}s")

    liq = [(t.get("dex") or {}).get("liq_usd", 0) for t in buys]
    if liq:
        liq.sort()
        thin = sum(1 for x in liq if x < MIN_LIQ)
        print(f"\nPOOL LIQUIDITY at the moment of copy")
        print(f"  median ${st.median(liq):,.0f}   p25 ${liq[int(.25*len(liq))]:,.0f}")
        print(f"  below ${MIN_LIQ:,.0f}: {thin}/{len(liq)} ({100*thin/len(liq):.0f}%)")

    print(f"\nCOPY RESULT — enter at detection, exit at horizon, {100*FEE:.2f}% per side")
    print(f"{'horizon':>9} {'n':>5} {'win%':>7} {'mean':>9} {'median':>9} {'total%':>9}")
    any_row = False
    for h in HORIZONS:
        rets = []
        for t in buys:
            series = prices.get(t["mint"])
            if not series:
                continue
            entry = (t.get("dex") or {}).get("price_usd")
            exit_px = price_after(series, t["detect_ts"] + h)
            if not entry or not exit_px:
                continue
            rets.append((exit_px / entry) * (1 - FEE) ** 2 - 1.0)
        if not rets:
            continue
        any_row = True
        wins = sum(1 for r in rets if r > 0)
        print(f"{h//60:>7}m {len(rets):>5} {100*wins/len(rets):>6.1f}% "
              f"{100*st.mean(rets):>+8.2f}% {100*st.median(rets):>+8.2f}% "
              f"{100*sum(rets):>+8.1f}%")
    if not any_row:
        print("  no horizon has matured yet — price snapshots need more time")

    by = defaultdict(int)
    for t in trades:
        by[t.get("name") or "?"] += 1
    print(f"\nmost active of the tracked wallets: " +
          ", ".join(f"{k} ({v})" for k, v in
                    sorted(by.items(), key=lambda kv: -kv[1])[:6]))
    if trades:
        span = (max(t["detect_ts"] for t in trades)
                - min(t["detect_ts"] for t in trades)) / 3600.0
        print(f"observation window: {span:.1f} hours")


main()
