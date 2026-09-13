#!/usr/bin/env python3
"""Does the claimed edge survive contact with real outcomes?

The desk logs, at decision time, the model's fair probability and the edge it
believes it is taking. It later logs what actually happened. This pairs the two
and asks three questions the P&L number alone cannot answer:

  1. REALIZED    What did a trade actually return, and is that distinguishable
                 from zero? A total P&L near $0 over a few hundred trades is
                 not evidence of "roughly break-even" -- it is usually evidence
                 of "we cannot tell yet". The confidence interval says which.

  2. CLAIMED vs REALIZED
                 Sum the edge the model claimed at entry. If the model claimed
                 $1,400 and delivered -$2, the edge is not small, it is absent,
                 and the gap is the size of the self-deception.

  3. CALIBRATION Bucket trades by the model's own fair probability and compare
                 it to the realized win rate in that bucket. A model with real
                 alpha is calibrated: things it calls 70% happen about 70% of
                 the time. This is the diagnostic that localises the error --
                 it shows *where* on the probability curve the model lies.

It also reports how many resolved trades are needed to detect the claimed edge,
so a run can be sized before it starts instead of argued about afterwards.

Paper ledgers only. Reads nothing live and places no orders.

    python scripts/bt/expectancy.py data/kalshi_lag_ledger.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

# Two shapes reach this tool.
#
# PAIRED  the live desk appends an entry row and, later, a resolve row. They
#         carry a "kind" and must be matched up.
# FLAT    the backtest replays (scripts/bt/replay_*.py) emit one settled row per
#         trade, entry and outcome together, with no "kind" at all.
#
# The flat shape is what makes a real answer possible today: replay_kalshilag.py
# produces hundreds of trades against Kalshi's published historical book, where
# a live paper run needs weeks to reach the same count.

# The probability field does NOT mean the same thing in every ledger, and the
# difference is invisible until a trade takes the "no" side:
#
#   live kalshi_lag   "fair" is P(YES). Verified against its own rows: edge
#                     reconciles as (1 - fair) - entry_p - fee on a no-side
#                     trade, and as fair - entry_p - fee on a yes-side trade.
#   replay_kalshilag  "fair" is already P(side taken) -- it stores side_fair,
#                     passing 1 - fair for the no side.
#   spot_lag          "fair_side" is already P(side taken) (fair_up is the
#                     other one).
#
# Calibration compares the model's probability against whether that side won,
# so everything must be normalised to P(side taken) first. Reading P(YES) as
# P(side) silently scores no-side trades against their own complement.
LEDGERS = {
    "kalshi_lag": {
        "entry": "kalshi_lag", "resolve": "kalshi_lag_resolve", "key": "ticker",
        "prob": "fair", "prob_is_yes": True, "side": "side",
    },
    "spot_lag": {
        "entry": "spot_lag", "resolve": "spot_lag_resolve", "key": "slug",
        "prob": "fair_side", "prob_is_yes": False, "side": "side",
    },
}

# replay_kalshilag computes pnl = payout - cost - fee, so it is already net.
FLAT_LEDGERS = {
    "kalshilag_trades": {"prob": "fair", "pnl": "pnl", "prob_is_yes": False, "side": "side"},
    "spotlag_trades": {"prob": "fair_side", "pnl": "pnl", "prob_is_yes": False, "side": "side"},
}


def side_prob(entry: dict, spec: dict) -> float | None:
    """The model's probability that the side actually bought would win."""
    raw = entry.get(spec["prob"])
    if raw is None:
        return None
    p = float(raw)
    if spec.get("prob_is_yes") and str(entry.get(spec["side"], "")).lower() == "no":
        return 1.0 - p
    return p

Z95 = 1.959963985  # two-sided 95%
Z80 = 0.8416212336  # 80% power

# Below this many resolved trades no verdict is rendered. A handful of wins on
# high-probability bets is the single most common way to fool yourself here:
# the sample std comes out tiny, the interval comes out narrow, and the tool
# reports confidence it has not earned.
MIN_N_FOR_VERDICT = 30

