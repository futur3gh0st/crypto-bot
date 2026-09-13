#!/usr/bin/env python3
"""Replay the kalshi_lag sleeve on real Kalshi bid/ask history.

This is the only desk sleeve whose venue publishes a real historical book, so
it is the only one where "would this have filled?" has an honest answer.
Asks are taken straight from the candlesticks; Kalshi quotes bids only and the
client derives no_ask = 1 - yes_bid, which is reproduced here exactly.
"""
from __future__ import annotations
import bisect, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stablebot.desk.kalshi_lag import LagParams
from stablebot.desk.reference import _dispersion_bp, _median
from stablebot.desk.signal import (SignalCfg, distance_is_measurable,
                                   fair_with_reference_noise)
from stablebot.kalshi.strategy import kalshi_taker_fee as curve_fee

CACHE = ROOT / "data" / "bt_cache"


def load_candles():
    out = {}
    p = CACHE / "kalshilag_candles.jsonl"
    for l in p.open():
        d = json.loads(l)
        rows = d.get("c") or []
        if rows:
            rows.sort()
            out[d["ticker"]] = (rows, [r[0] for r in rows])
    return out


def load_reference():
    ref = {}
    d = CACHE / "reference"
    for p in d.glob("*.json"):
        j = json.loads(p.read_text())
        cb = {int(k): v for k, v in j.get("coinbase", {}).items()}
        bs = {int(k): v for k, v in j.get("bitstamp", {}).items()}
        ref[p.stem] = (cb, bs)
    return ref


def quote_at(candles, ts):
    """First candle closing at or after ts -> (yes_ask, yes_bid)."""
    rows, keys = candles
    i = bisect.bisect_left(keys, ts)
    if i >= len(rows):
        return None
    if keys[i] - ts > 120:
        return None
    _t, ya, yb = rows[i]
    return ya, yb


def reference_at(ref, coin, ts):
    """Composite of the 1m closes ending at ts. Mirrors ReferencePrice._build."""
    src = ref.get(coin)
    if not src:
        return None, None
    cb, bs = src
    minute = (int(ts) // 60) * 60
    vals = []
    for d in (cb, bs):
        for m in (minute, minute - 60):
            if m in d and d[m] > 0:
                vals.append(d[m]); break
    if len(vals) < 2:
        return None, None
    mid = _median(vals)
    return mid, _dispersion_bp(vals, mid)


def run(starting=10000.0, verbose=True):
    p, sig = LagParams(), SignalCfg()
    candles, ref = load_candles(), load_reference()
    cands = [json.loads(l) for l in (CACHE / "kalshilag_candidates.jsonl").open()]
    cands.sort(key=lambda r: r["signal_ts"])
    cash = equity = starting
    open_pos, trades, gates = {}, [], {}

    def gate(k):
        gates[k] = gates.get(k, 0) + 1

    def settle(now):
        nonlocal cash, equity
        for tk in [t for t, q in open_pos.items() if q["close_ts"] <= now]:
            q = open_pos.pop(tk)
            cash += q["shares"] if q["won"] else 0.0
            equity = cash + sum(x["cost"] for x in open_pos.values())
            q["pnl"] = (q["shares"] if q["won"] else 0.0) - q["cost"] - q["fee"]
            q["equity"] = equity
            trades.append(q)

    for r in cands:
        settle(r["signal_ts"])
        cd = candles.get(r["ticker"])
        if not cd:
            gate("no_candles"); continue
        refpx, disp = reference_at(ref, r["coin"], r["signal_ts"])
        if refpx is None:
            gate("no_reference"); continue
        ref_bp = max(p.min_ref_sigma_bp, disp or 0.0)
        ref_sigma = ref_bp / 1e4
        ok, ratio = distance_is_measurable(refpx, r["strike"], ref_sigma,
                                           sig.min_distance_ratio)
        if not ok:
            gate("reference_noise"); continue
        rem_min = max(r["remaining"] / 60.0, 1 / 60.0)
        try:
            fair, fair_err = fair_with_reference_noise(refpx, r["strike"],
                                                       r["sigma"], rem_min, ref_sigma)
        except ValueError:
            gate("no_vol"); continue
        q = quote_at(cd, r["signal_ts"])
        if q is None:
            gate("no_quote"); continue
        yes_ask, yes_bid = q
        no_ask = 1.0 - yes_bid
        cands_side = []
        if 0 < yes_ask < 1:
            cands_side.append(("yes", yes_ask, fair - yes_ask - curve_fee(yes_ask), fair))
        if 0 < no_ask < 1:
            cands_side.append(("no", no_ask, (1 - fair) - no_ask - curve_fee(no_ask), 1 - fair))
        if not cands_side:
            gate("no_quote"); continue
        if sig.require_model_side:
            fav = [c for c in cands_side if c[3] > 0.5]
            if not fav:
                gate("wrong_side"); continue
            cands_side = fav
        side, ask, edge, side_fair = max(cands_side, key=lambda c: c[2])
        if not (sig.min_ask <= ask <= sig.max_ask):
            gate("ask_band"); continue
        required = sig.min_edge + sig.edge_uncertainty_mult * fair_err
        if edge < required:
            gate("edge"); continue
        if r["ticker"] in open_pos:
            gate("already_open"); continue
        if len(open_pos) >= p.max_concurrent:
            gate("max_concurrent"); continue
        clip = min(p.risk_frac * equity, p.fixed_clip)
        shares = clip / ask
        cost = shares * ask
        fee = shares * curve_fee(ask)
        if cost + fee > cash + 1e-9:
            gate("insufficient_cash"); continue
        cash -= cost + fee
        won = (r["result"] == "yes") if side == "yes" else (r["result"] == "no")
        open_pos[r["ticker"]] = {"ticker": r["ticker"], "coin": r["coin"], "side": side,
                                 "close_ts": r["close_ts"], "signal_ts": r["signal_ts"],
                                 "ask": ask, "edge": edge, "fair": side_fair,
                                 "fair_err": fair_err, "z": r["z"], "ratio": ratio,
                                 "shares": shares, "cost": cost, "fee": fee, "won": won}
        gate("FILLED")
    settle(10**11)
    return trades, gates, equity


if __name__ == "__main__":
    start = float(sys.argv[1]) if len(sys.argv) > 1 else 10000.0
    tr, g, eq = run(start)
    n = len(tr); w = sum(1 for t in tr if t["won"])
    pnl = sum(t["pnl"] for t in tr)
    be = sum(t["ask"] for t in tr) / n if n else 0
    print("\nkalshi_lag  (real book, no mid proxy)")
    print(f"  trades={n}  wins={w} ({100*w/n if n else 0:.1f}%)  "
          f"avg ask (breakeven)={100*be:.1f}%")
    print(f"  PnL={pnl:+,.2f}   end equity={eq:,.2f}  (start {start:,.0f})")
    print(f"  fees={sum(t['fee'] for t in tr):,.2f}")
    print("  gates: " + "  ".join(f"{k}={v}" for k, v in
                                  sorted(g.items(), key=lambda kv: -kv[1])[:9]))
    with (CACHE / "kalshilag_trades.jsonl").open("w") as f:
        for t in tr:
            f.write(json.dumps(t, separators=(",", ":")) + "\n")
