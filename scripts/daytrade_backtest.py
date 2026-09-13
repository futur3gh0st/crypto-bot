#!/usr/bin/env python3
"""Paper-only 30-day directional day-trade backtest.

No live orders. Fills only on real public OHLC bars (no invented prices).
Signal on bar close, fill at the next bar's open (no lookahead), except
fade_spike's documented 1-bar hold which exits on that hold bar's close
(or an earlier 15m close if the dump recovers).

Does not import or modify stablebot poly-run / funding / depeg code.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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
WINDOW_DAYS = 30
WARMUP_DAYS = 10

DONCHIAN_ENTRY = 20
DONCHIAN_EXIT = 10
FADE_RET = -0.015
EMA_FAST = 9
EMA_SLOW = 21

STRATEGIES = ("trend_donchian", "fade_spike", "ema_cross")
BOOKS = ("four25", "btc100", "btc3x")

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


@dataclass(frozen=True)
class Bar:
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


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
    leverage: float

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
            "leverage": self.leverage,
        }


@dataclass
class Position:
    symbol: str
    entry_time: datetime
    entry_px: float
    qty: float
    notional: float
    entry_fee: float
    leverage: float
    signal_open: float | None = None  # fade recovery level
    hold_close_time: datetime | None = None  # fade 1-bar hold


@dataclass
class Pending:
    kind: str  # "entry" | "exit"
    symbol: str
    fill_time: datetime  # expected next-bar open
    reason: str
    signal_open: float | None = None
    hold_close_time: datetime | None = None


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
    days_ge_500: int
    fees_paid: float
    days: list[DayRow]
    trades: list[Trade]
    notes: list[str]
    data_source: str
    symbols_used: list[str]
    symbols_skipped: list[str]
    leverage: float
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
            "days_ge_500": self.days_ge_500,
            "fees_paid": self.fees_paid,
            "notes": self.notes,
            "data_source": self.data_source,
            "symbols_used": self.symbols_used,
            "symbols_skipped": self.symbols_skipped,
            "leverage": self.leverage,
            "allocation": self.allocation,
            "fee_assumption": (
                "10 bps taker + 1 bp half-spread each side "
                "(12 bps one-way, 24 bps round trip); fill at OHLC, spread in fee"
            ),
            "lookahead": "signal on close, fill next bar open (fade_spike hold exits on close)",
            "paper_only": True,
            "live_orders": False,
            "n_days": len(self.days),
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
    # OKX: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
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
                    close_time=ot + timedelta(milliseconds=1),
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


def _okx_bar(interval: str) -> str:
    return {"1h": "1H", "15m": "15m"}[interval]


def fetch_klines(
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> tuple[list[Bar], str]:
    """Fetch real klines. Returns (bars, source_url). Raises if all sources fail."""
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    step_ms = 3_600_000 if interval == "1h" else 900_000
    errors: list[str] = []

    for base in (VISION_KLINES, BINANCE_KLINES):
        try:
            out: list[Bar] = []
            cursor = start_ms
            pages = 0
            while cursor < end_ms and pages < 20:
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

    # OKX public history (newest-first pages)
    try:
        inst = _okx_inst(symbol)
        bar = _okx_bar(interval)
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


def drop_incomplete(bars: list[Bar], now: datetime) -> list[Bar]:
    return [b for b in bars if b.close_time < now and b.open_time < now]


# ---------------------------------------------------------------------------
# Indicators (fixed)
# ---------------------------------------------------------------------------


def ema_series(values: list[float], span: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < span or span < 1:
        return out
    k = 2.0 / (span + 1.0)
    acc = sum(values[:span]) / span
    out[span - 1] = acc
    for i in range(span, len(values)):
        acc = values[i] * k + acc * (1.0 - k)
        out[i] = acc
    return out


def prior_high(highs: list[float], i: int, n: int) -> float | None:
    if i < n:
        return None
    return max(highs[i - n : i])


def prior_low(lows: list[float], i: int, n: int) -> float | None:
    if i < n:
        return None
    return min(lows[i - n : i])


# ---------------------------------------------------------------------------
# Per-symbol signal pass (no fills yet — just intended actions on each bar)
# ---------------------------------------------------------------------------


@dataclass
class SignalEvent:
    """Decision taken on this bar's close. Fill happens later."""

    bar_index: int
    kind: str  # entry | exit
    reason: str
    signal_open: float | None = None