# Student's t, two-sided 95%, by degrees of freedom. The normal 1.96 understates
# the interval badly at small n -- at n=3 the true multiplier is 4.30, not 1.96.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086,
        25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980}


def t95(df: int) -> float:
    """Two-sided 95% critical value; falls back to the normal limit for large df."""
    if df < 1:
        return float("inf")
    if df in _T95:
        return _T95[df]
    smaller = [k for k in _T95 if k <= df]
    return _T95[max(smaller)] if smaller else Z95


def detect(rows: list[dict]) -> tuple[str, bool]:
    """-> (name, is_flat). A row with no "kind" is a settled backtest trade."""
    kinds = {r.get("kind") for r in rows}
    for name, spec in LEDGERS.items():
        if spec["entry"] in kinds:
            return name, False
    # Flat: no "kind", but entry and outcome are in the same row.
    sample = rows[0]
    if "kind" not in sample and "won" in sample:
        for name, spec in FLAT_LEDGERS.items():
            if spec["prob"] in sample:
                return name, True
        raise SystemExit(
            "flat ledger with no recognised probability field; "
            f"has: {sorted(sample)[:12]}"
        )
    raise SystemExit(f"unrecognised ledger; kinds present: {sorted(k for k in kinds if k)}")


def as_pairs(rows: list[dict], pnl_field: str) -> list[dict]:
    """Present flat settled rows through the same {entry, resolve} interface, so
    every downstream statistic has exactly one code path."""
    out = []
    for r in rows:
        if r.get("won") is None:
            continue
        resolve = dict(r)
        resolve["pnl_after_fee"] = r.get(pnl_field)
        out.append({"entry": r, "resolve": resolve})
    return out


def pair(rows: list[dict], entry_kind: str, resolve_kind: str, key: str) -> list[dict]:
    """Match each resolve to its entry. Same key can recur, so consume in order."""
    pending: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("kind") == entry_kind:
            pending.setdefault(str(r.get(key)), []).append(r)
    paired = []
    for r in rows:
        if r.get("kind") != resolve_kind:
            continue
        queue = pending.get(str(r.get(key)))
        if not queue:
            continue
        paired.append({"entry": queue.pop(0), "resolve": r})
    return paired


