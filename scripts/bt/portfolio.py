#!/usr/bin/env python3
"""Portfolio layer: all sleeves on one pot, under the desk's real allocator.

The desk does not run sleeves at fixed size. The Allocator scores each on
realised expectancy, benches demonstrably losing ones (and re-probes them 45
minutes later), and sets each sleeve's per-trade clip. That materially changes
the answer for a losing sleeve, so the shipped Allocator and RiskGovernor are
driven here rather than reimplemented -- time.monotonic is patched to the
simulated clock so their internal scheduling behaves as it does live.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import stablebot.desk.allocator as alloc_mod
from stablebot.desk.allocator import Allocator, AllocatorCfg
from stablebot.desk.state import DeskState, SleeveStat

CACHE = ROOT / "data" / "bt_cache"

SIM = {"now": 0.0}
alloc_mod.time = type("T", (), {"monotonic": staticmethod(lambda: SIM["now"])})()


def load_stream():
    """All sleeve trades as one chronological stream.

    unit_cost is cash out per unit (fees included); unit_payout is cash in at
    settlement. A lock pays 1.00 per completed pair and costs 1 - edge.
    """
    ev = []
    p = CACHE / "spotlag_trades_final.jsonl"
    if p.exists():
        for l in p.open():
            t = json.loads(l)
            fps = t["fee"] / max(t["shares"], 1e-9)
            ev.append({"sleeve": "spot_lag", "in": t["signal_ts"], "out": t["window_end"],
                       "ask": t["ask"], "won": t["won"],
                       "unit_cost": t["ask"] + fps,
                       "unit_payout": 1.0 if t["won"] else 0.0})
    p = CACHE / "kalshilag_trades.jsonl"
    if p.exists():
        for l in p.open():
            t = json.loads(l)
            fps = t["fee"] / max(t["shares"], 1e-9)
            ev.append({"sleeve": "kalshi_lag", "in": t["signal_ts"], "out": t["close_ts"],
                       "ask": t["ask"], "won": t["won"],
                       "unit_cost": t["ask"] + fps,
                       "unit_payout": 1.0 if t["won"] else 0.0})
    p = CACHE / "polylock_stream.jsonl"
    if p.exists():
        for l in p.open():
            t = json.loads(l)
            ev.append({"sleeve": "poly_lock", "in": t["in"], "out": t["out"],
                       "ask": 1.0 - t["edge"], "won": True,
                       "unit_cost": 1.0 - t["edge"], "unit_payout": 1.0})
    ev.sort(key=lambda e: e["in"])
    return ev


def run(pot=10000.0, use_allocator=True, lock_clip_cap=None):
    ev = load_stream()
    if not ev:
        print("no sleeve trades to combine"); return None
    state = DeskState()
    names = {"spot_lag": "Spot-Lag", "poly_lock": "Poly Lock",
             "kalshi_lag": "Kalshi Lag", "kalshi_lock": "Kalshi Lock"}
    for n, l in names.items():
        state.sleeves[n] = SleeveStat(name=n, label=l)
    state.autopilot = use_allocator
    al = Allocator(AllocatorCfg(pot=pot))
    SIM["now"] = ev[0]["in"]
    al.rebalance(state, force=True)

    cash = pot
    equity = pot
    open_pos = []
    daily = {}
    peak = pot
    maxdd = 0.0
    filled = benched_skips = 0

    def settle(now):
        nonlocal cash, equity
        rest = []
        for q in open_pos:
            if q["out"] <= now:
                cash += q["shares"] * q["payout"]
                st = state.sleeves[q["sleeve"]]
                st.record_result(q["pnl"], q["ask"])
                d = __import__("datetime").datetime.fromtimestamp(q["out"], __import__("datetime").timezone.utc).strftime("%Y-%m-%d")
                daily[d] = daily.get(d, 0.0) + q["pnl"]
            else:
                rest.append(q)
        open_pos[:] = rest
        equity = cash + sum(q["cost"] for q in open_pos)

    for e in ev:
        SIM["now"] = e["in"]
        settle(e["in"])
        if use_allocator:
            al.rebalance(state)
            al.tune(state)
        st = state.sleeves[e["sleeve"]]
        if use_allocator and not st.enabled:
            benched_skips += 1; continue
        clip = st.clip if (use_allocator and st.clip > 0) else 15.0
        if lock_clip_cap is not None and e["sleeve"] == "poly_lock":
            # the book holds what it holds; wanting more does not deepen it
            clip = min(clip, lock_clip_cap)
        if clip <= 0:
            benched_skips += 1; continue
        shares = clip / max(e["unit_cost"], 1e-9)
        cost = shares * e["unit_cost"]
        if cost > cash:
            continue
        pnl = shares * (e["unit_payout"] - e["unit_cost"])
        cash -= cost
        open_pos.append({"sleeve": e["sleeve"], "out": e["out"], "shares": shares,
                         "cost": cost, "won": e["won"], "pnl": pnl, "ask": e["ask"],
                         "payout": e["unit_payout"]})
        st.trades += 1
        filled += 1
        equity = cash + sum(q["cost"] for q in open_pos)
        peak = max(peak, equity)
        maxdd = max(maxdd, (peak - equity) / peak if peak > 0 else 0.0)

    SIM["now"] = max(e["out"] for e in ev) + 1
    settle(SIM["now"])
    return {"equity": equity, "pot": pot, "filled": filled,
            "benched_skips": benched_skips, "daily": daily, "maxdd": maxdd,
            "sleeves": {n: {"trades": s.trades, "realized": s.realized,
                            "wins": s.wins, "losses": s.losses,
                            "enabled": s.enabled, "reason": s.disabled_reason}
                        for n, s in state.sleeves.items()}}


if __name__ == "__main__":
    import sys as _s
    cap = float(_s.argv[1]) if len(_s.argv) > 1 else None
    tag = "" if cap is None else f", poly_lock capped at ${cap:.0f} of real depth"
    for label, ua in ((f"autopilot ON (allocator benches losers){tag}", True),
                      (f"autopilot OFF (flat $15 clips){tag}", False)):
        r = run(10000.0, ua, cap)
        if not r:
            continue
        print(f"\n=== {label} ===")
        print(f"  end equity ${r['equity']:,.2f}  "
              f"PnL {r['equity']-r['pot']:+,.2f} ({100*(r['equity']-r['pot'])/r['pot']:+.1f}%)")
        print(f"  fills={r['filled']}  skipped-while-benched={r['benched_skips']}  "
              f"max drawdown={100*r['maxdd']:.1f}%")
        for n, s in r["sleeves"].items():
            if s["trades"] or s["realized"]:
                print(f"    {n:12} trades={s['trades']:5d} realized={s['realized']:+10,.2f}"
                      + ("" if s["enabled"] else f"  [BENCHED: {s['reason'][:50]}]"))
