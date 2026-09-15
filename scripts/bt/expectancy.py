#!/usr/bin/env python3
"""Does the claimed edge survive contact with real outcomes?

This is a thin front for `edgecheck` (https://github.com/futur3gh0st/edgecheck).
All the statistics live there. What lives here is the one thing edgecheck
cannot know: what each of this project's ledgers calls its columns, and
which of them mean what.

    python scripts/bt/expectancy.py data/kalshi_lag_ledger.jsonl
    python scripts/bt/expectancy.py data/bt_cache/kalshilag_trades.jsonl --bankroll 1000

Any extra flags are passed through to `edgecheck run` (--json, --deploy-size,
--by, --bootstrap, ...). Exit code is edgecheck's: 0 GO, 1 NO_GO, 3 NO_VERDICT.

Paper ledgers only. Reads nothing live and places no orders.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from edgecheck.cli import main as edgecheck_main
except ImportError:                                   # pragma: no cover
    sys.exit("edgecheck is not installed: .venv/bin/pip install 'edgecheck>=0.2.0'")

# Two shapes reach this tool.
#
# PAIRED  the live desk appends an entry row and, later, a resolve row. They
#         carry a "kind" and are joined on a key.
# FLAT    the backtest replays (scripts/bt/replay_*.py) emit one settled row per
#         trade, entry and outcome together, with no "kind" at all.
#
# The probability field does NOT mean the same thing in every ledger, and the
# difference is invisible until a trade takes the "no" side:
#
#   live kalshi_lag   "fair" is P(YES). Verified against its own rows: edge
#                     reconciles as (1 - fair) - entry_p - fee on a no-side
#                     trade, and as fair - entry_p - fee on a yes-side trade.
#   replay_kalshilag  "fair" is already P(side taken).
#   spot_lag          "fair_side" is already P(side taken).
#
# Clustering: every Kalshi 15-minute window settles on one price print, so
# trades in the same window -- across coins -- share an outcome. `close_ts`
# is that window. Spot-lag windows are `window_end`.
LEDGERS: dict[str, list[str]] = {
    "kalshi_lag": [
        "--kind", "kind", "--entry-kind", "kalshi_lag", "--resolve-kind", "kalshi_lag_resolve",
        "--key", "ticker", "--pnl", "pnl_after_fee", "--cost", "fee", "--won", "won",
        "--predicted", "fair", "--side", "side", "--predicted-is-yes",
        "--claimed", "edge", "--size", "shares", "--price", "entry_p",
        "--depth", "ask_size", "--time", "ts", "--cluster", "close_ts",
    ],
    "spot_lag": [
        "--kind", "kind", "--entry-kind", "spot_lag", "--resolve-kind", "spot_lag_resolve",
        "--key", "slug", "--pnl", "pnl_after_fee", "--cost", "fee", "--won", "won",
        "--predicted", "fair_side", "--side", "side",
        "--claimed", "edge", "--size", "shares", "--price", "entry_p",
        "--time", "ts", "--cluster", "window_end",
    ],
}

# replay_kalshilag computes pnl = payout - cost - fee, so it is already net.
FLAT_LEDGERS: dict[str, list[str]] = {
    "kalshilag_trades": [
        "--pnl", "pnl", "--cost", "fee", "--won", "won", "--predicted", "fair", "--side", "side",
        "--claimed", "edge", "--size", "shares", "--price", "ask",
        "--time", "signal_ts", "--cluster", "close_ts", "--key", "ticker",
    ],
    "spotlag_trades": [
        "--pnl", "pnl", "--cost", "fee", "--won", "won", "--predicted", "fair_side", "--side", "side",
        "--claimed", "edge", "--size", "shares",
        "--time", "signal_ts", "--cluster", "window_end",
    ],
}


def detect(rows: list[dict]) -> tuple[str, list[str]]:
    """-> (ledger name, edgecheck column flags). A row with no "kind" is a
    settled backtest trade."""
    kinds = {r.get("kind") for r in rows}
    for name, flags in LEDGERS.items():
        if name in kinds:
            return name, flags
    sample = rows[0]
    if "kind" not in sample and "won" in sample:
        if "fair_side" in sample:
            return "spotlag_trades", FLAT_LEDGERS["spotlag_trades"]
        if "fair" in sample:
            return "kalshilag_trades", FLAT_LEDGERS["kalshilag_trades"]
        raise SystemExit(
            "flat ledger with no recognised probability field; "
            f"has: {sorted(sample)[:12]}"
        )
    raise SystemExit(f"unrecognised ledger; kinds present: {sorted(k for k in kinds if k)}")


def read(path: Path) -> list[dict]:
    rows = []
    for line in path.open():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # a partial final line while the desk is appending
    return rows


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0].startswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    ledger = Path(args[0])
    if not ledger.exists():
        print(f"no such ledger: {ledger}", file=sys.stderr)
        return 2
    rows = read(ledger)
    if not rows:
        print(f"empty ledger: {ledger}", file=sys.stderr)
        return 2
    name, flags = detect(rows)
    print(f"  ledger {ledger}  shape {name}", file=sys.stderr)
    return edgecheck_main(["run", str(ledger), *flags, *args[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