def mean_sd(xs: list[float]) -> tuple[float, float]:
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    m = sum(xs) / n
    if n < 2:
        return m, 0.0
    return m, math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ledger", type=Path)
    ap.add_argument("--buckets", type=int, default=5, help="calibration buckets (default 5)")
    args = ap.parse_args()

    if not args.ledger.exists():
        print(f"no such ledger: {args.ledger}", file=sys.stderr)
        return 2
    rows = [json.loads(l) for l in args.ledger.open() if l.strip()]
    if not rows:
        print(f"{args.ledger} is empty -- nothing resolved yet.")
        return 0

    name, is_flat = detect(rows)
    if is_flat:
        spec = FLAT_LEDGERS[name]
        trades = as_pairs(rows, spec["pnl"])
        entries = len(rows)
        shape = "flat (settled backtest replay)"
    else:
        spec = LEDGERS[name]
        trades = pair(rows, spec["entry"], spec["resolve"], spec["key"])
        entries = sum(1 for r in rows if r.get("kind") == spec["entry"])
        shape = "paired (live paper ledger)"

    print(f"\n  ledger        {args.ledger}")
    print(f"  sleeve        {name}")
    print(f"  shape         {shape}")
    print(f"  entries       {entries}")
    print(f"  resolved      {len(trades)}")
    if not trades:
        print("\n  Nothing resolved yet. Expectancy is undefined -- let it run.\n")
        return 0

    # ---- 1. realized -------------------------------------------------------
    pnl = [float(t["resolve"].get("pnl_after_fee") or 0.0) for t in trades]
    n = len(pnl)
    total = sum(pnl)
    m, sd = mean_sd(pnl)
    se = sd / math.sqrt(n) if n > 1 and sd > 0 else 0.0
    crit = t95(n - 1)
    lo, hi = (m - crit * se, m + crit * se) if se else (m, m)

    print("\n  --- 1. realized, per trade -------------------------------------")
    print(f"  total P&L                 {total:+,.2f}")
    print(f"  mean per trade            {m:+.4f}")
    print(f"  std dev                   {sd:.4f}")
    if se:
        print(f"  95% CI (t, df={n - 1})          [{lo:+.4f}, {hi:+.4f}]")
    else:
        print("  95% CI                    n/a (need >1 resolved trade)")

    if n < MIN_N_FOR_VERDICT:
        print(f"  verdict                   NO VERDICT -- {n} resolved trades, "
              f"need {MIN_N_FOR_VERDICT}")
        print("                            (a few wins on high-probability bets "
              "look identical to skill)")
    elif se and (lo > 0 or hi < 0):
        print("  verdict                   DISTINGUISHABLE from zero")
    else:
        print("  verdict                   NOT distinguishable from zero")

    # ---- 2. claimed vs realized -------------------------------------------
    claimed = []
    for t in trades:
        e = t["entry"]
        edge, shares = e.get("edge"), e.get("shares")
        if edge is not None and shares is not None:
            claimed.append(float(edge) * float(shares))
    print("\n  --- 2. what the model claimed ---------------------------------")
    if claimed:
        ctot = sum(claimed)
        print(f"  claimed edge, total       {ctot:+,.2f}   (sum of edge x shares at entry)")
        print(f"  claimed per trade         {ctot / len(claimed):+.4f}")
        print(f"  realized per trade        {m:+.4f}")
        if abs(ctot) > 1e-9:
            print(f"  realization ratio         {total / ctot:6.1%}   (100% = model was right)")
    else:
        print("  no edge/shares on entries -- cannot compare.")

    # ---- 3. calibration ----------------------------------------------------
    print("\n  --- 3. calibration: model probability vs reality ---------------")
    pts = []
    for t in trades:
        p = side_prob(t["entry"], spec)
        w = t["resolve"].get("won")
        if p is not None and w is not None:
            pts.append((p, 1.0 if w else 0.0))
    if not pts:
        print(f"  no '{spec['prob']}'/'won' pairs -- cannot calibrate.")
    else:
        pts.sort()
        size = max(1, len(pts) // args.buckets)
        print(f"  {'model says':>12} {'actually won':>14} {'n':>5}   {'gap':>8}")
        for i in range(0, len(pts), size):
            chunk = pts[i : i + size]
            if not chunk:
                continue
            pred = sum(p for p, _ in chunk) / len(chunk)
            act = sum(w for _, w in chunk) / len(chunk)
            print(f"  {pred:11.1%} {act:13.1%} {len(chunk):5d}   {act - pred:+7.1%}")
        allp = sum(p for p, _ in pts) / len(pts)
        alla = sum(w for _, w in pts) / len(pts)
        print(f"  {'OVERALL':>12} {'':>0}")
        print(f"  {allp:11.1%} {alla:13.1%} {len(pts):5d}   {alla - allp:+7.1%}")

    # ---- 4. how long must this run? ---------------------------------------
    print("\n  --- 4. sample size needed -------------------------------------")
    if sd > 0 and claimed:
        target = abs(sum(claimed) / len(claimed))
        if target > 1e-9:
            need = ((Z95 + Z80) ** 2) * (sd**2) / (target**2)
            need = max(need, MIN_N_FOR_VERDICT)
            print(f"  to detect the claimed {target:.4f}/trade at 95% conf, 80% power:")
            print(f"  resolved trades needed    {math.ceil(need):,}")
            print(f"  have                      {n:,}")
            if n < MIN_N_FOR_VERDICT:
                print(f"  note                      std dev is itself unreliable at "
                      f"n={n}; treat this as a floor, not a target")
            if n < need:
                print(f"  >>> UNDERPOWERED: {math.ceil(need) - n:,} more resolved trades needed.")
    else:
        print("  need more resolved trades before this is computable.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
