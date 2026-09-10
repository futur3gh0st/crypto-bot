#!/usr/bin/env python3
"""Stage A: spot_lag signal candidates from cached Binance 1m klines.

Replicates VolSpotLagPaper._maybe_enter's pre-quote gates exactly:
  * EWMA vol (halflife 120 bars), updated on each new closed bar, then read
  * z = log(close/prev_close) / sigma  must clear signal.z_entry
  * the signal bar must open inside the current window
  * elapsed in (0, max_elapsed_sec];  remaining >= min_remaining_sec
Emits one row per firing window. Ask/edge/concurrency gates come later.
"""
from __future__ import annotations

import json, math, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stablebot.desk.signal import SignalCfg, VolTracker, vol_fair_up
from stablebot.poly.markets import COIN_SPOT

CACHE = ROOT / "data" / "bt_cache" / "klines"
OUT = ROOT / "data" / "bt_cache" / "spotlag_candidates.jsonl"


def load(symbol: str) -> list[tuple[int, float, float, int]]:
    """(open_time_s, open, close, close_time_s)"""
    rows = []
    with (CACHE / f"{symbol}_1m.jsonl").open() as f:
        for line in f:
            o_ms, o, h, l, c, ct_ms = json.loads(line)
            rows.append((o_ms // 1000, o, c, ct_ms // 1000))
    rows.sort()
    return rows


def run(coins: list[str], minutes: int, sig: SignalCfg) -> list[dict]:
    out = []
    interval = minutes * 60
    for coin in coins:
        symbol = COIN_SPOT[coin]
        bars = load(symbol)
        vol = VolTracker(halflife=sig.vol_halflife)
        # window open price = open of the 1m bar starting at window_start
        open_at = {o: op for o, op, _c, _ct in bars}
        n_fire = n_bar = 0
        for i in range(1, len(bars)):
            o, _op, close, ct = bars[i]
            prev_close = bars[i - 1][2]
            if close <= 0 or prev_close <= 0:
                continue
            ret = math.log(close / prev_close)
            vol.update(symbol, ret)          # desk updates, then reads
            sigma = vol.sigma(symbol)
            if sigma is None or sigma <= 0:
                continue
            n_bar += 1
            z = ret / sigma
            if abs(z) < sig.z_entry:
                continue
            now_ts = ct                       # cycle fires right after the bar closes
            w_start = (now_ts // interval) * interval
            w_end = w_start + interval
            if not (w_start <= o < w_end):
                continue
            elapsed = now_ts - w_start
            remaining = w_end - now_ts
            if not (0 < elapsed <= sig.max_elapsed_sec):
                continue
            if remaining < sig.min_remaining_sec:
                continue
            open_px = open_at.get(w_start)
            if not open_px or open_px <= 0:
                continue
            direction = "UP" if ret > 0 else "DOWN"
            side = "up" if direction == "UP" else "down"
            try:
                fair_up = vol_fair_up(close, open_px, sigma, max(remaining / 60.0, 1 / 60.0))
            except ValueError:
                continue
            fair_side = fair_up if direction == "UP" else (1.0 - fair_up)
            out.append({
                "coin": coin, "symbol": symbol, "minutes": minutes,
                "slug": f"{coin}-updown-{minutes}m-{w_start}",
                "window_start": w_start, "window_end": w_end,
                "signal_ts": now_ts, "elapsed": elapsed, "remaining": remaining,
                "z": z, "sigma": sigma, "ret": ret, "direction": direction, "side": side,
                "spot": close, "open_px": open_px,
                "fair_up": fair_up, "fair_side": fair_side,
            })
            n_fire += 1
        print(f"  {coin}: {n_bar} bars scored, {n_fire} windows fired "
              f"({100.0*n_fire/max(n_bar,1):.2f}%)", flush=True)
    out.sort(key=lambda r: r["signal_ts"])
    return out


if __name__ == "__main__":
    coins = ["btc", "eth", "sol", "xrp", "doge", "bnb"]
    sig = SignalCfg()
    print(f"spot_lag signal scan: {sig.describe()}  windows=5m")
    rows = run(coins, 5, sig)
    with OUT.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    print(f"TOTAL candidates: {len(rows)} -> {OUT}")
