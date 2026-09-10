#!/usr/bin/env python3
"""spot_lag replay v3: correct for the measured staleness of prices-history.

Live sampling shows the last print is a median 31s old and, on the side a move
favours, sits a mean 7.9c BELOW the ask a taker would pay (73% of the time),
while the live book spread is only 1c. The series lags; it is not the book.

So instead of filling at the stale print, fill at the price the tape actually
reached `lag` seconds later, plus half the observed spread. That is what the
lagging series was going to catch up to.
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
HALF_SPREAD = 0.005          # median live spread 0.010


def load():
    cands = sorted((json.loads(l) for l in (CACHE / "spotlag_candidates.jsonl").open()),
                   key=lambda r: r["signal_ts"])
    poly = {}
    for l in (CACHE / "spotlag_poly.jsonl").open():
        r = json.loads(l)
        if r.get("ok"):
            poly[r["slug"]] = r
    return cands, poly


def run(cands, poly, lag, starting=10000.0, premium=HALF_SPREAD,
        max_concurrent=4, risk_frac=0.05, fixed_clip=15.0):
    cash = equity = starting
    open_pos, trades, gates = {}, [], {}

    def gate(k):
        gates[k] = gates.get(k, 0) + 1

    def settle(now):
        nonlocal cash, equity
        for s in [s for s, q in open_pos.items() if q["window_end"] <= now]:
            q = open_pos.pop(s)
            cash += q["shares"] if q["won"] else 0.0
            equity = cash + sum(x["cost"] for x in open_pos.values())
            q["pnl"] = (q["shares"] if q["won"] else 0.0) - q["cost"] - q["fee"]
            trades.append(q)

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
            if t >= st + lag and 0.0 < p < 1.0:
                pick = (t, p); break
        if pick is None:
            gate("no_quote"); continue
        _t, px = pick
        ask = min(0.999, px + premium)
        if not (SIG.min_ask <= ask <= SIG.max_ask):
            gate("ask_band"); continue
        fee_ps = poly_taker_fee(ask)
        # the decision still uses the model fair computed at the signal instant
        edge = r["fair_side"] - ask - fee_ps
        if edge < SIG.min_edge:
            gate("edge"); continue
        if r["slug"] in open_pos:
            gate("already_open"); continue
        if len(open_pos) >= max_concurrent:
            gate("max_concurrent"); continue
        clip = min(risk_frac * equity, fixed_clip)
        shares = clip / ask
        cost, fee = shares * ask, shares * fee_ps
        if cost + fee > cash + 1e-9:
            gate("insufficient_cash"); continue
        cash -= cost + fee
        open_pos[r["slug"]] = {"slug": r["slug"], "coin": r["coin"], "ask": ask,
                               "window_end": r["window_end"], "shares": shares,
                               "cost": cost, "fee": fee, "edge": edge,
                               "won": winner == r["side"]}
        gate("FILLED")
    settle(10**11)
    return trades, gates, equity


if __name__ == "__main__":
    cands, poly = load()
    start = 10000.0
    print("Filling at the price the tape reached `lag` seconds after the signal,")
    print("plus half the live spread (0.005). lag=0 is the naive backtest.\n")
    print(f"{'lag':>6} {'trades':>7} {'win%':>7} {'avg ask':>8} {'PnL':>12} {'end equity':>12}")
    for lag in (0, 15, 30, 45, 60):
        tr, g, eq = run(cands, poly, lag, start)
        n = len(tr); w = sum(1 for t in tr if t["won"])
        pnl = sum(t["pnl"] for t in tr)
        aa = sum(t["ask"] for t in tr) / n if n else 0
        print(f"{lag:>5}s {n:>7} {100*w/n if n else 0:>6.1f}% {aa:>8.3f} "
              f"{pnl:>+12,.2f} {eq:>12,.2f}")
