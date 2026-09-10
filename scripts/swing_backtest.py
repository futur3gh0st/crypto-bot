#!/usr/bin/env python3
"""Paper-only 30-day swing / daily-rule backtest.

No live orders. Fills only on real public OHLC bars (no invented prices).
Daily strategies: signal on daily UTC close, fill at next day's open,
except dip_hold recovery / 3-close time-stop which exit on that daily close
(the rule is "exit after 3 daily closes or if close recovers").

buy_hold is the baseline: first in-window 1h open → last in-window 1h close.

Does not import or modify stablebot poly-run / funding / depeg code.
Parameters are fixed. Do not curve-fit after seeing results.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants (fixed rules — do not curve-fit)
# ---------------------------------------------------------------------------

VISION_KLINES = "https://data-api.binance.vision/api/v3/klines"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
OKX_CANDLES = "https://www.okx.com/api/v5/market/history-candles"

USER_AGENT = "stablebot/0.1 (research paper-trading; no live orders)"

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT")
STARTING_BALANCE = 10_000.0
ONE_WAY_FEE = 0.0012  # 10 bp taker + 1 bp half-spread
SMA_PERIOD = 20
DIP_RET = -0.02
DIP_HOLD_CLOSES = 3

# Locked to the prior day-trade score window (last closed hour 2026-08-15 09:00Z)
WINDOW_START = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)
WINDOW_END_BAR_OPEN = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
WARMUP_START = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)

CACHE_PATH = Path("/workspace/crypto-bot/data/backtests/daytrade_klines_20260815T100318Z.json")

STRATEGIES = ("buy_hold", "daily_momentum", "dip_hold", "sma_trend")
BOOKS = ("btc100", "four25")

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "backtests"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


@dataclass(frozen=True)
class Bar:
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class DailyBar:
    date: str
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    n_hours: int
    complete: bool


@dataclass
class Trade:
    symbol: str
    entry_time: datetime
    entry_px: float
    exit_time: datetime
    exit_px: float
    qty: float
    notional: float
    pnl: float
    fees: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "entry_time": _iso(self.entry_time),
            "entry_px": self.entry_px,
            "exit_time": _iso(self.exit_time),
            "exit_px": self.exit_px,
            "qty": self.qty,
            "notional": self.notional,
            "pnl": self.pnl,
            "fees": self.fees,
            "reason": self.reason,
        }


@dataclass
class Position:
    symbol: str
    entry_time: datetime
    entry_px: float
    qty: float
    notional: float
    entry_fee: float
    signal_open: float | None = None
    closes_held: int = 0


@dataclass
class Pending:
    kind: str  # entry | exit
    symbol: str
    fill_time: datetime
    use_open: bool
    reason: str
    signal_open: float | None = None


@dataclass
class DayRow:
    date: str
    starting_equity: float
    ending_equity: float
    pnl: float
    trades: int
    fees_paid: float


@dataclass
class BookResult:
    strategy: str
    book: str
    start: datetime
    end: datetime
    starting_balance: float
    ending_equity: float
    total_pnl: float
    total_return: float
    n_trades: int
    n_wins: int
    win_rate: float
    win_days: int
    lose_days: int
    avg_day_pnl: float
    median_day_pnl: float
    best_day: dict[str, Any]
    worst_day: dict[str, Any]
    max_drawdown: float
    max_drawdown_usd: float
    fees_paid: float
    days: list[DayRow]
    trades: list[Trade]
    notes: list[str]
    data_source: str
    symbols_used: list[str]
    symbols_skipped: list[str]
    allocation: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "book": self.book,
            "start": _iso(self.start),
            "end": _iso(self.end),
            "starting_balance": self.starting_balance,
            "ending_equity": self.ending_equity,
            "total_pnl": self.total_pnl,
            "total_return": self.total_return,
            "n_trades": self.n_trades,
            "n_wins": self.n_wins,
            "win_rate": self.win_rate,
            "win_days": self.win_days,
            "lose_days": self.lose_days,
            "avg_day_pnl": self.avg_day_pnl,
            "median_day_pnl": self.median_day_pnl,
            "best_day": self.best_day,
            "worst_day": self.worst_day,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_usd": self.max_drawdown_usd,
            "fees_paid": self.fees_paid,
            "notes": self.notes,
            "data_source": self.data_source,
            "symbols_used": self.symbols_used,
            "symbols_skipped": self.symbols_skipped,
            "allocation": self.allocation,
            "fee_assumption": (
                "10 bps taker + 1 bp half-spread each side "
                "(12 bps one-way, 24 bps round trip); fill at OHLC, spread in fee"
            ),
            "lookahead": (
                "daily: signal on UTC daily close, fill next day's open; "
                "dip_hold recovery/time-stop exit on that daily close; "
                "buy_hold fills first in-window 1h open and last 1h close"
            ),
            "paper_only": True,
            "live_orders": False,
            "n_days": len(self.days),
            "rules_fixed": True,
            "curve_fit": False,
        }


# ---------------------------------------------------------------------------
# HTTP / klines
# ---------------------------------------------------------------------------


def _http_json(url: str, timeout: float = 25.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def _parse_binance_rows(rows: list) -> list[Bar]:
    bars: list[Bar] = []
    if not isinstance(rows, list):
        return bars
    for row in rows:
        try:
            bars.append(
                Bar(
                    open_time=_utc(int(row[0])),
                    close_time=_utc(int(row[6])),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        except (TypeError, ValueError, IndexError):
            continue
    return bars


def _parse_okx_rows(rows: list) -> list[Bar]:
    bars: list[Bar] = []
    if not isinstance(rows, list):
        return bars
    for row in rows:
        try:
            ot = _utc(int(row[0]))
            confirm = str(row[8]) if len(row) > 8 else "1"
            if confirm != "1":
                continue
            o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            bars.append(
                Bar(
                    open_time=ot,
                    close_time=ot + timedelta(hours=1) - timedelta(seconds=1),
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=float(row[5]),
                )
            )
        except (TypeError, ValueError, IndexError):
            continue
    bars.sort(key=lambda b: b.open_time)
    return bars


def _okx_inst(symbol: str) -> str:
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}-USDT"
    return symbol


def _dedupe_bars(bars: list[Bar]) -> list[Bar]:
    seen: set[datetime] = set()
    out: list[Bar] = []
    for b in sorted(bars, key=lambda x: x.open_time):
        if b.open_time in seen:
            continue
        if b.open <= 0 or b.high <= 0 or b.low <= 0 or b.close <= 0:
            continue
        seen.add(b.open_time)
        out.append(b)
    return out


def fetch_klines(
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> tuple[list[Bar], str]:
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    step_ms = 3_600_000 if interval == "1h" else 86_400_000
    errors: list[str] = []

    for base in (VISION_KLINES, BINANCE_KLINES):
        try:
            out: list[Bar] = []
            cursor = start_ms
            pages = 0
            while cursor < end_ms and pages < 40:
                params = {
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": str(cursor),
                    "endTime": str(end_ms),
                    "limit": "1000",
                }
                url = f"{base}?{urllib.parse.urlencode(params)}"
                rows = _http_json(url)
                if isinstance(rows, dict):
                    raise RuntimeError(f"{base} {symbol}: {rows}")
                chunk = _parse_binance_rows(rows)
                if not chunk:
                    break
                out.extend(chunk)
                nxt = int(chunk[-1].open_time.timestamp() * 1000) + step_ms
                if nxt <= cursor:
                    break
                cursor = nxt
                pages += 1
                if len(chunk) < 1000:
                    break
            if out:
                return _dedupe_bars(out), base
            errors.append(f"{base}: empty")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{base}: {type(exc).__name__}: {exc}")

    try:
        inst = _okx_inst(symbol)
        bar = "1H" if interval == "1h" else "1Dutc"
        out: list[Bar] = []
        after: int | None = None
        pages = 0
        while pages < 40:
            params = {"instId": inst, "bar": bar, "limit": "100"}
            if after is not None:
                params["after"] = str(after)
            url = f"{OKX_CANDLES}?{urllib.parse.urlencode(params)}"
            payload = _http_json(url)
            rows = (payload or {}).get("data") or []
            chunk = _parse_okx_rows(rows)
            if not chunk:
                break
            out.extend(chunk)
            oldest = min(int(r[0]) for r in rows)
            after = oldest
            pages += 1
            if oldest <= start_ms:
                break
        out = [b for b in _dedupe_bars(out) if start <= b.open_time <= end]
        if out:
            return out, OKX_CANDLES
        errors.append("okx: empty after filter")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"okx: {type(exc).__name__}: {exc}")

    raise RuntimeError(f"{symbol} {interval} failed: {'; '.join(errors)}")


def bars_from_cache(rows: list[dict]) -> list[Bar]:
    out: list[Bar] = []
    for row in rows:
        out.append(
            Bar(
                open_time=_parse_iso(row["open_time"]),
                close_time=_parse_iso(row["close_time"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            )
        )
    return _dedupe_bars(out)


def load_cached_1h(path: Path) -> tuple[dict[str, list[Bar]], dict[str, Any]]:
    payload = json.loads(path.read_text())
    h1 = {s: bars_from_cache(rows) for s, rows in payload["bars_1h"].items()}
    return h1, payload


def resample_daily(bars: list[Bar]) -> list[DailyBar]:
    """Resample 1h bars to UTC calendar days. Partial days kept but flagged."""
    groups: dict[str, list[Bar]] = {}
    for b in bars:
        groups.setdefault(_day(b.open_time), []).append(b)
    out: list[DailyBar] = []
    for date in sorted(groups):
        chunk = sorted(groups[date], key=lambda x: x.open_time)
        n = len(chunk)
        out.append(
            DailyBar(
                date=date,
                open_time=chunk[0].open_time,
                close_time=chunk[-1].close_time,
                open=chunk[0].open,
                high=max(x.high for x in chunk),
                low=min(x.low for x in chunk),
                close=chunk[-1].close,
                volume=sum(x.volume for x in chunk),
                n_hours=n,
                complete=n >= 24,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Book simulation
# ---------------------------------------------------------------------------


def _empty_result(
    strategy: str,
    book: str,
    allocation: float,
    data_source: str,
    skipped: list[str],
    notes: list[str],
) -> BookResult:
    return BookResult(
        strategy=strategy,
        book=book,
        start=WINDOW_START,
        end=WINDOW_END_BAR_OPEN,
        starting_balance=STARTING_BALANCE,
        ending_equity=STARTING_BALANCE,
        total_pnl=0.0,
        total_return=0.0,
        n_trades=0,
        n_wins=0,
        win_rate=0.0,
        win_days=0,
        lose_days=0,
        avg_day_pnl=0.0,
        median_day_pnl=0.0,
        best_day={"date": None, "pnl": 0.0},
        worst_day={"date": None, "pnl": 0.0},
        max_drawdown=0.0,
        max_drawdown_usd=0.0,
        fees_paid=0.0,
        days=[],
        trades=[],
        notes=notes + ["no symbols available for this book"],
        data_source=data_source,
        symbols_used=[],
        symbols_skipped=list(skipped),
        allocation=allocation,
    )


def simulate_book(
    strategy: str,
    book: str,
    h1: dict[str, list[Bar]],
    daily: dict[str, list[DailyBar]],
    data_source: str,
    skipped: list[str],
) -> BookResult:
    if book == "four25":
        symbols = [s for s in SYMBOLS if s in h1]
        allocation = 1.0 / max(1, len(symbols))
    else:
        symbols = [s for s in ("BTCUSDT",) if s in h1]
        allocation = 1.0

    notes: list[str] = [
        f"data source: {data_source}",
        "fee assumption: 10 bps taker + 1 bp half-spread each side "
        "(12 bps one-way, 24 bps round trip); OHLC fill, spread counted in fee only",
        "no lookahead: daily signal on UTC daily close, fill next day's open; "
        "dip_hold recovery and 3-close time-stop exit on that daily close; "
        "buy_hold buys first in-window 1h open and flattens last 1h close",
        "paper only — no live orders",
        "fixed rules, not tuned after seeing the window",
        "daily return for dip_hold is close-to-close (close_t / close_{t-1} - 1)",
        f"sma_trend uses {SMA_PERIOD}-day SMA of daily closes; warmup days before the window",
        "no leverage; long-only; no stacking on dip_hold",
    ]

    if not symbols:
        return _empty_result(strategy, book, allocation, data_source, skipped, notes)

    idx: dict[str, dict[datetime, int]] = {
        s: {b.open_time: i for i, b in enumerate(h1[s])} for s in symbols
    }
    daily_by_date: dict[str, dict[str, DailyBar]] = {}
    daily_list: dict[str, list[DailyBar]] = {}
    daily_i: dict[str, dict[str, int]] = {}
    sma20: dict[str, dict[str, float | None]] = {}
    next_day_open: dict[str, dict[str, datetime]] = {}

    for s in symbols:
        # SMA uses complete warmup days + in-window days (last partial day kept)
        series = list(daily[s])
        daily_list[s] = series
        daily_by_date[s] = {d.date: d for d in series}
        daily_i[s] = {d.date: i for i, d in enumerate(series)}
        closes_for_sma: list[float | None] = []
        sma_map: dict[str, float | None] = {}
        buf: list[float] = []
        for d in series:
            # skip incomplete days except they still occupy calendar slots as None
            if d.complete:
                buf.append(d.close)
                if len(buf) > SMA_PERIOD:
                    buf.pop(0)
                if len(buf) == SMA_PERIOD:
                    sma_map[d.date] = sum(buf) / SMA_PERIOD
                else:
                    sma_map[d.date] = None
            else:
                sma_map[d.date] = None
            closes_for_sma.append(d.close if d.complete else None)
        sma20[s] = sma_map
        nd: dict[str, datetime] = {}
        for i, d in enumerate(series[:-1]):
            nd[d.date] = series[i + 1].open_time
        next_day_open[s] = nd

    times = sorted(
        {
            b.open_time
            for s in symbols
            for b in h1[s]
            if WINDOW_START <= b.open_time <= WINDOW_END_BAR_OPEN
        }
    )
    if not times:
        raise RuntimeError(f"no bars inside window for {book} {strategy}")

    last_t = times[-1]
    first_t = times[0]

    cash = STARTING_BALANCE
    positions: dict[str, Position] = {}
    pending: dict[str, Pending] = {}
    trades: list[Trade] = []
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
        # walk back to last available bar at or before t
        bars_s = h1[sym]
        for b in reversed(bars_s):
            if b.open_time <= t:
                return b.close
        return None

    def equity_at(t: datetime, prefer: str = "close") -> float:
        eq = cash
        for sym, pos in positions.items():
            px = last_px(sym, t, prefer)
            if px is None:
                px = pos.entry_px
            eq += pos.qty * px
        return eq

    def close_pos(sym: str, t: datetime, px: float, reason: str) -> None:
        nonlocal cash, fees_paid
        pos = positions.pop(sym, None)
        if pos is None:
            return
        exit_notional = pos.qty * px
        exit_fee = ONE_WAY_FEE * exit_notional
        cash += exit_notional - exit_fee
        fees_paid += exit_fee
        pnl = pos.qty * (px - pos.entry_px) - pos.entry_fee - exit_fee
        trades.append(
            Trade(
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
        d = _day(t)
        daily_trade_count[d] = daily_trade_count.get(d, 0) + 1
        daily_fees[d] = daily_fees.get(d, 0.0) + exit_fee

    def open_many(entries: list[tuple[str, datetime, float, str, float | None]]) -> None:
        """Open one or more spots sized vs pre-fill equity; scale if cash-short."""
        nonlocal cash, fees_paid
        entries = [e for e in entries if e[0] not in positions]
        if not entries:
            return
        t0 = entries[0][1]
        eq = equity_at(t0, prefer="open")
        if eq <= 0:
            return
        raw = {sym: allocation * eq for sym, _, _, _, _ in entries}
        total_cost = sum(v * (1.0 + ONE_WAY_FEE) for v in raw.values())
        if total_cost <= 0:
            return
        if total_cost > cash + 1e-9:
            scale = cash / total_cost
            raw = {s: v * scale for s, v in raw.items()}
        for sym, t, px, reason, sig_open in entries:
            notional = raw[sym]
            if notional <= 0 or px <= 0:
                continue
            qty = notional / px
            entry_fee = ONE_WAY_FEE * notional
            cost = notional + entry_fee
            if cost > cash + 1e-9:
                notional = cash / (1.0 + ONE_WAY_FEE)
                if notional <= 0:
                    continue
                qty = notional / px
                entry_fee = ONE_WAY_FEE * notional
                cost = notional + entry_fee
            cash -= cost
            positions[sym] = Position(
                symbol=sym,
                entry_time=t,
                entry_px=px,
                qty=qty,
                notional=notional,
                entry_fee=entry_fee,
                signal_open=sig_open,
                closes_held=0,
            )
            fees_paid += entry_fee
            d = _day(t)
            daily_fees[d] = daily_fees.get(d, 0.0) + entry_fee

    def is_day_close_bar(sym: str, t: datetime) -> bool:
        i = idx[sym].get(t)
        if i is None:
            return False
        bars_s = h1[sym]
        bar = bars_s[i]
        date = _day(bar.open_time)
        # last 1h bar of this UTC date in the series
        if i + 1 >= len(bars_s):
            return True
        return _day(bars_s[i + 1].open_time) != date

    # --- buy_hold: one shot at first in-window open ---
    if strategy == "buy_hold":
        first_entries: list[tuple[str, datetime, float, str, float | None]] = []
        for s in symbols:
            i = idx[s].get(first_t)
            if i is None:
                continue
            first_entries.append((s, first_t, h1[s][i].open, "buy_hold_first_open", None))
        open_many(first_entries)

    for t in times:
        # 1) pending fills at this bar's open (daily strategies)
        open_batch: list[tuple[str, datetime, float, str, float | None]] = []
        for s in symbols:
            pend = pending.get(s)
            if pend is None or pend.fill_time != t or not pend.use_open:
                if pend is not None and pend.fill_time < t:
                    pending.pop(s, None)
                continue
            i = idx[s].get(t)
            if i is None:
                pending.pop(s, None)
                continue
            px = h1[s][i].open
            if pend.kind == "exit" and s in positions:
                close_pos(s, t, px, pend.reason)
                pending.pop(s, None)
            elif pend.kind == "entry" and s not in positions:
                open_batch.append((s, t, px, pend.reason, pend.signal_open))
                pending.pop(s, None)
            else:
                pending.pop(s, None)
        if open_batch:
            open_many(open_batch)

        # 2) daily-close exits (dip_hold) and new signals for next open
        for s in symbols:
            if not is_day_close_bar(s, t):
                continue
            i = idx[s].get(t)
            if i is None:
                continue
            date = _day(t)
            dbar = daily_by_date[s].get(date)
            if dbar is None:
                continue

            # dip_hold: count this daily close; maybe exit on this close
            if strategy == "dip_hold" and s in positions:
                pos = positions[s]
                pos.closes_held += 1
                recovered = pos.signal_open is not None and dbar.close > pos.signal_open
                timed = pos.closes_held >= DIP_HOLD_CLOSES
                if recovered:
                    close_pos(s, dbar.close_time, dbar.close, "dip_recover_close")
                elif timed:
                    close_pos(s, dbar.close_time, dbar.close, "dip_3close_time")

            # do not schedule fills after the last in-window bar
            nxt = next_day_open[s].get(date)
            if nxt is None or nxt < WINDOW_START or nxt > WINDOW_END_BAR_OPEN:
                continue

            if strategy == "daily_momentum":
                want_long = dbar.close > dbar.open
                if want_long and s not in positions:
                    pending[s] = Pending("entry", s, nxt, True, "momentum_prior_up", None)
                elif (not want_long) and s in positions:
                    pending[s] = Pending("exit", s, nxt, True, "momentum_prior_down", None)

            elif strategy == "sma_trend":
                sma = sma20[s].get(date)
                if sma is None:
                    continue
                if dbar.close > sma and s not in positions:
                    pending[s] = Pending("entry", s, nxt, True, "sma20_close_above", None)
                elif dbar.close < sma and s in positions:
                    pending[s] = Pending("exit", s, nxt, True, "sma20_close_below", None)

            elif strategy == "dip_hold":
                di = daily_i[s].get(date)
                if di is None or di < 1:
                    continue
                prev = daily_list[s][di - 1]
                if prev.close <= 0:
                    continue
                ret = dbar.close / prev.close - 1.0
                if ret <= DIP_RET and s not in positions and s not in pending:
                    pending[s] = Pending(
                        "entry",
                        s,
                        nxt,
                        True,
                        "dip_prior_ret_le_-2pct",
                        signal_open=dbar.open,
                    )

        eq = equity_at(t, prefer="close")
        equity_curve.append((t, eq))
        daily_equity.setdefault(_day(t), []).append((t, eq))

    # flatten leftovers on last in-window close
    for s in list(positions):
        i = idx[s].get(last_t)
        if i is None:
            cands = [b for b in h1[s] if WINDOW_START <= b.open_time <= WINDOW_END_BAR_OPEN]
            if not cands:
                continue
            last_bar = cands[-1]
        else:
            last_bar = h1[s][i]
        close_pos(s, last_bar.close_time, last_bar.close, "flatten_last_close")
        notes.append(f"flattened open {s} at last closed bar {_iso(last_bar.close_time)}")

    ending = cash
    days: list[DayRow] = []
    dates = sorted(daily_equity)
    prev_end = STARTING_BALANCE
    for d in dates:
        marks = daily_equity[d]
        start_eq = prev_end
        end_eq = marks[-1][1]
        if d == dates[-1]:
            end_eq = ending
        days.append(
            DayRow(
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
    peak = STARTING_BALANCE
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
        f"avg_day_pnl=${avg:,.2f} on ${STARTING_BALANCE:,.0f} over {len(days)} calendar days. "
        "Rules were not tweaked to force a win."
    )

    return BookResult(
        strategy=strategy,
        book=book,
        start=first_t,
        end=last_t,
        starting_balance=STARTING_BALANCE,
        ending_equity=ending,
        total_pnl=ending - STARTING_BALANCE,
        total_return=(ending - STARTING_BALANCE) / STARTING_BALANCE,
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


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def save_result(result: BookResult, stamp: str) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = (
        f"swing_{result.strategy}_{result.book}_"
        f"{result.start.date()}_{result.end.date()}_{int(result.starting_balance)}_{stamp}"
    )
    json_path = OUT_DIR / f"{tag}.json"
    csv_path = OUT_DIR / f"{tag}_daily.csv"
    payload = result.to_dict()
    payload["trades"] = [t.to_dict() for t in result.trades]
    json_path.write_text(json.dumps(payload, indent=2))
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["date", "starting_equity", "ending_equity", "pnl", "trades", "fees_paid"]
        )
        for d in result.days:
            w.writerow(
                [
                    d.date,
                    f"{d.starting_equity:.6f}",
                    f"{d.ending_equity:.6f}",
                    f"{d.pnl:.6f}",
                    d.trades,
                    f"{d.fees_paid:.6f}",
                ]
            )
    return json_path, csv_path


def print_table(results: list[BookResult], label: str = "30d") -> None:
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
        f"PAPER swing / daily-rule backtest  $10,000  {label}  "
        "no live orders  no leverage  rules NOT retuned"
    )
    print(fmt(list(headers)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))
    print()
    print("Fees: 12 bps one-way (10 taker + 1 half-spread) each side. Long-only.")
    print("Daily rules: signal on UTC daily close, fill next day's open.")
    print("buy_hold: first in-window 1h open → last in-window 1h close (baseline).")
    print("dip_hold: prior daily ret <= -2%; exit after 3 daily closes or close")
    print("above signal-day open, whichever first. No stacking.")


def monthly_pnl(result: BookResult) -> list[dict[str, Any]]:
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


def raw_buy_hold(h1: dict[str, list[Bar]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for s, bars in h1.items():
        in_win = [b for b in bars if WINDOW_START <= b.open_time <= WINDOW_END_BAR_OPEN]
        if not in_win:
            continue
        first = in_win[0]
        last = in_win[-1]
        ret = last.close / first.open - 1.0 if first.open > 0 else 0.0
        out[s] = {
            "first_open_time": _iso(first.open_time),
            "first_open": first.open,
            "last_close_time": _iso(last.close_time),
            "last_close": last.close,
            "return": ret,
            "return_pct": ret * 100.0,
            "fees": 0.0,
            "note": "raw first-open to last-close, no fees, no book",
        }
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Paper swing / daily-rule backtest")
    p.add_argument("--start", help="window start ISO UTC, e.g. 2026-02-15T00:00:00Z")
    p.add_argument("--end", help="last closed 1h bar open ISO UTC, e.g. 2026-08-15T09:00:00Z")
    p.add_argument("--warmup", help="warmup start ISO UTC (daily bars before window)")
    p.add_argument(
        "--strategy",
        action="append",
        dest="strategies",
        help="restrict to these strategies (repeatable). Default: all.",
    )
    p.add_argument(
        "--fetch",
        action="store_true",
        help="fetch 1h klines from Binance Vision for [warmup, end] instead of the 30d cache",
    )
    p.add_argument("--tag", default="swing", help="output filename prefix (default swing)")
    p.add_argument("--label", default="30d", help="table label")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global WINDOW_START, WINDOW_END_BAR_OPEN, WARMUP_START
    args = _parse_args(argv)
    if args.start:
        WINDOW_START = _parse_iso(args.start)
    if args.end:
        WINDOW_END_BAR_OPEN = _parse_iso(args.end)
    if args.warmup:
        WARMUP_START = _parse_iso(args.warmup)
    wanted = tuple(args.strategies) if args.strategies else STRATEGIES
    for s in wanted:
        if s not in STRATEGIES:
            print(f"ERROR: unknown strategy {s}; known={STRATEGIES}", file=sys.stderr)
            return 2

    now = datetime.now(timezone.utc)
    print(
        f"swing paper backtest  window {_iso(WINDOW_START)} → {_iso(WINDOW_END_BAR_OPEN)}  "
        f"warmup from {_iso(WARMUP_START)}  now={_iso(now)}  strategies={wanted}  fetch={args.fetch}",
        flush=True,
    )
    print(
        "Honesty: paper only, no live orders, no lookahead, rules not retuned.",
        flush=True,
    )

    sources: list[str] = []
    skipped: list[str] = []
    h1: dict[str, list[Bar]] = {}

    if args.fetch:
        for sym in SYMBOLS:
            try:
                bars, src = fetch_klines(sym, "1h", WARMUP_START, WINDOW_END_BAR_OPEN)
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
                    f"first={_iso(bars[0].open_time)} last={_iso(bars[-1].open_time)}"
                )
                print(sources[-1], flush=True)
            except Exception as exc:  # noqa: BLE001
                skipped.append(f"{sym}: {exc}")
                print(f"SKIP {sym}: {exc}", flush=True)
    else:
        if not CACHE_PATH.is_file():
            print(f"ERROR: missing kline cache {CACHE_PATH}", file=sys.stderr)
            return 2
        h1, cache_meta = load_cached_1h(CACHE_PATH)
        sources = list(cache_meta.get("sources") or [])
        skipped = list(cache_meta.get("skipped") or [])
        sources.append(f"reused cache {CACHE_PATH}")

    # Fetch extra 1h warmup so SMA20 has 20 complete UTC days before the window
    # (skipped when --fetch already pulled [warmup, end])
    if args.fetch:
        pass  # already have warmup bars
    for sym in SYMBOLS:
        if args.fetch:
            break
        if sym not in h1:
            skipped.append(f"{sym}: missing from cache")
            continue
        first = h1[sym][0].open_time
        if first <= WARMUP_START:
            continue
        try:
            extra, src = fetch_klines(sym, "1h", WARMUP_START, first)
            extra = [b for b in extra if b.open_time < first]
            if extra:
                h1[sym] = _dedupe_bars(extra + h1[sym])
                sources.append(
                    f"{sym} 1h warmup {src} n_extra={len(extra)} "
                    f"merged_first={_iso(h1[sym][0].open_time)} merged_n={len(h1[sym])}"
                )
                print(sources[-1], flush=True)
            else:
                skipped.append(f"{sym} warmup: empty extra")
                print(f"WARN {sym} warmup empty", flush=True)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{sym} warmup: {exc}")
            print(f"WARN {sym} warmup: {exc}", flush=True)

    # completeness check for score window
    for sym, bars in list(h1.items()):
        in_win = [b for b in bars if WINDOW_START <= b.open_time <= WINDOW_END_BAR_OPEN]
        if not in_win:
            skipped.append(f"{sym}: no in-window bars")
            del h1[sym]
            continue
        if in_win[0].open_time != WINDOW_START or in_win[-1].open_time != WINDOW_END_BAR_OPEN:
            sources.append(
                f"{sym} window bars first={_iso(in_win[0].open_time)} last={_iso(in_win[-1].open_time)} n={len(in_win)}"
            )
        print(
            f"{sym} 1h n={len(bars)} first={_iso(bars[0].open_time)} last={_iso(bars[-1].open_time)} "
            f"window_n={len(in_win)}",
            flush=True,
        )

    if "BTCUSDT" not in h1:
        print("ERROR: no BTCUSDT klines; refusing to invent fills/PnL", file=sys.stderr)
        return 2

    daily: dict[str, list[DailyBar]] = {s: resample_daily(bars) for s, bars in h1.items()}
    for s, series in daily.items():
        complete = sum(1 for d in series if d.complete)
        pre = sum(1 for d in series if d.complete and d.open_time < WINDOW_START)
        print(
            f"{s} daily n={len(series)} complete={complete} complete_before_window={pre} "
            f"first={series[0].date} last={series[-1].date} last_complete={series[-1].complete} "
            f"last_hours={series[-1].n_hours}",
            flush=True,
        )
        if pre < SMA_PERIOD and "sma_trend" in wanted:
            notes_warn = f"{s}: only {pre} complete UTC days before window (need {SMA_PERIOD} for SMA20 at first signal)"
            skipped.append(notes_warn)
            print(f"WARN {notes_warn}", flush=True)
        elif pre < 1:
            skipped.append(f"{s}: no complete UTC day before window (dip_hold needs a prior close)")
            print(f"WARN {s}: no complete UTC day before window", flush=True)

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    data_source = VISION_KLINES
    for line in sources:
        if VISION_KLINES in line:
            data_source = VISION_KLINES
            break
        if BINANCE_KLINES in line:
            data_source = BINANCE_KLINES
            break
        if OKX_CANDLES in line:
            data_source = OKX_CANDLES
            break

    # persist merged 1h used (audit) — do not overwrite the day-trade cache
    merged_cache = {
        "fetched_at": _iso(now),
        "window_start": _iso(WINDOW_START),
        "window_end_bar_open": _iso(WINDOW_END_BAR_OPEN),
        "warmup_start": _iso(WARMUP_START),
        "reused_cache": str(CACHE_PATH),
        "sources": sources,
        "skipped": skipped,
        "bars_1h": {
            s: [
                {
                    "open_time": _iso(b.open_time),
                    "close_time": _iso(b.close_time),
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
    merged_path = OUT_DIR / f"{args.tag}_klines_{stamp}.json"
    merged_path.write_text(json.dumps(merged_cache))
    print(f"wrote merged kline cache {merged_path}", flush=True)

    raw = raw_buy_hold(h1)
    print("raw buy-hold (first open → last close, no fees):", flush=True)
    for s, row in raw.items():
        print(f"  {s:10s}  {row['return_pct']:+.3f}%  {row['first_open']} → {row['last_close']}", flush=True)

    results: list[BookResult] = []
    files: list[dict[str, str]] = []
    for strat in wanted:
        for book in BOOKS:
            if book == "btc100" and "BTCUSDT" not in h1:
                print(f"SKIP {strat} {book}: no BTCUSDT", flush=True)
                continue
            res = simulate_book(strat, book, h1, daily, data_source, skipped)
            jp, cp = save_result(res, stamp)
            results.append(res)
            files.append(
                {
                    "strategy": strat,
                    "book": book,
                    "json": str(jp),
                    "csv": str(cp),
                }
            )
            print(
                f"{strat:16s} {book:8s} end=${res.ending_equity:,.2f} "
                f"pnl={res.total_pnl:+,.2f} trades={res.n_trades} "
                f"avg_day={res.avg_day_pnl:+,.2f} -> {jp.name}",
                flush=True,
            )

    profitable = [
        {"strategy": r.strategy, "book": r.book, "pnl": r.total_pnl, "ret": r.total_return}
        for r in results
        if r.total_pnl > 0
    ]
    extra = {
        "fetched_at": _iso(now),
        "window_start": _iso(WINDOW_START),
        "window_end_bar_open": _iso(WINDOW_END_BAR_OPEN),
        "last_closed_hour": _iso(WINDOW_END_BAR_OPEN),
        "data_source": data_source,
        "sources": sources,
        "skipped": skipped,
        "kline_cache_reused": None if args.fetch else str(CACHE_PATH),
        "kline_cache_merged": str(merged_path),
        "fetch": bool(args.fetch),
        "tag": args.tag,
        "strategies": list(wanted),
        "rule_retuned": False,
        "dip_ret": DIP_RET,
        "dip_hold_closes": DIP_HOLD_CLOSES,
        "fee_assumption": "10 bps taker + 1 bp half-spread each side (12 bps one-way)",
        "starting_balance": STARTING_BALANCE,
        "leverage": 1.0,
        "paper_only": True,
        "live_orders": False,
        "curve_fit": False,
        "parameters_optimized_after_results": False,
        "honesty": (
            "Paper only. No live orders. No lookahead. "
            "Rules were fixed before the run and not tuned to win. "
            "PnL is from public OHLC fills only — not invented. "
            "buy_hold is the uninformed baseline, not a clever system."
        ),
        "raw_buy_hold_no_fees": raw,
        "profitable_books": profitable,
        "any_profitable": bool(profitable),
    }
    monthly = {f"{r.strategy}_{r.book}": monthly_pnl(r) for r in results}
    extra["monthly_pnl"] = monthly
    extra["curve_fit"] = False
    cmp_path = OUT_DIR / f"{args.tag}_comparison_{stamp}.json"
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

    print_table(results, label=args.label)
    print("monthly PnL (sum of daily marked PnL):", flush=True)
    for key, rows in monthly.items():
        print(f"  {key}", flush=True)
        for row in rows:
            print(
                f"    {row['month']}  {row['pnl']:+,.2f}  ({row['n_days']} days)",
                flush=True,
            )
    print(f"comparison json: {cmp_path}")
    for f in files:
        print(f"  {f['strategy']:16s} {f['book']:8s}  {f['json']}")
        print(f"  {'':16s} {'':8s}  {f['csv']}")
    if profitable:
        print("profitable after fees (pnl > 0):")
        for p in profitable:
            print(f"  {p['strategy']} {p['book']}  pnl={p['pnl']:+,.2f}  ret={p['ret']*100:+.2f}%")
    else:
        print("NO book was profitable after fees (all pnl <= 0). Rules were not tweaked.")
    if skipped:
        print("skipped / warnings:")
        for s in skipped:
            print(f"  - {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
