#!/usr/bin/env python3
"""Watch the candle fetch, then drive replay + expectancy, publishing status as it goes.

Writes data/bt_cache/status.json on every tick so a browser can show live progress
without anyone having to tail a log. Stages:

    fetching   -> candles arriving; progress = markets cached / markets wanted
    replaying  -> replay_kalshilag over the real Kalshi book
    analysing  -> expectancy.py over the resulting trades
    done       -> results embedded in the status file
    failed     -> stage + stderr embedded, so a failure is visible, not silent

Deliberately observational: it never touches the fetch process, so killing either
one leaves the other intact and the fetch stays resumable.

    python scripts/bt/pipeline_watch.py
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache"
STATUS = CACHE / "status.json"
PRUNED = CACHE / "kalshilag_candidates_pruned.jsonl"
CANDLES = CACHE / "kalshilag_candles.jsonl"
TRADES = CACHE / "kalshilag_trades.jsonl"
PY = str(ROOT / ".venv" / "bin" / "python")

TICK = 5.0
started = time.time()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write(**kw) -> None:
    kw.setdefault("heartbeat", now_iso())
    kw.setdefault("elapsed_sec", round(time.time() - started))
    STATUS.write_text(json.dumps(kw, indent=2))


def wanted() -> int:
    seen = set()
    if not PRUNED.exists():
        return seen
    for l in PRUNED.open():
        try:
            r = json.loads(l)
        except json.JSONDecodeError:
            continue      # a partially-written final line while the fetch appends
        seen.add((r["series"], r["ticker"]))
    return seen


def cached(want: set[tuple[str, str]]) -> tuple[int, int]:
    """-> (markets we wanted and have, of those, how many carry a book).

    Counted against `want`, not by raw row count. The candles file also holds
    rows from the earlier unpruned pass, and counting those made progress read
    100.2% while real coverage was 88.5%.
    """
    if not CANDLES.exists():
        return 0, 0
    have = withbook = 0
    seen: set[tuple[str, str]] = set()
    for l in CANDLES.open():
        try:
            d = json.loads(l)
        except json.JSONDecodeError:
            continue      # same: we read this file while it is being appended to
        k = (d.get("series"), d.get("ticker"))
        if k not in want or k in seen:
            continue
        seen.add(k)
        have += 1
        if d.get("c"):
            withbook += 1
    return have, withbook


def fetch_alive() -> bool:
    r = subprocess.run(["pgrep", "-f", "fetch_kalshi_candles"],
                       capture_output=True, text=True, check=False)
    return bool(r.stdout.strip())


def run(label: str, argv: list[str], timeout: int = 3600) -> tuple[bool, str]:
    try:
        p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                           timeout=timeout, check=False)   # returncode handled below
    except subprocess.TimeoutExpired:
        return False, f"{label} timed out after {timeout}s"
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "")[-1200:]
    return True, p.stdout


def main() -> int:
    want = wanted()
    total = len(want)
    # Rows already on disk were fetched before this watchdog existed. Counting
    # them against our own elapsed time reports a rate an order of magnitude too
    # high and an ETA minutes too short, so rate is measured from the baseline.
    n0, _ = cached(want)
    write(stage="fetching", done=n0, total=total, baseline=n0,
          pct=round(100.0 * n0 / total, 1) if total else 0.0)

    # ---- stage 1: watch the fetch ----------------------------------------
    stall_ticks = 0
    while True:
        n, withbook = cached(want)
        alive = fetch_alive()
        pct = (100.0 * n / total) if total else 0.0
        gained = n - n0
        rate = gained / max(time.time() - started, 1e-9)
        eta = (total - n) / rate / 60 if rate > 0 and gained > 0 else None
        write(stage="fetching", done=n, total=total, baseline=n0,
              pct=round(pct, 1), with_book=withbook,
              rate_per_sec=round(rate, 2), fetched_this_run=gained,
              eta_min=round(eta, 1) if eta else None, fetch_running=alive)

        if not alive and n >= total:
            break
        if not alive and n < total:
            # process gone with work outstanding: report rather than hang
            if stall_ticks > 2:
                write(stage="failed", failed_at="fetching", done=n, total=total,
                      error=f"fetch exited with {total - n} markets outstanding; "
                            f"rerun fetch_kalshi_candles.py to resume")
                return 1
            stall_ticks += 1
        else:
            stall_ticks = 0
        time.sleep(TICK)

    # ---- stage 2: replay --------------------------------------------------
    n, withbook = cached(want)
    write(stage="replaying", done=n, total=total, pct=100.0, with_book=withbook)
    ok, out = run("replay", [PY, "scripts/bt/replay_kalshilag.py"])
    if not ok:
        write(stage="failed", failed_at="replaying", error=out)
        return 1
    replay_out = out

    trades = sum(1 for _ in TRADES.open()) if TRADES.exists() else 0

    # ---- stage 3: expectancy ---------------------------------------------
    write(stage="analysing", trades=trades, replay=replay_out[-2000:])
    ok, exp_out = run("expectancy", [PY, "scripts/bt/expectancy.py", str(TRADES)])
    if not ok:
        write(stage="failed", failed_at="analysing", error=exp_out, replay=replay_out[-2000:])
        return 1

    # ---- pull the headline figures out for the dashboard ------------------
    def grab(pattern: str, text: str) -> str | None:
        m = re.search(pattern, text)
        return m.group(1).strip() if m else None

    write(stage="done",
          trades=trades,
          markets_with_book=withbook,
          replay=replay_out[-2000:],
          expectancy=exp_out,
          headline={
              "total_pnl": grab(r"total P&L\s+([-+0-9,.]+)", exp_out),
              "mean_per_trade": grab(r"mean per trade\s+([-+0-9.]+)", exp_out),
              "ci": grab(r"95% CI[^\[]*(\[[^\]]+\])", exp_out),
              "verdict": grab(r"verdict\s+(.+)", exp_out),
              "realization": grab(r"realization ratio\s+([-0-9.]+%)", exp_out),
              "calibration_gap": grab(r"OVERALL[\s\S]*?([-+][0-9.]+%)\s*$", exp_out),
          })
    print(exp_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
