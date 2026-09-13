#!/usr/bin/env python3
"""Replay the recorded books + prints through a fixed passive-quoting policy
and decide, print by print, which resting orders would have filled.

Policy (pre-registered; do not tune mid-run):
  * join the best bid and the best ask on every bracket, QUOTE_SIZE a side
  * requote to the current touch at every book snapshot; a move cancels and
    rejoins at the back of the new queue
  * stop quoting WINDOW_END_SEC before close; hold every fill to settlement

Three fill tiers, all computed from the same resting order:
  through  - a print at a price strictly worse than our level. The level was
             swept. Hard lower bound on fills; hardest-adversely-selected fills.
  queue    - FIFO: prints at our level on our side consume the size that was
             resting ahead of us when we joined; we fill on the overflow.
             Cancellations ahead of us are ignored, so this is still a lower
             bound. Primary metric.
  atlevel  - any print at our level counts as ours. Upper bound.

Fees: Kalshi maker = 25% of the taker curve = 0.0175*p*(1-p) (fee schedule
7.7.26). Polymarket weather makers pay 0 and receive rebateRate 0.25 of the
contra taker fee (gamma feeSchedule rate 0.05); the p*(1-p) form of that fee
is assumed, so P&L is reported with and without it.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache" / "maker_wx"
QUOTE_SIZE = 10.0
WINDOW_END_SEC = 3 * 3600
TIERS = ("through", "queue", "atlevel")
MARKOUT_SEC = (3600, 6 * 3600)


def kalshi_maker_fee(p: float) -> float:
    return 0.0175 * p * (1.0 - p) if 0.0 < p < 1.0 else 0.0


def poly_rebate(p: float) -> float:
    return 0.25 * 0.05 * p * (1.0 - p) if 0.0 < p < 1.0 else 0.0


class Order:
    """One resting order; each tier keeps its own remaining size."""

    __slots__ = ("price", "ahead", "joined", "remaining")

    def __init__(self, price: float, ahead: float, joined: float, size: float) -> None:
        self.price = price
        self.ahead = ahead
        self.joined = joined
        self.remaining = dict.fromkeys(TIERS, size)


def _fills_for(order: Order, side: str, price: float, size: float, ts: float,
               market: dict) -> list[dict]:
    """side is our side: 'buy' for our bid, 'sell' for our ask. price/size = the print."""
    worse = price < order.price if side == "buy" else price > order.price
    at = abs(price - order.price) < 1e-9
    out: list[dict] = []

    def take(tier: str, qty: float) -> None:
        qty = min(qty, order.remaining[tier])
        if qty <= 0:
            return
        order.remaining[tier] -= qty
        out.append({"venue": market["venue"], "id": market["id"], "city": market["city"],
                    "date": market["date"], "ts": ts, "side": side, "price": order.price,
                    "size": qty, "tier": tier, "joined": order.joined,
                    "hours_to_close": (market["close_ts"] - ts) / 3600.0})

    if worse:
        for tier in TIERS:
            take(tier, order.remaining[tier])
    elif at:
        take("atlevel", size)
        order.ahead -= size
        if order.ahead < 0:
            take("queue", -order.ahead)
            order.ahead = 0.0
    return out


def simulate(markets: dict[tuple[str, str], dict], books: list[dict], trades: list[dict],
             quote_size: float = QUOTE_SIZE, window_end_sec: float = WINDOW_END_SEC) -> list[dict]:
    """Return one row per (partial) fill per tier."""
    events: dict[tuple[str, str], list[tuple[float, int, dict]]] = defaultdict(list)
    for b in books:
        events[(b["venue"], b["id"])].append((b["ts"], 0, b))     # book before trade at equal ts
    for t in trades:
        events[(t["venue"], t["id"])].append((t["ts"], 1, t))
    fills: list[dict] = []
    for key, evs in events.items():
        m = markets.get(key)
        if m is None:
            continue
        end = m["close_ts"] - window_end_sec
        bid: Order | None = None
        ask: Order | None = None
        for ts, kind, e in sorted(evs, key=lambda x: (x[0], x[1])):
            if ts > end:
                bid = ask = None
                continue
            if kind == 0:
                quoted_bid = e["bid_sz"] > 0 and 0.0 < e["bid"] < 1.0
                quoted_ask = e["ask_sz"] > 0 and 0.0 < e["ask"] < 1.0
                if not quoted_bid:
                    bid = None
                elif bid is None or bid.price != e["bid"]:
                    bid = Order(e["bid"], e["bid_sz"], ts, quote_size)
                if not quoted_ask:
                    ask = None
                elif ask is None or ask.price != e["ask"]:
                    ask = Order(e["ask"], e["ask_sz"], ts, quote_size)
            elif e["aggressor"] == "sell" and bid is not None:
                fills += _fills_for(bid, "buy", e["price"], e["size"], ts, m)
            elif e["aggressor"] == "buy" and ask is not None:
                fills += _fills_for(ask, "sell", e["price"], e["size"], ts, m)
    return fills


def settle(fills: list[dict], markets: dict[tuple[str, str], dict], books: list[dict]) -> None:
    """Attach settlement P&L (per contract, then x size) and markouts in place."""
    mids: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for b in books:
        if b["bid_sz"] > 0 and b["ask_sz"] > 0:
            mids[(b["venue"], b["id"])].append((b["ts"], (b["bid"] + b["ask"]) / 2.0))
    for v in mids.values():
        v.sort()
    for f in fills:
        m = markets[(f["venue"], f["id"])]
        sign = 1.0 if f["side"] == "buy" else -1.0
        p = f["price"]
        f["result"] = m["result"]
        if m["result"] in ("yes", "no"):
            y = 1.0 if m["result"] == "yes" else 0.0
            gross = sign * (y - p)
            fee = kalshi_maker_fee(p) if f["venue"] == "kalshi" else 0.0
            reb = poly_rebate(p) if f["venue"] == "poly" else 0.0
            f["pnl"] = (gross - fee) * f["size"]
            f["pnl_rebate"] = (gross - fee + reb) * f["size"]
        else:
            f["pnl"] = f["pnl_rebate"] = None
        series = mids.get((f["venue"], f["id"]), [])
        for sec in MARKOUT_SEC:
            later = [mid for ts, mid in series if ts >= f["ts"] + sec]
            f[f"markout_{sec // 3600}h"] = sign * (later[0] - p) if later else None


def load() -> tuple[dict, list[dict], list[dict]]:
    def rows(name: str) -> list[dict]:
        p = CACHE / name
        return [json.loads(line) for line in p.open()] if p.exists() else []
    markets = {(m["venue"], m["id"]): m for m in rows("markets.jsonl")}
    return markets, rows("books.jsonl"), rows("trades.jsonl")


def main() -> None:
    markets, books, trades = load()
    fills = simulate(markets, books, trades)
    settle(fills, markets, books)
    with (CACHE / "fills.jsonl").open("w") as f:
        for r in fills:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    by_tier = defaultdict(float)
    for r in fills:
        by_tier[r["tier"]] += r["size"]
    print(f"markets={len(markets)} books={len(books)} prints={len(trades)} "
          f"fills(contracts) " + " ".join(f"{t}={by_tier[t]:.0f}" for t in TIERS))


if __name__ == "__main__":
    main()
