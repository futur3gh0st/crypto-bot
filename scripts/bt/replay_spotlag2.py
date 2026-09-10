#!/usr/bin/env python3
"""spot_lag replay v2: freshness filter + measured ask premium.

Two corrections the naive replay needs, both measured against live books:

  freshness  a prices-history print that is ~60s old disagrees with the live
             ask by 20-33c. Only fill on a print close to the decision instant;
             otherwise the desk would have seen "no usable quote".

  premium    even a fresh print is mid/last, not the ask, and sits 5-12c below
             the ask on the side a move favours. Charge that.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from stablebot.desk.signal import SignalCfg
from stablebot.poly.replay import poly_taker_fee, winning_side

CACHE = ROOT / "data" / "bt_cache"
SIG = SignalCfg()


def load():
    cands = [json.loads(l) for l in (CACHE / "spotlag_candidates.jsonl").open()]
    poly = {}
    for l in (CACHE / "spotlag_poly.jsonl").open():
        r = json.loads(l)
        if r.get("ok"):
            poly[r["slug"]] = r
    return sorted(cands, key=lambda r: r["signal_ts"]), poly


def run(cands, poly, premium, max_delay, starting=10000.0,
        max_concurrent=4, risk_frac=0.05, fixed_clip=15.0):
    cash = equity = starting
    open_pos, trades, gates = {}, [], {}

    def gate(k):
        gates[k] = gates.get(k, 0) + 1

    def settle(now):
        nonlocal cash, equity
        for slug in [s for s, p in open_pos.items() if p["window_end"] <= now]:
            p = open_pos.pop(slug)
            cash += p["shares"] if p["won"] else 0.0
            equity = cash + sum(q["cost"] for q in open_pos.values())
            p["pnl"] = (p["shares"] if p["won"] else 0.0) - p["cost"] - p["fee"]
            p["equity"] = equity
            trades.append(p)

    for r in cands:
        settle(r["signal_ts"])
        pr = poly.get(r["slug"])
        if not pr:
            gate("no_poly_data"); continue
        winner = winning_side(pr.get("outcomes"), pr.get("outcomePrices"))
        if winner is None:
            gate("unresolved"); continue
        st = r["signal_ts"]
        pick = None
        for t, p in sorted(pr["hist"]):
            if t >= st and 0.0 < p < 1.0:
                pick = (t, p); break
        if pick is None:
            gate("no_quote"); continue
        t, mid = pick
        if t - st > max_delay:
            gate("stale_quote"); continue
        ask = min(0.999, mid + premium)
        if not (SIG.min_ask <= ask <= SIG.max_ask):
            gate("ask_band"); continue
        fee_ps = poly_taker_fee(ask)
        edge = r["fair_side"] - ask - fee_ps
        if edge < SIG.min_edge:
            gate("edge"); continue
        if r["slug"] in open_pos:
            gate("already_open"); continue
        if len(open_pos) >= max_concurrent:
            gate("max_concurrent"); continue
        clip = min(risk_frac * equity, fixed_clip)
        shares = clip / ask
        cost = shares * ask
        fee = shares * fee_ps
        if cost + fee > cash + 1e-9:
            gate("insufficient_cash"); continue
        cash -= cost + fee
        open_pos[r["slug"]] = {"slug": r["slug"], "coin": r["coin"], "side": r["side"],
                               "window_end": r["window_end"], "signal_ts": st,
                               "ask": ask, "mid": mid, "delay": t - st, "edge": edge,
                               "fair": r["fair_side"], "shares": shares, "cost": cost,
                               "fee": fee, "won": winner == r["side"]}
        gate("FILLED")
    settle(10**11)
    return trades, gates, equity


if __name__ == "__main__":
    cands, poly = load()
    print(f"{'delay<=':>8} {'premium':>8} {'trades':>7} {'win%':>6} {'brkeven%':>9} {'PnL':>11} {'equity':>11}")
    for max_delay in (15, 30, 10**9):
        for prem in (0.0, 0.03, 0.06, 0.08, 0.10):
            tr, g, eq = run(cands, poly, prem, max_delay)
            n = len(tr); w = sum(1 for t in tr if t["won"])
            pnl = sum(t["pnl"] for t in tr)
            be = sum(t["ask"] for t in tr) / n if n else 0
            lbl = "any" if max_delay > 1000 else str(max_delay) + "s"
            print(f"{lbl:>8} {prem:>8.3f} {n:>7} {100*w/n if n else 0:>5.1f}% "
                  f"{100*be:>8.1f}% {pnl:>+11,.2f} {eq:>11,.2f}")
