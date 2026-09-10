#!/usr/bin/env python3
"""Paper-only 6m screen of a SMALL FIXED set of simple daily rules.

Reuses swing_backtest.simulate_book for long-only books.
Does not retune dip_hold. Does not hunt parameters.
No live orders. Fills only on real public 1h OHLC.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/workspace/crypto-bot/scripts")
import swing_backtest as sb  # noqa: E402

WINDOW_START = datetime(2026, 2, 15, 0, 0, tzinfo=timezone.utc)
WINDOW_END_BAR_OPEN = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
# Extra complete UTC days so SMA50 exists at the first in-window close.
WARMUP_START = datetime(2025, 12, 20, 0, 0, tzinfo=timezone.utc)
CACHE = Path("/workspace/crypto-bot/data/backtests/swing_6m_klines_20260815T102641Z.json")
DIP_BTC = Path(
    "/workspace/crypto-bot/data/backtests/"
    "swing_dip_hold_btc100_2026-02-15_2026-08-15_10000_20260815T102641Z.json"
)
DIP_FOUR = Path(
    "/workspace/crypto-bot/data/backtests/"
    "swing_dip_hold_four25_2026-02-15_2026-08-15_10000_20260815T102641Z.json"
)

sb.WINDOW_START = WINDOW_START
sb.WINDOW_END_BAR_OPEN = WINDOW_END_BAR_OPEN
sb.WARMUP_START = WARMUP_START


def simulate_short_down_days(
    h1: dict[str, list[sb.Bar]],
    daily: dict[str, list[sb.DailyBar]],
    data_source: str,
    skipped: list[str],
) -> sb.BookResult:
    """Inverse of daily_momentum, BTC-only: short after a down day, flatten after an up day.

    Directional short (not a lock). Same 12 bps each side. 1x, no leverage.
    Cash-secured: entry deducts fee only; equity marks qty*(entry-px).
    """
    strategy = "short_down_days"
    book = "btc100"
    symbols = [s for s in ("BTCUSDT",) if s in h1]
    allocation = 1.0
    notes = [
        f"data source: {data_source}",
        "fee assumption: 10 bps taker + 1 bp half-spread each side "
        "(12 bps one-way, 24 bps round trip); OHLC fill, spread counted in fee only",
        "no lookahead: daily signal on UTC daily close, fill next day's open",
        "paper only — no live orders",
        "fixed rules, not tuned after seeing the window",
        "short_down_days is the inverse of daily_momentum: short (or stay short) "
        "when prior day close < open; flatten when prior day close >= open",
        "directional short, not a lock; same 12 bps each side; BTC-only; no leverage",
        "this tape was a down market, so a short-when-down rule has a tailwind "
        "and may be curve-fit to this window — not a lock",
    ]
    if not symbols:
        return sb._empty_result(strategy, book, allocation, data_source, skipped, notes)

    s = symbols[0]
    idx = {s: {b.open_time: i for i, b in enumerate(h1[s])}}
    series = list(daily[s])
    daily_by_date = {d.date: d for d in series}
    next_day_open: dict[str, datetime] = {}
    for i, d in enumerate(series[:-1]):
        next_day_open[d.date] = series[i + 1].open_time

    times = sorted(
        {
            b.open_time
            for b in h1[s]
            if WINDOW_START <= b.open_time <= WINDOW_END_BAR_OPEN
        }
    )
    if not times:
        raise RuntimeError("no bars inside window for short_down_days")
    last_t = times[-1]
    first_t = times[0]

    cash = sb.STARTING_BALANCE
    positions: dict[str, sb.Position] = {}
    pending: dict[str, sb.Pending] = {}
    trades: list[sb.Trade] = []
    fees_paid = 0.0
    equity_curve: list[tuple[datetime, float]] = []
    daily_equity: dict[str, list[tuple[datetime, float]]] = {}
    daily_trade_count: dict[str, int] = {}
    daily_fees: dict[str, float] = {}

    def last_px(sym: str, t: datetime, prefer: str) -> float | None:
        i = idx[sym].get(t)
        if i is not None:
            bar = h1[sym][i]
            return bar.open if prefer == "open" else bar.close
        for b in reversed(h1[sym]):
            if b.open_time <= t:
                return b.close
        return None

    def equity_at(t: datetime, prefer: str = "close") -> float:
        eq = cash
        for sym, pos in positions.items():
            px = last_px(sym, t, prefer)
            if px is None:
                px = pos.entry_px
            # short mark: +qty * (entry - px)
            eq += pos.qty * (pos.entry_px - px)
        return eq

    def close_pos(sym: str, t: datetime, px: float, reason: str) -> None:
        nonlocal cash, fees_paid
        pos = positions.pop(sym, None)
        if pos is None:
            return
        exit_notional = pos.qty * px
        exit_fee = sb.ONE_WAY_FEE * exit_notional
        realized = pos.qty * (pos.entry_px - px)
        cash += realized - exit_fee
        fees_paid += exit_fee
        pnl = realized - pos.entry_fee - exit_fee
        trades.append(
            sb.Trade(
                symbol=sym,
                entry_time=pos.entry_time,
                entry_px=pos.entry_px,
                exit_time=t,
                exit_px=px,
                qty=pos.qty,
                notional=pos.notional,
                pnl=pnl,
                fees=pos.entry_fee + exit_fee,
                reason=reason,
            )
        )
        d = sb._day(t)
        daily_trade_count[d] = daily_trade_count.get(d, 0) + 1
        daily_fees[d] = daily_fees.get(d, 0.0) + exit_fee

    def open_short(sym: str, t: datetime, px: float, reason: str) -> None:
        nonlocal cash, fees_paid
        if sym in positions or px <= 0:
            return
        eq = equity_at(t, prefer="open")
        if eq <= 0:
            return
        notional = allocation * eq
        entry_fee = sb.ONE_WAY_FEE * notional
        if entry_fee > cash + 1e-9:
            notional = cash / sb.ONE_WAY_FEE if sb.ONE_WAY_FEE > 0 else 0.0
            entry_fee = sb.ONE_WAY_FEE * notional
        if notional <= 0:
            return
        qty = notional / px
        cash -= entry_fee
        positions[sym] = sb.Position(
            symbol=sym,
            entry_time=t,
            entry_px=px,
            qty=qty,
            notional=notional,
            entry_fee=entry_fee,
            signal_open=None,
            closes_held=0,
        )
        fees_paid += entry_fee
        d = sb._day(t)
        daily_fees[d] = daily_fees.get(d, 0.0) + entry_fee

    def is_day_close_bar(sym: str, t: datetime) -> bool:
        i = idx[sym].get(t)
        if i is None:
            return False
        bars_s = h1[sym]
        date = sb._day(bars_s[i].open_time)
        if i + 1 >= len(bars_s):
            return True
        return sb._day(bars_s[i + 1].open_time) != date

    for t in times:
        pend = pending.get(s)
        if pend is not None and pend.fill_time == t and pend.use_open:
            i = idx[s].get(t)
            if i is None:
                pending.pop(s, None)
            else:
                px = h1[s][i].open
                if pend.kind == "exit" and s in positions:
                    close_pos(s, t, px, pend.reason)
                    pending.pop(s, None)
                elif pend.kind == "entry" and s not in positions:
                    open_short(s, t, px, pend.reason)
                    pending.pop(s, None)
                else:
                    pending.pop(s, None)
        elif pend is not None and pend.fill_time < t:
            pending.pop(s, None)

        if is_day_close_bar(s, t):
            date = sb._day(t)
            dbar = daily_by_date.get(date)
            nxt = next_day_open.get(date)
            if dbar is not None and nxt is not None and WINDOW_START <= nxt <= WINDOW_END_BAR_OPEN:
                want_short = dbar.close < dbar.open
                if want_short and s not in positions:
                    pending[s] = sb.Pending(
                        "entry", s, nxt, True, "short_prior_down", None
                    )
                elif (not want_short) and s in positions:
                    pending[s] = sb.Pending(
                        "exit", s, nxt, True, "flatten_prior_up", None
                    )

        eq = equity_at(t, prefer="close")
        equity_curve.append((t, eq))
        daily_equity.setdefault(sb._day(t), []).append((t, eq))

    if s in positions:
        i = idx[s].get(last_t)
        if i is None:
            cands = [
                b
                for b in h1[s]
                if WINDOW_START <= b.open_time <= WINDOW_END_BAR_OPEN
            ]
            last_bar = cands[-1] if cands else None
        else:
            last_bar = h1[s][i]
        if last_bar is not None:
            close_pos(s, last_bar.close_time, last_bar.close, "flatten_last_close")
            notes.append(
                f"flattened open {s} at last closed bar {sb._iso(last_bar.close_time)}"
            )

    ending = cash
    days: list[sb.DayRow] = []
    dates = sorted(daily_equity)
    prev_end = sb.STARTING_BALANCE
    for d in dates:
        marks = daily_equity[d]
        start_eq = prev_end
        end_eq = marks[-1][1]
        if d == dates[-1]:
            end_eq = ending
        days.append(
            sb.DayRow(
                date=d,
                starting_equity=start_eq,
                ending_equity=end_eq,
                pnl=end_eq - start_eq,
                trades=daily_trade_count.get(d, 0),
                fees_paid=daily_fees.get(d, 0.0),
            )
        )
        prev_end = end_eq

    pnls = [d.pnl for d in days]
    win_days = sum(1 for p in pnls if p > 0)
    lose_days = sum(1 for p in pnls if p < 0)
    best = max(days, key=lambda r: r.pnl) if days else None
    worst = min(days, key=lambda r: r.pnl) if days else None
    n_wins = sum(1 for tr in trades if tr.pnl > 0)
    peak = sb.STARTING_BALANCE
    max_dd = 0.0
    max_dd_usd = 0.0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd_usd = peak - eq
        dd = dd_usd / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_usd = dd_usd
    if ending < peak:
        dd_usd = peak - ending
        dd = dd_usd / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_usd = dd_usd
    avg = statistics.mean(pnls) if pnls else 0.0
    med = statistics.median(pnls) if pnls else 0.0
    notes.append(
        f"avg_day_pnl=${avg:,.2f} on ${sb.STARTING_BALANCE:,.0f} over {len(days)} "
        "calendar days. Rules were not tweaked to force a win."
    )
    return sb.BookResult(
        strategy=strategy,
        book=book,
        start=first_t,
        end=last_t,
        starting_balance=sb.STARTING_BALANCE,
        ending_equity=ending,
        total_pnl=ending - sb.STARTING_BALANCE,
        total_return=(ending - sb.STARTING_BALANCE) / sb.STARTING_BALANCE,
        n_trades=len(trades),
        n_wins=n_wins,
        win_rate=(n_wins / len(trades)) if trades else 0.0,
        win_days=win_days,
        lose_days=lose_days,
        avg_day_pnl=avg,
        median_day_pnl=med,
        best_day={"date": best.date if best else None, "pnl": best.pnl if best else 0.0},
        worst_day={"date": worst.date if worst else None, "pnl": worst.pnl if worst else 0.0},
        max_drawdown=max_dd,
        max_drawdown_usd=max_dd_usd,
        fees_paid=fees_paid,
        days=days,
        trades=trades,
        notes=notes,
        data_source=data_source,
        symbols_used=symbols,
        symbols_skipped=list(skipped),
        allocation=allocation,
    )


def summary_row(r: sb.BookResult | dict) -> dict:
    if isinstance(r, sb.BookResult):
        return {
            "strategy": r.strategy,
            "book": r.book,
            "start": r.start.strftime("%Y-%m-%d"),
            "end": r.end.strftime("%Y-%m-%d"),
            "ending_equity": r.ending_equity,
            "pnl": r.total_pnl,
            "ret_pct": r.total_return * 100.0,
            "trades": r.n_trades,
            "win_pct": r.win_rate * 100.0,
            "maxDD_pct": r.max_drawdown * 100.0,
            "maxDD_usd": r.max_drawdown_usd,
            "fees": r.fees_paid,
        }
    start = r.get("start", "")
    end = r.get("end", "")
    if isinstance(start, str) and "T" in start:
        start = start[:10]
    if isinstance(end, str) and "T" in end:
        end = end[:10]
    return {
        "strategy": r["strategy"],
        "book": r["book"],
        "start": start,
        "end": end,
        "ending_equity": r["ending_equity"],
        "pnl": r["total_pnl"],
        "ret_pct": r["total_return"] * 100.0,
        "trades": r["n_trades"],
        "win_pct": r["win_rate"] * 100.0,
        "maxDD_pct": r["max_drawdown"] * 100.0,
        "maxDD_usd": r["max_drawdown_usd"],
        "fees": r["fees_paid"],
    }


def print_table(rows: list[dict]) -> None:
    headers = (
        "strategy",
        "book",
        "pnl",
        "ret%",
        "trades",
        "win%",
        "maxDD%",
        "maxDD$",
        "fees",
    )
    cells = []
    for r in rows:
        cells.append(
            [
                r["strategy"],
                r["book"],
                f"{r['pnl']:+,.2f}",
                f"{r['ret_pct']:+.2f}",
                str(r["trades"]),
                f"{r['win_pct']:.1f}",
                f"{r['maxDD_pct']:.2f}",
                f"{r['maxDD_usd']:,.2f}",
                f"{r['fees']:,.2f}",
            ]
        )
    widths = [len(h) for h in headers]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))

    def fmt(cols: list[str]) -> str:
        left = {0, 1}
        return "  ".join(
            c.ljust(widths[i]) if i in left else c.rjust(widths[i])
            for i, c in enumerate(cols)
        )

    print()
    print(
        "PAPER 6m screen  $10,000  no live orders  no leverage  "
        "FIXED rules  dip_hold NOT retuned"
    )
    print(fmt(list(headers)))
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print(fmt(row))
    print()


def main() -> int:
    now = datetime.now(timezone.utc)
    print(
        f"swing 6m screen  window {sb._iso(WINDOW_START)} → "
        f"{sb._iso(WINDOW_END_BAR_OPEN)}  sma warmup from {sb._iso(WARMUP_START)}  "
        f"now={sb._iso(now)}",
        flush=True,
    )
    print(
        "Honesty: paper only, no lookahead, fixed rules, this tape was a down "
        "market so shorts/cash-heavy rules have a tailwind.",
        flush=True,
    )

    if not CACHE.is_file():
        print(f"ERROR: missing kline cache {CACHE}", file=sys.stderr)
        return 2

    h1, cache_meta = sb.load_cached_1h(CACHE)
    sources = list(cache_meta.get("sources") or [])
    skipped = list(cache_meta.get("skipped") or [])
    sources.append(f"reused cache {CACHE}")
    print(f"reused {CACHE}", flush=True)

    # Fetch extra warmup so SMA20/SMA50 exist at window start. Score bars stay the cache.
    for sym in sb.SYMBOLS:
        if sym not in h1:
            skipped.append(f"{sym}: missing from cache")
            continue
        first = h1[sym][0].open_time
        if first <= WARMUP_START:
            continue
        try:
            extra, src = sb.fetch_klines(sym, "1h", WARMUP_START, first)
            extra = [b for b in extra if WARMUP_START <= b.open_time < first]
            if extra:
                h1[sym] = sb._dedupe_bars(extra + h1[sym])
                sources.append(
                    f"{sym} 1h warmup {src} n_extra={len(extra)} "
                    f"merged_first={sb._iso(h1[sym][0].open_time)} merged_n={len(h1[sym])}"
                )
                print(sources[-1], flush=True)
            else:
                skipped.append(f"{sym} warmup: empty extra")
                print(f"WARN {sym} warmup empty", flush=True)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{sym} warmup: {exc}")
            print(f"WARN {sym} warmup: {exc}", flush=True)

    for sym, bars in list(h1.items()):
        in_win = [b for b in bars if WINDOW_START <= b.open_time <= WINDOW_END_BAR_OPEN]
        if not in_win:
            skipped.append(f"{sym}: no in-window bars")
            del h1[sym]
            continue
        print(
            f"{sym} 1h n={len(bars)} first={sb._iso(bars[0].open_time)} "
            f"last={sb._iso(bars[-1].open_time)} window_n={len(in_win)}",
            flush=True,
        )

    if "BTCUSDT" not in h1:
        print("ERROR: no BTCUSDT klines; refusing to invent fills/PnL", file=sys.stderr)
        return 2

    daily = {s: sb.resample_daily(bars) for s, bars in h1.items()}
    for s, series in daily.items():
        complete = sum(1 for d in series if d.complete)
        pre = sum(1 for d in series if d.complete and d.open_time < WINDOW_START)
        print(
            f"{s} daily n={len(series)} complete={complete} "
            f"complete_before_window={pre} first={series[0].date} last={series[-1].date} "
            f"last_complete={series[-1].complete} last_hours={series[-1].n_hours}",
            flush=True,
        )
        if pre < 50:
            skipped.append(f"{s}: only {pre} complete UTC days before window (want 50 for SMA50)")
        elif pre < 20:
            skipped.append(f"{s}: only {pre} complete UTC days before window (want 20 for SMA20)")

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    data_source = sb.VISION_KLINES
    for line in sources:
        if sb.VISION_KLINES in line:
            data_source = sb.VISION_KLINES
            break
        if sb.BINANCE_KLINES in line:
            data_source = sb.BINANCE_KLINES
            break

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

    jobs: list[tuple[str, str, int | None]] = [
        ("buy_hold", "btc100", None),
        ("buy_hold", "four25", None),
        ("sma20_trend", "btc100", 20),
        ("sma20_trend", "four25", 20),
        ("sma50_trend", "btc100", 50),
        ("sma50_trend", "four25", 50),
        ("daily_momentum", "btc100", None),
        ("daily_momentum", "four25", None),
    ]

    for name, book, sma_n in jobs:
        if book == "four25" and not any(s in h1 for s in sb.SYMBOLS):
            print(f"SKIP {name} {book}: no symbols", flush=True)
            continue
        if book == "btc100" and "BTCUSDT" not in h1:
            print(f"SKIP {name} {book}: no BTCUSDT", flush=True)
            continue
        engine = {
            "buy_hold": "buy_hold",
            "sma20_trend": "sma_trend",
            "sma50_trend": "sma_trend",
            "daily_momentum": "daily_momentum",
        }[name]
        old = sb.SMA_PERIOD
        if sma_n is not None:
            sb.SMA_PERIOD = sma_n
        try:
            res = sb.simulate_book(engine, book, h1, daily, data_source, skipped)
        finally:
            sb.SMA_PERIOD = old
        res.strategy = name
        if sma_n is not None:
            res.notes = list(res.notes) + [
                f"{name} uses {sma_n}-day SMA of complete UTC daily closes; "
                "long when close > SMA, flatten when close < SMA; fill next open"
            ]
        jp, cp = sb.save_result(res, stamp)
        results.append(res)
        files.append({"strategy": name, "book": book, "json": str(jp), "csv": str(cp)})
        print(
            f"{name:16s} {book:8s} end=${res.ending_equity:,.2f} "
            f"pnl={res.total_pnl:+,.2f} trades={res.n_trades} "
            f"maxDD={res.max_drawdown*100:.2f}% -> {jp.name}",
            flush=True,
        )

    short_res = simulate_short_down_days(h1, daily, data_source, skipped)
    jp, cp = sb.save_result(short_res, stamp)
    results.append(short_res)
    files.append(
        {
            "strategy": "short_down_days",
            "book": "btc100",
            "json": str(jp),
            "csv": str(cp),
        }
    )
    print(
        f"{'short_down_days':16s} {'btc100':8s} end=${short_res.ending_equity:,.2f} "
        f"pnl={short_res.total_pnl:+,.2f} trades={short_res.n_trades} "
        f"maxDD={short_res.max_drawdown*100:.2f}% -> {jp.name}",
        flush=True,
    )

    dip_btc = json.loads(DIP_BTC.read_text())
    dip_four = json.loads(DIP_FOUR.read_text())
    rows = [summary_row(r) for r in results]
    rows.append(summary_row(dip_btc))
    rows.append(summary_row(dip_four))
    # stable report order
    order = [
        ("buy_hold", "btc100"),
        ("buy_hold", "four25"),
        ("sma20_trend", "btc100"),
        ("sma20_trend", "four25"),
        ("sma50_trend", "btc100"),
        ("sma50_trend", "four25"),
        ("daily_momentum", "btc100"),
        ("daily_momentum", "four25"),
        ("short_down_days", "btc100"),
        ("dip_hold", "btc100"),
        ("dip_hold", "four25"),
    ]
    by = {(r["strategy"], r["book"]): r for r in rows}
    ordered = [by[k] for k in order if k in by]

    print_table(ordered)
    winners = [r for r in ordered if r["pnl"] > 0]
    print("books with pnl > 0 after fees:", flush=True)
    if winners:
        for r in winners:
            print(
                f"  {r['strategy']} {r['book']}  pnl={r['pnl']:+,.2f}  "
                f"ret={r['ret_pct']:+.2f}%  maxDD={r['maxDD_pct']:.2f}% / "
                f"${r['maxDD_usd']:,.2f}",
                flush=True,
            )
    else:
        print("  (none)", flush=True)

    monthly = {f"{r.strategy}_{r.book}": sb.monthly_pnl(r) for r in results}
    extra = {
        "fetched_at": sb._iso(now),
        "horizon": "6m",
        "screen": True,
        "window_start": sb._iso(WINDOW_START),
        "window_end_bar_open": sb._iso(WINDOW_END_BAR_OPEN),
        "last_closed_hour": sb._iso(WINDOW_END_BAR_OPEN),
        "warmup_start": sb._iso(WARMUP_START),
        "data_source": data_source,
        "sources": sources,
        "skipped": skipped,
        "kline_cache_reused": str(CACHE),
        "dip_hold_reused": [str(DIP_BTC), str(DIP_FOUR)],
        "dip_hold_rerun": False,
        "fee_assumption": "10 bps taker + 1 bp half-spread each side (12 bps one-way)",
        "starting_balance": sb.STARTING_BALANCE,
        "leverage": 1.0,
        "paper_only": True,
        "live_orders": False,
        "curve_fit": False,
        "parameters_optimized_after_results": False,
        "rule_retuned": False,
        "parameter_hunt": False,
        "honesty": (
            "Paper only. No live orders. No lookahead. Fixed small rule set, "
            "not hunted across 20 combos. dip_hold numbers reused from the prior "
            "6m run (not retuned, not rerun). Signal on UTC daily close, fill next "
            "open. This tape was a down market (raw BTC/ETH/SOL/DOGE all negative), "
            "so shorts and cash-heavy trend rules have a tailwind. "
            "short_down_days is a directional short, not a lock, and may be "
            "curve-fit to this down tape."
        ),
        "raw_buy_hold_no_fees": raw,
        "monthly_pnl": monthly,
        "table": ordered,
        "profitable_books": [
            {"strategy": r["strategy"], "book": r["book"], "pnl": r["pnl"], "ret_pct": r["ret_pct"]}
            for r in winners
        ],
        "books_summary": ordered,
        "files": files,
        "books": [r.to_dict() for r in results],
        "dip_hold_books": [
            {
                "strategy": dip_btc["strategy"],
                "book": dip_btc["book"],
                "start": dip_btc["start"],
                "end": dip_btc["end"],
                "ending_equity": dip_btc["ending_equity"],
                "total_pnl": dip_btc["total_pnl"],
                "total_return": dip_btc["total_return"],
                "n_trades": dip_btc["n_trades"],
                "win_rate": dip_btc["win_rate"],
                "max_drawdown": dip_btc["max_drawdown"],
                "max_drawdown_usd": dip_btc["max_drawdown_usd"],
                "fees_paid": dip_btc["fees_paid"],
                "reused": True,
            },
            {
                "strategy": dip_four["strategy"],
                "book": dip_four["book"],
                "start": dip_four["start"],
                "end": dip_four["end"],
                "ending_equity": dip_four["ending_equity"],
                "total_pnl": dip_four["total_pnl"],
                "total_return": dip_four["total_return"],
                "n_trades": dip_four["n_trades"],
                "win_rate": dip_four["win_rate"],
                "max_drawdown": dip_four["max_drawdown"],
                "max_drawdown_usd": dip_four["max_drawdown_usd"],
                "fees_paid": dip_four["fees_paid"],
                "reused": True,
            },
        ],
    }
    cmp_path = sb.OUT_DIR / f"swing_6m_screen_{stamp}.json"
    cmp_path.write_text(json.dumps(extra, indent=2, default=str))
    print(f"comparison json: {cmp_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
