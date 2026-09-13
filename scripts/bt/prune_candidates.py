#!/usr/bin/env python3
"""Drop candidates the replay will reject anyway, before paying to fetch their book.

replay_kalshilag applies its gates in a fixed order. Three of them -- no_reference,
reference_noise, no_vol -- depend only on the reference composite and the candidate
row, never on the candlesticks. Running them first means the candle fetch only pays
for markets that can still become a trade.

This changes nothing about the result: the same gates, the same parameters, the same
order, just evaluated earlier. Anything dropped here would have been dropped there.

Kalshi rate-limits hard, and the candle fetch is the only expensive stage, so this is
the difference between a multi-hour pull and a short one.

    python scripts/bt/prune_candidates.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stablebot.desk.kalshi_lag import LagParams
from stablebot.desk.signal import SignalCfg, distance_is_measurable, fair_with_reference_noise

# Reuse the replay's own loaders and reference logic rather than restating them --
# a second implementation is a second thing that can drift.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_kalshilag import load_reference, reference_at  # noqa: E402

CACHE = ROOT / "data" / "bt_cache"
SRC = CACHE / "kalshilag_candidates.jsonl"
OUT = CACHE / "kalshilag_candidates_pruned.jsonl"


def main() -> int:
    p, sig = LagParams(), SignalCfg()
    ref = load_reference()
    if not ref:
        print("no reference data -- run scripts/bt/fetch_reference.py first", file=sys.stderr)
        return 2

    cands = [json.loads(l) for l in SRC.open() if l.strip()]
    gates: dict[str, int] = {}
    kept = []

    def gate(k: str) -> None:
        gates[k] = gates.get(k, 0) + 1

    for r in cands:
        refpx, disp = reference_at(ref, r["coin"], r["signal_ts"])
        if refpx is None:
            gate("no_reference"); continue
        ref_sigma = max(p.min_ref_sigma_bp, disp or 0.0) / 1e4
        ok, _ratio = distance_is_measurable(refpx, r["strike"], ref_sigma, sig.min_distance_ratio)
        if not ok:
            gate("reference_noise"); continue
        rem_min = max(r["remaining"] / 60.0, 1 / 60.0)
        try:
            fair_with_reference_noise(refpx, r["strike"], r["sigma"], rem_min, ref_sigma)
        except ValueError:
            gate("no_vol"); continue
        gate("KEPT"); kept.append(r)

    with OUT.open("w") as f:
        for r in kept:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")

    uniq = len({(r["series"], r["ticker"]) for r in kept})
    uniq_all = len({(r["series"], r["ticker"]) for r in cands})
    print(f"  candidates in    {len(cands):,}   ({uniq_all:,} unique markets)")
    for k, v in sorted(gates.items(), key=lambda kv: -kv[1]):
        print(f"    {k:18} {v:,}")
    print(f"  kept             {len(kept):,}   ({uniq:,} unique markets to fetch)")
    if uniq_all:
        print(f"  fetch reduced to {100 * uniq / uniq_all:.1f}% of the original pull")
    print(f"  -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
