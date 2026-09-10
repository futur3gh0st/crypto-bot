#!/usr/bin/env python3
"""Quantify the gap between prices-history and the ask a taker would pay."""
from __future__ import annotations
import json, sys
from pathlib import Path

P = Path(__file__).resolve().parents[2] / "data" / "bt_cache" / "lag_dense.jsonl"


def pct(v, f):
    return v[int(f * (len(v) - 1))] if v else float("nan")


def main():
    rows = [json.loads(l) for l in P.open()]
    fav_gap, unfav_gap, spreads, ages = [], [], [], []
    win_gap = []          # restricted to the desk's decision window
    for r in rows:
        fav = r.get("fav")
        for side in ("up", "down"):
            d = r.get(side)
            if not isinstance(d, dict):
                continue
            a, b, h = d.get("ask"), d.get("bid"), d.get("hist")
            if a is None or b is None or h is None:
                continue
            if not (0 < a < 1 and 0 < b < 1 and 0 < h < 1):
                continue
            spreads.append(a - b)
            if d.get("hist_t"):
                ages.append(r["t"] - d["hist_t"])
            gap = a - h                     # what we UNDERPAY by if we fill at h
            (fav_gap if side == fav else unfav_gap).append(gap)
            if side == fav and 45 <= r["elapsed"] <= 105:
                win_gap.append(gap)
    n = len(rows)
    print(f"dense samples: {n} snapshots, {len(fav_gap)+len(unfav_gap)} side-observations")
    sp = sorted(spreads)
    print(f"\nlive book spread (ask-bid): median={pct(sp,.5):.3f}  p90={pct(sp,.9):.3f}")
    ag = sorted(ages)
    print(f"age of last prices-history print: median={pct(ag,.5):.0f}s  p90={pct(ag,.9):.0f}s")
    for name, v in (("FAVOURED side (the one the desk buys)", fav_gap),
                    ("unfavoured side", unfav_gap),
                    ("FAVOURED side, 45-105s into window (decision moment)", win_gap)):
        v = sorted(v)
        if not v:
            print(f"\n{name}: no samples"); continue
        mean = sum(v) / len(v)
        print(f"\n{name}: n={len(v)}")
        print(f"  ask - prices_history = mean {mean:+.4f}  median {pct(v,.5):+.4f}"
              f"  p25 {pct(v,.25):+.4f}  p75 {pct(v,.75):+.4f}")
        print(f"  share where history UNDERSTATES the ask: "
              f"{100*sum(1 for x in v if x > 0)/len(v):.0f}%")
    return sorted(win_gap) or sorted(fav_gap)


if __name__ == "__main__":
    main()