def signals_donchian(bars: list[Bar], active_from: datetime | None = None) -> dict[int, list[SignalEvent]]:
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    events: dict[int, list[SignalEvent]] = {}
    long = False
    for i in range(len(bars)):
        if active_from is not None and bars[i].open_time < active_from:
            continue
        ph = prior_high(highs, i, DONCHIAN_ENTRY)
        pl = prior_low(lows, i, DONCHIAN_EXIT)
        if (not long) and ph is not None and closes[i] > ph:
            events.setdefault(i, []).append(SignalEvent(i, "entry", "donchian_break_high"))
            long = True
        elif long and pl is not None and closes[i] < pl:
            events.setdefault(i, []).append(SignalEvent(i, "exit", "donchian_break_low"))
            long = False
    return events


def signals_ema(bars: list[Bar], active_from: datetime | None = None) -> dict[int, list[SignalEvent]]:
    closes = [b.close for b in bars]
    e9 = ema_series(closes, EMA_FAST)
    e21 = ema_series(closes, EMA_SLOW)
    events: dict[int, list[SignalEvent]] = {}
    long = False
    for i in range(1, len(bars)):
        if active_from is not None and bars[i].open_time < active_from:
            continue
        a0, b0 = e9[i - 1], e21[i - 1]
        a1, b1 = e9[i], e21[i]
        if None in (a0, b0, a1, b1):
            continue
        cross_up = a0 <= b0 and a1 > b1
        cross_dn = a0 >= b0 and a1 < b1
        if (not long) and cross_up:
            events.setdefault(i, []).append(SignalEvent(i, "entry", "ema9_cross_above_ema21"))
            long = True
        elif long and cross_dn:
            events.setdefault(i, []).append(SignalEvent(i, "exit", "ema9_cross_below_ema21"))
            long = False
    return events


def signals_fade(bars: list[Bar], active_from: datetime | None = None) -> dict[int, list[SignalEvent]]:
    events: dict[int, list[SignalEvent]] = {}
    for i in range(1, len(bars)):
        if active_from is not None and bars[i].open_time < active_from:
            continue
        prev = bars[i - 1].close
        if prev <= 0:
            continue
        ret = bars[i].close / prev - 1.0
        if ret <= FADE_RET:
            events.setdefault(i, []).append(
                SignalEvent(i, "entry", "fade_spike_-1.5pct", signal_open=bars[i].open)
            )
    return events


SIGNAL_FN = {
    "trend_donchian": signals_donchian,
    "fade_spike": signals_fade,
    "ema_cross": signals_ema,
}


# ---------------------------------------------------------------------------
# Book simulation
# ---------------------------------------------------------------------------


def _interval(bars: list[Bar]) -> timedelta:
    if len(bars) >= 2:
        return bars[1].open_time - bars[0].open_time
    return timedelta(hours=1)


def _index_by_time(bars: list[Bar]) -> dict[datetime, int]:
    return {b.open_time: i for i, b in enumerate(bars)}


def _m15_in_hour(m15: list[Bar], hour_open: datetime) -> list[Bar]:
    end = hour_open + timedelta(hours=1)
    return [b for b in m15 if hour_open <= b.open_time < end]


