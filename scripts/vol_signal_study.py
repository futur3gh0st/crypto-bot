#!/usr/bin/env python3
"""Does the vol-aware fair beat the crude linear fair? Measured, not asserted.

Walks real Binance 1m bars, reconstructs the same 5m/15m Up-Down windows the
bot trades, and at each minute inside a window asks both models for P(close >
open). Volatility is estimated only from bars strictly before the observation,
so there is no lookahead. Scores both against what actually happened.

  Brier score  = mean (predicted - outcome)^2. Lower is better. 0.25 is the
                 score of always saying 0.50, so anything above that is worse
                 than not having a model at all.
  Calibration  = within each predicted-probability bucket, how often the
                 outcome actually occurred. A good model tracks the diagonal.

Paper research only. No orders, no live path.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stablebot.desk.signal import VolTracker, vol_fair_up  # noqa: E402
from stablebot.poly.fair import crude_fair_up  # noqa: E402

VISION = "https://data-api.binance.vision/api/v3/klines"
UA = "stablebot/0.1 (research; paper-only; no live orders)"
COINS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
}


def fetch(symbol: str, pulls: int = 6) -> list[dict]:
    out: list[list] = []
    end = None
    for _ in range(pulls):
        url = f"{VISION}?symbol={symbol}&interval=1m&limit=1000"
        if end:
            url += f"&endTime={end}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            k = json.loads(r.read())
        if not k:
            break
        out = k + out
        end = int(k[0][0]) - 1
    return [
        {
            "open_ms": int(b[0]),
            "open": float(b[1]),
            "close": float(b[4]),
            "close_ms": int(b[6]),
        }
        for b in out
    ]


def brier(preds: list[tuple[float, int]]) -> float:
    if not preds:
        return float("nan")
    return sum((p - o) ** 2 for p, o in preds) / len(preds)


def log_loss(preds: list[tuple[float, int]]) -> float:
    if not preds:
        return float("nan")
    eps = 1e-6
    tot = 0.0
    for p, o in preds:
        p = min(1 - eps, max(eps, p))
        tot += -(o * math.log(p) + (1 - o) * math.log(1 - p))
    return tot / len(preds)


def calibration(preds: list[tuple[float, int]], bins: int = 10) -> list[tuple]:
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, o in preds:
        idx = min(bins - 1, int(p * bins))
        buckets[idx].append((p, o))
    rows = []
    for i, b in enumerate(buckets):
        if not b:
            continue
        rows.append(
            (
                f"{i/bins:.1f}-{(i+1)/bins:.1f}",
                len(b),
                sum(p for p, _ in b) / len(b),
                sum(o for _, o in b) / len(b),
            )
        )
    return rows


def study(coin: str, minutes: int, pulls: int, halflife: float) -> dict:
    symbol = COINS[coin]
    bars = fetch(symbol, pulls)
    if len(bars) < 200:
        return {"coin": coin, "error": f"only {len(bars)} bars"}

    interval = minutes * 60
    vt = VolTracker(halflife=halflife)

    # group bars by window
    windows: dict[int, list[dict]] = {}
    for b in bars:
        ws = (b["open_ms"] // 1000 // interval) * interval
        windows.setdefault(ws, []).append(b)

    crude_preds: list[tuple[float, int]] = []
    vol_preds: list[tuple[float, int]] = []
    fires_z = 0
    fires_pct = 0
    observations = 0
    prev_close: float | None = None
    sigma_samples: list[float] = []

    for ws in sorted(windows):
        wb = sorted(windows[ws], key=lambda x: x["open_ms"])
        if len(wb) < minutes:
            continue  # partial window at either end
        w_open = wb[0]["open"]
        w_close = wb[-1]["close"]
        if w_open <= 0:
            continue
        outcome = 1 if w_close > w_open else 0
        if w_close == w_open:
            continue  # FLAT scratches; not a directional outcome

        for i, b in enumerate(wb):
            # vol from bars strictly before this one
            if prev_close is not None and prev_close > 0 and b["close"] > 0:
                sigma_now = vt.sigma(symbol)
                ret_1m = math.log(b["close"] / prev_close)
                elapsed_min = i + 1
                remaining = minutes - elapsed_min
                if sigma_now is not None and remaining >= 1:
                    observations += 1
                    sigma_samples.append(sigma_now)
                    spot = b["close"]
                    try:
                        vf = vol_fair_up(spot, w_open, sigma_now, remaining)
                    except ValueError:
                        vf = 0.5
                    cf = crude_fair_up(spot, w_open, scale=25.0)
                    vol_preds.append((vf, outcome))
                    crude_preds.append((cf, outcome))
                    if abs(ret_1m) >= 2.5 * sigma_now:
                        fires_z += 1
                    if abs(ret_1m) >= 0.003:
                        fires_pct += 1
                vt.update(symbol, ret_1m)
            prev_close = b["close"]

    med_sigma = sorted(sigma_samples)[len(sigma_samples) // 2] if sigma_samples else float("nan")
    return {
        "coin": coin,
        "bars": len(bars),
        "windows": len(windows),
        "observations": observations,
        "median_sigma_1m": med_sigma,
        "brier_crude": brier(crude_preds),
        "brier_vol": brier(vol_preds),
        "logloss_crude": log_loss(crude_preds),
        "logloss_vol": log_loss(vol_preds),
        "fire_rate_z25": fires_z / observations if observations else 0.0,
        "fire_rate_pct30": fires_pct / observations if observations else 0.0,
        "_vol_preds": vol_preds,
        "_crude_preds": crude_preds,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", default="btc,eth,sol,xrp,doge,bnb")
    ap.add_argument("--window", type=int, default=5)
    ap.add_argument("--pulls", type=int, default=6, help="1000 bars each")
    ap.add_argument("--halflife", type=float, default=120.0)
    args = ap.parse_args(argv)

    coins = [c.strip().lower() for c in args.coins.split(",") if c.strip()]
    results = []
    all_vol: list[tuple[float, int]] = []
    all_crude: list[tuple[float, int]] = []

    print(f"window={args.window}m  vol halflife={args.halflife:.0f} bars  "
          f"(baseline Brier for always-0.50 = 0.2500)\n")
    hdr = (f"{'coin':6} {'obs':>7} {'sigma1m':>9} {'Brier crude':>12} {'Brier vol':>10} "
           f"{'better by':>10} {'fire@0.3%':>10} {'fire@2.5s':>10}")
    print(hdr)
    print("-" * len(hdr))
    for c in coins:
        if c not in COINS:
            continue
        r = study(c, args.window, args.pulls, args.halflife)
        if r.get("error"):
            print(f"{c:6} {r['error']}")
            continue
        all_vol.extend(r.pop("_vol_preds"))
        all_crude.extend(r.pop("_crude_preds"))
        imp = (r["brier_crude"] - r["brier_vol"]) / r["brier_crude"] * 100
        print(
            f"{c:6} {r['observations']:7d} {r['median_sigma_1m']*100:8.4f}% "
            f"{r['brier_crude']:12.4f} {r['brier_vol']:10.4f} {imp:9.1f}% "
            f"{r['fire_rate_pct30']*100:9.3f}% {r['fire_rate_z25']*100:9.3f}%"
        )
        results.append(r)

    if all_vol:
        print()
        print(f"POOLED  Brier crude {brier(all_crude):.4f}   Brier vol {brier(all_vol):.4f}   "
              f"always-0.50 {0.25:.4f}")
        print(f"POOLED  logloss crude {log_loss(all_crude):.4f}   logloss vol {log_loss(all_vol):.4f}")
        print("\nCalibration of the vol-aware fair (predicted vs actually happened):")
        print(f"  {'bucket':>10} {'n':>7} {'mean pred':>10} {'realised':>10}  {'':<22}")
        for name, n, pred, real in calibration(all_vol):
            gap = real - pred
            bar = "#" * int(abs(gap) * 60)
            print(f"  {name:>10} {n:7d} {pred:10.3f} {real:10.3f}  {gap:+.3f} {bar}")
        print("\nCalibration of the crude linear fair:")
        for name, n, pred, real in calibration(all_crude):
            gap = real - pred
            bar = "#" * int(abs(gap) * 60)
            print(f"  {name:>10} {n:7d} {pred:10.3f} {real:10.3f}  {gap:+.3f} {bar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
