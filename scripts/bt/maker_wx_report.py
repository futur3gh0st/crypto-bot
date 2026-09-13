#!/usr/bin/env python3
"""Score the passive-quoting forward test.

Pre-registered protocol (fixed 2026-09-12, before the first fill settled):
  * primary metric: queue-tier P&L per calendar day, Kalshi maker fee charged,
    Polymarket rebate NOT counted
  * inference: dates are the clusters; 90% CI = mean +/- 1.645 * SE
  * single evaluation at >= MIN_DAYS settled dates. GO requires
        point estimate >= GO_USD_PER_DAY  and  CI lower bound > 0
  * everything else printed here is descriptive and cannot trigger a go

Reads data/bt_cache/maker_wx/fills.jsonl (run maker_wx_fill.py first).
"""
from __future__ import annotations

import importlib.util
import json
import math
import statistics as st
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache" / "maker_wx"
MIN_DAYS = 21
GO_USD_PER_DAY = 10.0
Z90 = 1.645
H2C_BUCKETS = ((36, 99), (24, 36), (12, 24), (6, 12), (3, 6))

_spec = importlib.util.spec_from_file_location("maker_wx_fill", Path(__file__).with_name("maker_wx_fill.py"))
fill_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fill_mod)
TIERS = fill_mod.TIERS


def daily(fills: list[dict], tier: str, key: str) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for f in fills:
        if f["tier"] == tier and f.get(key) is not None:
            out[f["date"]] += f[key]
    return dict(out)


def ci(days: dict[str, float]) -> tuple[float, float, float, int]:
    """mean/day, SE, half-width at 90%, n days. Dates are the clusters."""
    n = len(days)
    if n < 2:
        return (sum(days.values()) / n if n else 0.0), float("nan"), float("nan"), n
    vals = list(days.values())
    se = st.stdev(vals) / math.sqrt(n)
    return st.mean(vals), se, Z90 * se, n


def verdict(days: dict[str, float]) -> str:
    mean, se, hw, n = ci(days)
    if n < MIN_DAYS:
        return f"INTERIM ({n}/{MIN_DAYS} settled days) - monitoring only, no decision"
    lo = mean - hw
    ok = mean >= GO_USD_PER_DAY and lo > 0
    return (f"{'GO' if ok else 'NO-GO'}: mean ${mean:+.2f}/day, 90% CI [{lo:+.2f}, {mean + hw:+.2f}] "
            f"over {n} days; bar is >= ${GO_USD_PER_DAY:.0f}/day with CI lower bound > 0")


def main() -> None:
    p = CACHE / "fills.jsonl"
    fills = [json.loads(line) for line in p.open()] if p.exists() else []
    settled = [f for f in fills if f.get("pnl") is not None]
    pending = [f for f in fills if f.get("pnl") is None]
    print(f"fills: {len(fills)} rows, {len(settled)} settled, {len(pending)} pending settlement")
    print("\n== PRIMARY: queue tier, Kalshi maker fee charged, no Polymarket rebate")
    print(verdict(daily(settled, "queue", "pnl")))

    print("\n== Sensitivity (descriptive)")
    print(f"{'tier':>8} {'variant':>12} {'days':>4} {'contracts':>9} {'$/day':>8} {'SE':>6} {'total $':>8}")
    for tier in TIERS:
        for key, label in (("pnl", "fee, no reb"), ("pnl_rebate", "fee + reb")):
            d = daily(settled, tier, key)
            mean, se, _, n = ci(d)
            contracts = sum(f["size"] for f in settled if f["tier"] == tier)
            print(f"{tier:>8} {label:>12} {n:>4d} {contracts:>9.0f} {mean:>+8.2f} {se:>6.2f} {sum(d.values()):>+8.2f}")

    print("\n== Queue tier by venue (fee, no rebate)")
    for venue in ("kalshi", "poly"):
        sub = [f for f in settled if f["venue"] == venue]
        d = daily(sub, "queue", "pnl")
        mean, se, _, n = ci(d)
        c = sum(f["size"] for f in sub if f["tier"] == "queue")
        print(f"  {venue:7s} days={n:3d} contracts={c:7.0f} ${mean:+.2f}/day (SE {se:.2f})")

    print("\n== Queue tier by hours-to-close at fill (descriptive only)")
    print(f"{'bucket':>8} {'contracts':>9} {'$/contract':>10} {'markout 1h':>10} {'markout 6h':>10}")
    for lo, hi in H2C_BUCKETS:
        sub = [f for f in settled if f["tier"] == "queue" and lo <= f["hours_to_close"] < hi]
        c = sum(f["size"] for f in sub)
        if c == 0:
            continue
        per = sum(f["pnl"] for f in sub) / c
        m1 = [f["markout_1h"] for f in sub if f.get("markout_1h") is not None]
        m6 = [f["markout_6h"] for f in sub if f.get("markout_6h") is not None]
        print(f"{lo:>3d}-{hi:<4d} {c:>9.0f} {per:>+10.4f} "
              f"{(st.mean(m1) if m1 else float('nan')):>+10.4f} {(st.mean(m6) if m6 else float('nan')):>+10.4f}")

    print("\n== Spread capture vs adverse selection, queue tier, per contract")
    q = [f for f in settled if f["tier"] == "queue"]
    if q:
        c = sum(f["size"] for f in q)
        gross = sum(((1.0 if f["result"] == "yes" else 0.0) - f["price"] if f["side"] == "buy"
                     else f["price"] - (1.0 if f["result"] == "yes" else 0.0)) * f["size"] for f in q) / c
        m1 = [f["markout_1h"] for f in q if f.get("markout_1h") is not None]
        print(f"  contracts={c:.0f}  settlement edge/contract={gross:+.4f}  "
              f"mean 1h markout={(st.mean(m1) if m1 else float('nan')):+.4f}  (negative = filled into a move)")


if __name__ == "__main__":
    main()