def simulate_book(
    strategy: str,
    book: str,
    h1: dict[str, list[Bar]],
    m15: dict[str, list[Bar]],
    window_start: datetime,
    window_end: datetime,
    data_source: str,
    skipped: list[str],
) -> BookResult:
    if book == "four25":
        symbols = [s for s in SYMBOLS if s in h1]
        allocation = 1.0 / max(1, len(symbols))
        leverage = 1.0
    elif book == "btc100":
        symbols = [s for s in ("BTCUSDT",) if s in h1]
        allocation = 1.0
        leverage = 1.0
    else:  # btc3x
        symbols = [s for s in ("BTCUSDT",) if s in h1]
        allocation = 1.0
        leverage = 3.0

    notes: list[str] = [
        f"data source: {data_source}",
        "fee assumption: 10 bps taker + 1 bp half-spread each side "
        "(12 bps one-way, 24 bps round trip); OHLC fill, spread counted in fee only",
        "no lookahead: signal on bar close, entry/exit fill at NEXT bar open "
        "(fade_spike 1-bar hold exits on that bar's close, or earlier 15m close on recovery)",
        "paper only — no live orders",
        "fixed rules, not curve-fit to a $500/day target",
    ]
    if book == "btc3x":
        notes.append(
            "LEVERAGED 3x BTC-only — marked, not banked. "
            "Equity marked with 3x notional PnL; liquidate if equity hits 0."
        )

    if not symbols:
        empty = BookResult(
            strategy=strategy,
            book=book,
            start=window_start,
            end=window_end,
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
            days_ge_500=0,
            fees_paid=0.0,
            days=[],
            trades=[],
            notes=notes + ["no symbols available for this book"],
            data_source=data_source,
            symbols_used=[],
            symbols_skipped=list(skipped),
            leverage=leverage,
            allocation=allocation,
        )
        return empty

    sigs: dict[str, dict[int, list[SignalEvent]]] = {}
    idx: dict[str, dict[datetime, int]] = {}
    step: dict[str, timedelta] = {}
    for s in symbols:
        # indicators use warmup bars; position state starts flat so a
        # pre-window breakout does not invent an in-window position.
        active_from = window_start - timedelta(hours=1)
        sigs[s] = SIGNAL_FN[strategy](h1[s], active_from)
        idx[s] = _index_by_time(h1[s])
        step[s] = _interval(h1[s])

    times = sorted({b.open_time for s in symbols for b in h1[s] if window_start <= b.open_time <= window_end})
    if not times:
        raise RuntimeError(f"no bars inside window for {book} {strategy}")

    cash = STARTING_BALANCE
    positions: dict[str, Position] = {}
    pending: dict[str, Pending] = {}
    trades: list[Trade] = []
    fees_paid = 0.0
    equity_curve: list[tuple[datetime, float]] = []
    daily_equity: dict[str, list[tuple[datetime, float]]] = {}
    daily_trade_count: dict[str, int] = {}
    daily_fees: dict[str, float] = {}
    liquidated = False

    def mark_px(sym: str, t: datetime) -> float | None:
        i = idx[sym].get(t)
        if i is None:
            return None
        return h1[sym][i].close

    def equity_at(t: datetime, use_low: bool = False, low_sym: str | None = None, low_px: float | None = None) -> float:
        eq = cash
        for sym, pos in positions.items():
            if use_low and sym == low_sym and low_px is not None:
                px = low_px
            else:
                px = mark_px(sym, t)
                if px is None:
                    px = pos.entry_px
            if pos.leverage > 1.0 + 1e-12:
                # cash is remaining margin; add 3x (already in qty) price PnL
                eq += pos.qty * (px - pos.entry_px)
            else:
                # spot: cash already paid the notional; mark the coins
                eq += pos.qty * px
        return eq

    def close_pos(sym: str, t: datetime, px: float, reason: str) -> None:
        nonlocal cash, fees_paid
        pos = positions.pop(sym, None)
        if pos is None:
            return
        exit_notional = pos.qty * px
        exit_fee = ONE_WAY_FEE * exit_notional
        if pos.leverage > 1.0 + 1e-12:
            # derivative-style: cash holds remaining margin; add price PnL, pay exit fee
            cash += pos.qty * (px - pos.entry_px) - exit_fee
        else:
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
                leverage=pos.leverage,
            )
        )
        d = _day(t)
        daily_trade_count[d] = daily_trade_count.get(d, 0) + 1
        daily_fees[d] = daily_fees.get(d, 0.0) + exit_fee

    def open_pos(sym: str, t: datetime, px: float, pend: Pending) -> None:
        nonlocal cash, fees_paid, liquidated
        if liquidated or px <= 0:
            return
        eq = equity_at(t)
        if eq <= 0:
            return
        notional = allocation * eq * leverage
        if notional <= 0:
            return
        qty = notional / px
        entry_fee = ONE_WAY_FEE * notional
        if leverage > 1.0 + 1e-12:
            if cash < entry_fee:
                return
            cash -= entry_fee
        else:
            cost = notional + entry_fee
            if cost > cash + 1e-9:
                # shrink to cash (should be rare)
                notional = cash / (1.0 + ONE_WAY_FEE)
                if notional <= 0:
                    return
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
            leverage=leverage,
            signal_open=pend.signal_open,
            hold_close_time=pend.hold_close_time,
        )
        fees_paid += entry_fee
        d = _day(t)
        daily_fees[d] = daily_fees.get(d, 0.0) + entry_fee

    def liq_price(pos: Position) -> float | None:
        if pos.leverage <= 1.0 + 1e-12:
            return None
        # equity = cash + qty*(px-entry); cash already had entry fee deducted
        # zero when cash + qty*(px-entry) = 0 → px = entry - cash/qty
        if pos.qty <= 0:
            return None
        # cash here includes other symbols; for btc3x there is only one
        return pos.entry_px - cash / pos.qty

    for t in times:
        if liquidated:
            eq = max(0.0, cash)
            equity_curve.append((t, eq))
            daily_equity.setdefault(_day(t), []).append((t, eq))
            continue

        # 1) fills at open
        for sym in symbols:
            bar_i = idx[sym].get(t)
            if bar_i is None:
                continue
            bar = h1[sym][bar_i]
            pend = pending.get(sym)
            if pend is None:
                continue
            if pend.fill_time != t:
                # expected next bar was missing — skip this pending (do not invent)
                if pend.fill_time < t:
                    pending.pop(sym, None)
                continue
            if pend.kind == "exit" and sym in positions:
                close_pos(sym, t, bar.open, pend.reason)
            elif pend.kind == "entry" and sym not in positions:
                open_pos(sym, t, bar.open, pend)
            pending.pop(sym, None)

        # 2) 3x liquidation on bar low (longs)
        for sym in list(positions):
            bar_i = idx[sym].get(t)
            if bar_i is None:
                continue
            bar = h1[sym][bar_i]
            pos = positions[sym]
            lp = liq_price(pos)
            if lp is not None and bar.low <= lp:
                # liquidate at the zero-equity price (real low traded through it)
                close_pos(sym, t, lp, "liquidated_equity_0")
                if cash < 0:
                    cash = 0.0
                if equity_at(t) <= 0:
                    cash = 0.0
                    liquidated = True
                    notes.append(
                        f"liquidated {sym} at {_iso(t)} px={lp:.6f} "
                        f"(bar low {bar.low:.6f} traded through zero-equity price)"
                    )

        if liquidated:
            eq = 0.0
            equity_curve.append((t, eq))
            daily_equity.setdefault(_day(t), []).append((t, eq))
            continue

        # 3) fade_spike intra-hour / same-bar close exit
        if strategy == "fade_spike":
            for sym in list(positions):
                bar_i = idx[sym].get(t)
                if bar_i is None:
                    continue
                bar = h1[sym][bar_i]
                pos = positions[sym]
                recovered = False
                rec_px = None
                rec_t = None
                if pos.signal_open is not None and sym in m15:
                    for mb in _m15_in_hour(m15[sym], bar.open_time):
                        if mb.open_time < pos.entry_time:
                            continue
                        if mb.close > pos.signal_open:
                            recovered = True
                            rec_px = mb.close
                            rec_t = mb.open_time
                            break
                if recovered and rec_px is not None and rec_t is not None:
                    close_pos(sym, rec_t, rec_px, "fade_recover_15m_close")
                elif pos.hold_close_time is not None and bar.close_time >= pos.hold_close_time:
                    close_pos(sym, bar.close_time, bar.close, "fade_1bar_hold_close")

        # 4) close-based signals → pending next-bar fills (skip if next bar missing later)
        for sym in symbols:
            bar_i = idx[sym].get(t)
            if bar_i is None:
                continue
            evs = sigs[sym].get(bar_i) or []
            bars_s = h1[sym]
            next_i = bar_i + 1
            if next_i >= len(bars_s):
                continue
            nxt = bars_s[next_i]
            expected = t + step[sym]
            # skip if the next bar is not the immediate next interval
            if nxt.open_time != expected:
                continue
            if nxt.open_time < window_start or nxt.open_time > window_end:
                # allow fill on first window bar from a pre-window signal;
                # but do not open after window_end
                if nxt.open_time > window_end:
                    continue
            for ev in evs:
                if ev.kind == "exit" and sym in positions:
                    pending[sym] = Pending("exit", sym, nxt.open_time, ev.reason)
                elif ev.kind == "entry" and sym not in positions and ev.bar_index == bar_i:
                    # only act on signals whose close is at/after window start
                    # (warmup bars may generate a state; we still allow a pre-window
                    # cross to fill at the first in-window open if next bar is in window)
                    hold_ct = None
                    if strategy == "fade_spike":
                        hold_ct = nxt.close_time
                    if nxt.open_time >= window_start:
                        pending[sym] = Pending(
                            "entry",
                            sym,
                            nxt.open_time,
                            ev.reason,
                            signal_open=ev.signal_open,
                            hold_close_time=hold_ct,
                        )

        eq = equity_at(t)
        equity_curve.append((t, eq))
        daily_equity.setdefault(_day(t), []).append((t, eq))

    # flatten leftovers on last in-window close (real bar, plus exit fee)
    last_t = times[-1]
    for sym in list(positions):
        bar_i = idx[sym].get(last_t)
        if bar_i is None:
            # find last available bar for this symbol in window
            cands = [b for b in h1[sym] if window_start <= b.open_time <= window_end]
            if not cands:
                continue
            last_bar = cands[-1]
        else:
            last_bar = h1[sym][bar_i]
        close_pos(sym, last_bar.close_time, last_bar.close, "flatten_last_close")
        notes.append(f"flattened open {sym} at last closed bar {_iso(last_bar.close_time)}")

    ending = max(0.0, cash) if liquidated else cash
    # rebuild daily rows from equity curve
    days: list[DayRow] = []
    dates = sorted(daily_equity)
    prev_end = STARTING_BALANCE
    for d in dates:
        marks = daily_equity[d]
        start_eq = prev_end
        end_eq = marks[-1][1]
        # if we flattened after last mark, ending cash is the true end
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
    # include flatten in last equity for DD
    if ending < peak:
        dd_usd = peak - ending
        dd = dd_usd / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_usd = dd_usd

    avg = statistics.mean(pnls) if pnls else 0.0
    med = statistics.median(pnls) if pnls else 0.0
    days_ge = sum(1 for p in pnls if p >= 500.0)

    if abs(avg) < 250:
        notes.append(
            f"NOT close to $500/day: avg_day_pnl=${avg:,.2f} on ${STARTING_BALANCE:,.0f} "
            f"over {len(days)} calendar days. Rules were not tweaked to force a win."
        )
    elif avg < 500:
        notes.append(
            f"avg_day_pnl=${avg:,.2f} is below the $500/day target. Rules were not tweaked."
        )

    if book == "btc3x" and days_ge:
        w = worst.pnl if worst else 0.0
        wd = worst.date if worst else "?"
        notes.append(
            f"3x had {days_ge} calendar day(s) with pnl>=$500; "
            f"same breath: worst_day={wd} pnl=${w:,.2f}, "
            f"max_drawdown={max_dd*100:.2f}% (${max_dd_usd:,.2f}). "
            f"Leveraged mark — not banked."
        )

    return BookResult(
        strategy=strategy,
        book=book,
        start=times[0],
        end=times[-1],
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
        days_ge_500=days_ge,
        fees_paid=fees_paid,
        days=days,
        trades=trades,
        notes=notes,
        data_source=data_source,
        symbols_used=symbols,
        symbols_skipped=list(skipped),
        leverage=leverage,
        allocation=allocation,
    )


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def save_result(result: BookResult, stamp: str) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = (
        f"daytrade_{result.strategy}_{result.book}_"
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
            [
                "date",
                "starting_equity",
                "ending_equity",
                "pnl",
                "trades",
                "fees_paid",
            ]
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


def print_table(results: list[BookResult]) -> None:
    headers = (
        "strategy",
        "book",
        "end_eq",
        "pnl",
        "ret%",
        "trades",
        "win%",
        "avg_day",
        "med_day",
        "best",
        "worst",
        "maxDD%",
        "maxDD$",
        "d>=500",
        "fees",
    )
    rows: list[list[str]] = []
    for r in results:
        rows.append(
            [
                r.strategy,
                r.book + ("*" if r.book == "btc3x" else ""),
                f"{r.ending_equity:,.2f}",
                f"{r.total_pnl:+,.2f}",
                f"{r.total_return*100:+.2f}",
                str(r.n_trades),
                f"{r.win_rate*100:.1f}",
                f"{r.avg_day_pnl:+,.2f}",
                f"{r.median_day_pnl:+,.2f}",
                f"{r.best_day['pnl']:+,.2f}",
                f"{r.worst_day['pnl']:+,.2f}",
                f"{r.max_drawdown*100:.2f}",
                f"{r.max_drawdown_usd:,.2f}",
                str(r.days_ge_500),
                f"{r.fees_paid:,.2f}",
            ]
        )
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    def fmt(cols: list[str]) -> str:
        return "  ".join(c.ljust(widths[i]) if i < 2 else c.rjust(widths[i]) for i, c in enumerate(cols))
    print()
    print("PAPER day-trade backtest  $10,000  30d  no live orders  *btc3x=3x leveraged, not banked")
    print(fmt(list(headers)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))
    print()
    print("Fees: 12 bps one-way (10 taker + 1 half-spread) each side. Long-only. Next-bar-open fills.")
    print("Target $500/day is 5%/day on $10k — unlevered directional books are not expected to hit it.")


def save_comparison(results: list[BookResult], paths: list[dict[str, str]], stamp: str, extra: dict) -> Path:
    out = OUT_DIR / f"daytrade_comparison_{stamp}.json"
    out.write_text(
        json.dumps(
            {
                **extra,
                "paper_only": True,
                "live_orders": False,
                "starting_balance": STARTING_BALANCE,
                "books": [r.to_dict() for r in results],
                "files": paths,
            },
            indent=2,
        )
    )
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    now = datetime.now(timezone.utc)
    # last closed hour
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=WINDOW_DAYS)
    fetch_from = start - timedelta(days=WARMUP_DAYS)

    print(
        f"daytrade paper backtest  window { _iso(start) } → { _iso(end) }  "
        f"warmup from { _iso(fetch_from) }  now={ _iso(now) }",
        flush=True,
    )

    h1: dict[str, list[Bar]] = {}
    m15: dict[str, list[Bar]] = {}
    skipped: list[str] = []
    sources: list[str] = []

    for sym in SYMBOLS:
        try:
            bars, src = fetch_klines(sym, "1h", fetch_from, end)
            bars = drop_incomplete(bars, now)
            if len(bars) < EMA_SLOW + 5:
                skipped.append(f"{sym} 1h: only {len(bars)} bars")
                print(f"SKIP {sym} 1h: only {len(bars)} bars", flush=True)
                continue
            h1[sym] = bars
            sources.append(f"{sym} 1h {src} n={len(bars)} first={_iso(bars[0].open_time)} last={_iso(bars[-1].open_time)}")
            print(sources[-1], flush=True)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{sym} 1h: {exc}")
            print(f"SKIP {sym} 1h: {exc}", flush=True)

    for sym in list(h1):
        try:
            bars, src = fetch_klines(sym, "15m", start - timedelta(days=1), end)
            bars = drop_incomplete(bars, now)
            m15[sym] = bars
            sources.append(f"{sym} 15m {src} n={len(bars)}")
            print(sources[-1], flush=True)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{sym} 15m: {exc} (fade early-exit disabled for this symbol)")
            print(f"WARN {sym} 15m: {exc} — fade uses 1-bar hold only", flush=True)

    if "BTCUSDT" not in h1 and not h1:
        print("ERROR: no klines fetched; refusing to invent fills/PnL", file=sys.stderr)
        return 2

    # persist the exact bars used (audit)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    cache = {
        "fetched_at": _iso(now),
        "window_start": _iso(start),
        "window_end": _iso(end),
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
    cache_path = OUT_DIR / f"daytrade_klines_{stamp}.json"
    cache_path.write_text(json.dumps(cache))
    print(f"wrote kline cache {cache_path}", flush=True)

    data_source = VISION_KLINES
    if sources:
        # prefer the first successful 1h URL
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

    results: list[BookResult] = []
    files: list[dict[str, str]] = []
    for strat in STRATEGIES:
        for book in BOOKS:
            if book.startswith("btc") and "BTCUSDT" not in h1:
                print(f"SKIP {strat} {book}: no BTCUSDT", flush=True)
                continue
            res = simulate_book(strat, book, h1, m15, start, end, data_source, skipped)
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
                f"avg_day={res.avg_day_pnl:+,.2f} days>=500={res.days_ge_500} "
                f"-> {jp.name}",
                flush=True,
            )

    extra = {
        "fetched_at": _iso(now),
        "window_start": _iso(start),
        "window_end": _iso(end),
        "data_source": data_source,
        "sources": sources,
        "skipped": skipped,
        "kline_cache": str(cache_path),
        "fee_assumption": "10 bps taker + 1 bp half-spread each side (12 bps one-way)",
        "honesty": (
            "If avg_day_pnl is far below $500, that is the result. "
            "Parameters were not optimized after seeing the data."
        ),
    }
    cmp_path = save_comparison(results, files, stamp, extra)
    print_table(results)
    print(f"comparison json: {cmp_path}")
    for f in files:
        print(f"  {f['strategy']:16s} {f['book']:8s}  {f['json']}")
        print(f"  {'':16s} {'':8s}  {f['csv']}")
    if skipped:
        print("skipped:")
        for s in skipped:
            print(f"  - {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
