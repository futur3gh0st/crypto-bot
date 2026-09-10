#!/usr/bin/env python3
"""Paper-only 6-month dip_hold backtest.

Reuses swing_backtest.simulate_book exactly (same -2% / 3-close rule).
Does not retune. No live orders. Fills only on real public 1h OHLC.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Import the 30d engine so dip_hold logic is identical (not a rewrite).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import swing_backtest as sb  # noqa: E402

# Locked 6-month score window. Last closed hour matches the 30d test.
WINDOW_START = datetime(2026, 2, 15, 0, 0, tzinfo=timezone.utc)
WINDOW_END_BAR_OPEN = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
# A few complete UTC days before the window so Feb-14 close can signal a
# Feb-15 open fill (needs Feb-13 as the prior close for that return).
WARMUP_START = datetime(2026, 2, 10, 0, 0, tzinfo=timezone.utc)

# Override module-level window used inside simulate_book / raw_buy_hold.
sb.WINDOW_START = WINDOW_START
sb.WINDOW_END_BAR_OPEN = WINDOW_END_BAR_OPEN
sb.WARMUP_START = WARMUP_START

# 30d reference (do not retune toward these).
REF_30D = {
    "four25": 400.85,
    "btc100": 213.62,
    "start": "2026-07-16",
    "end": "2026-08-15",
}

STRATEGIES = ("dip_hold",)
BOOKS = ("btc100", "four25")


def monthly_pnl(result: sb.BookResult) -> list[dict]:
    buckets: dict[str, float] = defaultdict(float)
    n_days: dict[str, int] = defaultdict(int)
    for row in result.days:
        ym = row.date[:7]
        buckets[ym] += row.pnl
        n_days[ym] += 1
    return [
        {"month": m, "pnl": buckets[m], "n_days": n_days[m]}
        for m in sorted(buckets)
    ]


def print_table(results: list[sb.BookResult]) -> None:
    headers = (
        "strategy",
        "book",
        "start",
        "end",
        "end_eq",
        "pnl",
        "ret%",
        "trades",
        "win%",
        "avg_day",
        "best",
        "worst",
        "maxDD%",
        "maxDD$",
        "fees",
    )
    rows: list[list[str]] = []
    for r in results:
        rows.append(
            [
                r.strategy,
                r.book,
                r.start.strftime("%Y-%m-%d"),
                r.end.strftime("%Y-%m-%d"),
                f"{r.ending_equity:,.2f}",
                f"{r.total_pnl:+,.2f}",
                f"{r.total_return * 100:+.2f}",
                str(r.n_trades),
                f"{r.win_rate * 100:.1f}",
                f"{r.avg_day_pnl:+,.2f}",
                f"{r.best_day['pnl']:+,.2f}",
                f"{r.worst_day['pnl']:+,.2f}",
                f"{r.max_drawdown * 100:.2f}",
                f"{r.max_drawdown_usd:,.2f}",
                f"{r.fees_paid:,.2f}",
            ]
        )
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cols: list[str]) -> str:
        left = {0, 1, 2, 3}
        return "  ".join(
            c.ljust(widths[i]) if i in left else c.rjust(widths[i])
            for i, c in enumerate(cols)
        )

    print()
    print(
        "PAPER dip_hold  $10,000  ~6m  no live orders  no leverage  "
        "rule NOT retuned"
    )
    print(fmt(list(headers)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))
    print()
    print("Fees: 12 bps one-way (10 taker + 1 half-spread) each side. Long-only.")
    print("Signal on UTC daily close, fill next day's open. Exit on close.")
    print("dip_hold: prior daily ret <= -2%, hold 3 daily closes or recover")
    print("above the signal day's open, whichever first. No stacking.")


def main() -> int:
    now = datetime.now(timezone.utc)
    print(
        f"swing 6m paper backtest  window {sb._iso(WINDOW_START)} → "
        f"{sb._iso(WINDOW_END_BAR_OPEN)}  warmup from {sb._iso(WARMUP_START)}  "
        f"now={sb._iso(now)}",
        flush=True,
    )
    print(
        "Honesty: paper only, no live orders, no lookahead, "
        "dip_hold -2% / 3-close rule copied from 30d test, not retuned.",
        flush=True,
    )

    h1: dict[str, list[sb.Bar]] = {}
    sources: list[str] = []
    skipped: list[str] = []

    # Fetch real 1h klines for the full warmup+score window. Do not invent bars.
    # fetch_klines paginates (1000-bar limit, up to 20 pages — enough for ~6m).
    fetch_end = WINDOW_END_BAR_OPEN
    for sym in sb.SYMBOLS:
        try:
            bars, src = sb.fetch_klines(sym, "1h", WARMUP_START, fetch_end)
            # Keep bars whose open is in [warmup, last closed hour].
            bars = [
                b
                for b in bars
                if WARMUP_START <= b.open_time <= WINDOW_END_BAR_OPEN
            ]
            if not bars:
                skipped.append(f"{sym}: empty after window filter")
                print(f"SKIP {sym}: empty after window filter", flush=True)
                continue
            h1[sym] = bars
            sources.append(
                f"{sym} 1h {src} n={len(bars)} "
                f"first={sb._iso(bars[0].open_time)} last={sb._iso(bars[-1].open_time)}"
            )
            print(sources[-1], flush=True)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{sym}: {exc}")
            print(f"SKIP {sym}: {exc}", flush=True)

    if "BTCUSDT" not in h1:
        print("ERROR: no BTCUSDT klines; refusing to invent fills/PnL", file=sys.stderr)
        return 2

    for sym, bars in list(h1.items()):
        in_win = [
            b
            for b in bars
            if WINDOW_START <= b.open_time <= WINDOW_END_BAR_OPEN
        ]
        if not in_win:
            skipped.append(f"{sym}: no in-window bars")
            del h1[sym]
            continue
        print(
            f"{sym} window_n={len(in_win)} first={sb._iso(in_win[0].open_time)} "
            f"last={sb._iso(in_win[-1].open_time)}",
            flush=True,
        )

    daily: dict[str, list[sb.DailyBar]] = {
        s: sb.resample_daily(bars) for s, bars in h1.items()
    }
    for s, series in daily.items():
        complete = sum(1 for d in series if d.complete)
        pre = sum(1 for d in series if d.complete and d.open_time < WINDOW_START)
        print(
            f"{s} daily n={len(series)} complete={complete} "
            f"complete_before_window={pre} first={series[0].date} "
            f"last={series[-1].date} last_complete={series[-1].complete} "
            f"last_hours={series[-1].n_hours}",
            flush=True,
        )
        if pre < 1:
            skipped.append(
                f"{s}: no complete UTC day before window (first dip signal needs prior close)"
            )

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    data_source = sb.VISION_KLINES
    for line in sources:
        if sb.VISION_KLINES in line:
            data_source = sb.VISION_KLINES
            break
        if sb.BINANCE_KLINES in line:
            data_source = sb.BINANCE_KLINES
            break
        if sb.OKX_CANDLES in line:
            data_source = sb.OKX_CANDLES
            break

    sb.OUT_DIR.mkdir(parents=True, exist_ok=True)
    merged_cache = {
        "fetched_at": sb._iso(now),
        "window_start": sb._iso(WINDOW_START),
        "window_end_bar_open": sb._iso(WINDOW_END_BAR_OPEN),
        "warmup_start": sb._iso(WARMUP_START),
        "horizon": "6m",
        "strategy_filter": ["dip_hold"],
        "sources": sources,
        "skipped": skipped,
        "bars_1h": {
            s: [
                {
                    "open_time": sb._iso(b.open_time),
                    "close_time": sb._iso(b.close_time),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in series
            ]
            for s, series in h1.items()
        },
    }
    merged_path = sb.OUT_DIR / f"swing_6m_klines_{stamp}.json"
    merged_path.write_text(json.dumps(merged_cache))
    print(f"wrote merged kline cache {merged_path}", flush=True)

    raw = sb.raw_buy_hold(h1)
    print("raw buy-hold (first open → last close, no fees):", flush=True)
    for s, row in raw.items():
        print(
            f"  {s:10s}  {row['return_pct']:+.3f}%  "
            f"{row['first_open']} → {row['last_close']}",
            flush=True,
        )

    results: list[sb.BookResult] = []
    files: list[dict[str, str]] = []
    monthly: dict[str, list[dict]] = {}
    for strat in STRATEGIES:
        for book in BOOKS:
            if book == "btc100" and "BTCUSDT" not in h1:
                print(f"SKIP {strat} {book}: no BTCUSDT", flush=True)
                continue
            if book == "four25" and not any(s in h1 for s in sb.SYMBOLS):
                print(f"SKIP {strat} {book}: no symbols", flush=True)
                continue
            res = sb.simulate_book(strat, book, h1, daily, data_source, skipped)
            jp, cp = sb.save_result(res, stamp)
            # Prefix-rename would collide with 30d names only if dates match;
            # 6m dates differ so swing_dip_hold_*_2026-02-15_2026-08-15_* is unique.
            results.append(res)
            files.append(
                {
                    "strategy": strat,
                    "book": book,
                    "json": str(jp),
                    "csv": str(cp),
                }
            )
            monthly[f"{strat}_{book}"] = monthly_pnl(res)
            print(
                f"{strat:16s} {book:8s} end=${res.ending_equity:,.2f} "
                f"pnl={res.total_pnl:+,.2f} trades={res.n_trades} "
                f"avg_day={res.avg_day_pnl:+,.2f} -> {jp.name}",
                flush=True,
            )

    print_table(results)

    print("monthly PnL (sum of daily marked PnL):", flush=True)
    for key, rows in monthly.items():
        print(f"  {key}", flush=True)
        for row in rows:
            print(
                f"    {row['month']}  {row['pnl']:+,.2f}  ({row['n_days']} days)",
                flush=True,
            )

    # One-line vs 30d (same rule, different window).
    by_book = {r.book: r for r in results}
    f25 = by_book.get("four25")
    btc = by_book.get("btc100")
    cmp_line = (
        "vs 30d (same rule, not retuned): "
        f"6m four25 {f25.total_pnl:+,.2f} vs 30d +{REF_30D['four25']:.2f}; "
        f"6m btc100 {btc.total_pnl:+,.2f} vs 30d +{REF_30D['btc100']:.2f} "
        f"({REF_30D['start']} to {REF_30D['end']})."
        if f25 and btc
        else "vs 30d: missing a 6m book; 30d was four25 +$400.85, btc100 +$213.62."
    )
    print(cmp_line, flush=True)

    extra = {
        "fetched_at": sb._iso(now),
        "horizon": "6m",
        "window_start": sb._iso(WINDOW_START),
        "window_end_bar_open": sb._iso(WINDOW_END_BAR_OPEN),
        "last_closed_hour": sb._iso(WINDOW_END_BAR_OPEN),
        "warmup_start": sb._iso(WARMUP_START),
        "data_source": data_source,
        "sources": sources,
        "skipped": skipped,
        "kline_cache": str(merged_path),
        "fee_assumption": "10 bps taker + 1 bp half-spread each side (12 bps one-way)",
        "starting_balance": sb.STARTING_BALANCE,
        "leverage": 1.0,
        "paper_only": True,
        "live_orders": False,
        "curve_fit": False,
        "parameters_optimized_after_results": False,
        "rule_retuned": False,
        "dip_ret": sb.DIP_RET,
        "dip_hold_closes": sb.DIP_HOLD_CLOSES,
        "honesty": (
            "Paper only. No live orders. No lookahead. "
            "dip_hold rule matches the 30d test exactly "
            "(prior daily ret <= -2%, exit after 3 daily closes or close "
            "above signal-day open, long-only, no stacking). "
            "Not retuned to win. PnL from public OHLC fills only."
        ),
        "raw_buy_hold_no_fees": raw,
        "monthly_pnl": monthly,
        "vs_30d": {
            "window": f"{REF_30D['start']} to {REF_30D['end']}",
            "four25": REF_30D["four25"],
            "btc100": REF_30D["btc100"],
            "line": cmp_line,
        },
        "books_summary": [
            {
                "strategy": r.strategy,
                "book": r.book,
                "start": sb._iso(r.start),
                "end": sb._iso(r.end),
                "ending_equity": r.ending_equity,
                "total_pnl": r.total_pnl,
                "total_return": r.total_return,
                "n_trades": r.n_trades,
                "win_rate": r.win_rate,
                "avg_day_pnl": r.avg_day_pnl,
                "best_day": r.best_day,
                "worst_day": r.worst_day,
                "max_drawdown": r.max_drawdown,
                "max_drawdown_usd": r.max_drawdown_usd,
                "fees_paid": r.fees_paid,
            }
            for r in results
        ],
    }
    cmp_path = sb.OUT_DIR / f"swing_6m_dip_hold_comparison_{stamp}.json"
    cmp_path.write_text(
        json.dumps(
            {
                **extra,
                "books": [r.to_dict() for r in results],
                "files": files,
            },
            indent=2,
        )
    )

    print(f"comparison json: {cmp_path}")
    for f in files:
        print(f"  {f['strategy']:16s} {f['book']:8s}  {f['json']}")
        print(f"  {'':16s} {'':8s}  {f['csv']}")
    if skipped:
        print("skipped / warnings:")
        for s in skipped:
            print(f"  - {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
