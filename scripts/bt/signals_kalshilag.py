#!/usr/bin/env python3
"""Kalshi lag signal candidates from cached klines + settled market list.

Mirrors KalshiLagPaper._maybe_enter's pre-book gates: EWMA vol updated per
closed bar, |z| >= z_entry, then the soonest market with remaining time inside
[min_remaining_sec, max_remaining_sec].
"""
from __future__ import annotations
import json, math, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from stablebot.desk.signal import SignalCfg, VolTracker
from stablebot.desk.kalshi_lag import COIN_SERIES, LagParams
from stablebot.poly.markets import COIN_SPOT

CACHE = ROOT / "data" / "bt_cache"
OUT = CACHE / "kalshilag_candidates.jsonl"


def ts(raw):
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()


def load_bars(sym):
    rows = []
    with (CACHE / "klines" / f"{sym}_1m.jsonl").open() as f:
        for line in f:
            o, _op, _h, _l, c, ct = json.loads(line)
            rows.append((o // 1000, c, ct // 1000))
    rows.sort()
    return rows


def load_markets(series):
    p = CACHE / "kalshi" / f"{series}_markets.jsonl"
    if not p.exists():
        return []
    out = []
    for l in p.open():
        m = json.loads(l)
        try:
            close_ts = ts(m["close_time"])
        except Exception:
            continue
        strike = None
        for k in ("floor_strike", "cap_strike"):
            v = m.get(k)
            if v is not None:
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                if f > 0:
                    strike = f; break
        if strike is None or not m.get("result"):
            continue
        out.append({"ticker": m["ticker"], "close_ts": close_ts, "strike": strike,
                    "result": m["result"], "open_ts": ts(m["open_time"])})
    out.sort(key=lambda x: x["close_ts"])
    return out


def main():
    sig, p = SignalCfg(), LagParams()
    rows = []
    for coin, series in COIN_SERIES.items():
        mkts = load_markets(series)
        if not mkts:
            print(f"  {coin}/{series}: no settled markets"); continue
        sym = COIN_SPOT.get(coin)
        if not sym or not (CACHE / "klines" / f"{sym}_1m.jsonl").exists():
            print(f"  {coin}: no klines cached"); continue
        bars = load_bars(sym)
        lo = mkts[0]["close_ts"] - 3600
        vol = VolTracker(halflife=sig.vol_halflife)
        closes = sorted(m["close_ts"] for m in mkts)
        by_close = {m["close_ts"]: m for m in mkts}
        n = 0
        import bisect
        for i in range(1, len(bars)):
            _o, c, ct = bars[i]
            pc = bars[i - 1][1]
            if c <= 0 or pc <= 0:
                continue
            ret = math.log(c / pc)
            vol.update(sym, ret)
            s = vol.sigma(sym)
            if s is None or s <= 0 or ct < lo:
                continue
            z = ret / s
            if abs(z) < sig.z_entry:
                continue
            j = bisect.bisect_left(closes, ct + p.min_remaining_sec)
            if j >= len(closes):
                continue
            m = by_close[closes[j]]
            rem = m["close_ts"] - ct
            if not (p.min_remaining_sec <= rem <= p.max_remaining_sec):
                continue
            rows.append({"coin": coin, "symbol": sym, "series": series,
                         "ticker": m["ticker"], "signal_ts": ct, "z": z, "sigma": s,
                         "spot": c, "strike": m["strike"], "close_ts": m["close_ts"],
                         "remaining": rem, "result": m["result"],
                         "open_ts": m["open_ts"]})
            n += 1
        print(f"  {coin}: {n} signal candidates over {len(mkts)} markets", flush=True)
    rows.sort(key=lambda r: r["signal_ts"])
    with OUT.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    print(f"TOTAL kalshi_lag candidates: {len(rows)} -> {OUT}")


main()
