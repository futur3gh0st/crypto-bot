#!/usr/bin/env python3
"""Stage C: replay the spot_lag sleeve over the fetched candidates.

Applies the desk's post-signal gates in order (ask band -> edge vs fee ->
concurrency -> clip/cash), fills at the first Polymarket print at or after the
signal instant plus an ask premium, and settles from Gamma outcomePrices.

ask_premium models the gap between the mid/last series we can see historically
and the ask the desk would actually pay. Live sampling showed asks summing to
1.01 while mids summed to 1.00, i.e. about +0.005 per side.
"""
from __future__ import annotations

import json, sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stablebot.desk.signal import SignalCfg
from stablebot.poly.replay import poly_taker_fee, winning_side

CACHE = ROOT / "data" / "bt_cache"


@dataclass
class Cfg:
    ask_premium: float = 0.005
    max_concurrent: int = 4
    risk_frac: float = 0.05
    fixed_clip: float = 15.0
    starting: float = 1000.0
    sig: SignalCfg = field(default_factory=SignalCfg)


def load():
    cands = {}
    for l in (CACHE / "spotlag_candidates.jsonl").open():
        r = json.loads(l)
        cands[r["slug"]] = r
    poly = {}
    for l in (CACHE / "spotlag_poly.jsonl").open():
        r = json.loads(l)
        if r.get("ok"):
            poly[r["slug"]] = r
    return cands, poly


def entry_price(hist, signal_ts):
    """First print at or after the decision instant. No lookahead to a better one."""
    best = None
    for t, p in hist:
        if t >= signal_ts and 0.0 < p < 1.0:
            if best is None or t < best[0]:
                best = (t, p)
    return best


def run(cfg: Cfg, verbose=True):
    cands, poly = load()
    rows = sorted(cands.values(), key=lambda r: r["signal_ts"])
    equity = cfg.starting
    cash = cfg.starting
    open_pos = {}          # slug -> dict
    trades, gates = [], {}

    def gate(name):
        gates[name] = gates.get(name, 0) + 1

    def settle_due(now_ts):
        nonlocal cash, equity
        for slug in [s for s, p in open_pos.items() if p["window_end"] <= now_ts]:
            p = open_pos.pop(slug)
            payout = p["shares"] if p["won"] else 0.0
            cash += payout
            equity = cash + sum(q["cost"] for q in open_pos.values())
            p["pnl"] = payout - p["cost"] - p["fee"]
            p["equity"] = equity
            trades.append(p)

    for r in rows:
        settle_due(r["signal_ts"])
        pr = poly.get(r["slug"])
        if not pr:
            gate("no_poly_data"); continue
        winner = winning_side(pr.get("outcomes"), pr.get("outcomePrices"))
        if winner is None:
            gate("unresolved"); continue
        hp = entry_price(pr["hist"], r["signal_ts"])
        if hp is None:
            gate("no_quote"); continue
        _t, mid = hp
        ask = min(0.999, mid + cfg.ask_premium)
        if not (cfg.sig.min_ask <= ask <= cfg.sig.max_ask):
            gate("ask_band"); continue
        fee_ps = poly_taker_fee(ask)
        edge = r["fair_side"] - ask - fee_ps
        if edge < cfg.sig.min_edge:
            gate("edge"); continue
        if r["slug"] in open_pos:
            gate("already_open"); continue
        if len(open_pos) >= cfg.max_concurrent:
            gate("max_concurrent"); continue
        clip = min(cfg.risk_frac * equity, cfg.fixed_clip)
        if clip < 1.0:
            gate("sizing"); continue
        shares = clip / ask
        cost = shares * ask
        fee = shares * fee_ps
        if cost + fee > cash + 1e-9:
            gate("insufficient_cash"); continue
        cash -= cost + fee
        open_pos[r["slug"]] = {
            "slug": r["slug"], "coin": r["coin"], "side": r["side"],
            "window_end": r["window_end"], "signal_ts": r["signal_ts"],
            "z": r["z"], "fair": r["fair_side"], "ask": ask, "mid": mid,
            "edge": edge, "shares": shares, "cost": cost, "fee": fee,
            "won": (winner == r["side"]),
        }
        gate("FILLED")

    settle_due(10**11)
    return trades, gates, equity


def summarize(name, trades, gates, equity, cfg):
    n = len(trades)
    pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["won"])
    fees = sum(t["fee"] for t in trades)
    avg_ask = sum(t["ask"] for t in trades) / n if n else 0
    avg_edge = sum(t["edge"] for t in trades) / n if n else 0
    print(f"\n--- {name} (ask_premium={cfg.ask_premium:+.3f}) ---")
    print(f"  trades={n}  wins={wins} ({100*wins/n if n else 0:.1f}%)  "
          f"breakeven needed={100*avg_ask:.1f}%")
    print(f"  PnL={pnl:+,.2f}  fees={fees:,.2f}  end equity={equity:,.2f} "
          f"(start {cfg.starting:,.0f})")
    print(f"  avg ask={avg_ask:.3f}  avg claimed edge={avg_edge:+.3f}")
    top = sorted(gates.items(), key=lambda kv: -kv[1])[:8]
    print("  gates: " + "  ".join(f"{k}={v}" for k, v in top))
    return {"trades": n, "wins": wins, "pnl": pnl, "fees": fees,
            "equity": equity, "avg_ask": avg_ask, "gates": gates}


if __name__ == "__main__":
    out = {}
    for prem in (0.0, 0.005, 0.010):
        cfg = Cfg(ask_premium=prem, starting=float(sys.argv[1]) if len(sys.argv) > 1 else 1000.0)
        tr, g, eq = run(cfg)
        out[f"prem_{prem}"] = summarize("spot_lag", tr, g, eq, cfg)
        if prem == 0.005:
            with (CACHE / "spotlag_trades.jsonl").open("w") as f:
                for t in tr:
                    f.write(json.dumps(t, separators=(",", ":")) + "\n")
    (CACHE / "spotlag_summary.json").write_text(json.dumps(out, indent=2))
