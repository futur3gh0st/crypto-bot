#!/usr/bin/env python3
"""Paper-only spot_lag / btc_lag backtest (CyrilXBT / Grok-Bot-style lag proxy).

Mechanistic Polymarket Up/Down latency-arb PROXY using Binance 1m spot.
NO live orders. Does NOT replicate viral tweet P&L ($68→$218k) — that is
unverified hype.

Modes
-----
  realistic (DEFAULT): fill delay 1 bar; elapsed≤60s only; catch-up entry from
    crude_fair; min edge gate; FLAT = scratch; fees charged into equity.
  loose: legacy fixed entry_p; early≤90s OR remaining≥120s; FLAT = loss;
    fees sensitivity-only (not charged to equity).

Reuses Binance kline fetch patterns from scripts/daytrade_backtest.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from stablebot.poly.fair import crude_fair_up
except ImportError:  # pragma: no cover — allow running without install
    def crude_fair_up(spot: float, open_px: float, scale: float = 25.0) -> float:
        if open_px <= 0 or spot <= 0:
            raise ValueError("spot and open must be positive")
        ret = (spot - open_px) / open_px
        raw = 0.50 + scale * ret
        return min(0.98, max(0.02, raw))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VISION_KLINES = "https://data-api.binance.vision/api/v3/klines"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
USER_AGENT = "stablebot/0.1 (research paper-trading; spot_lag; no live orders)"

COIN_SYMBOL = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
}

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "spot_lag_bt"
POLY_EVENTS = ROOT / "data" / "poly_cache" / "events"
POLY_HISTORY = ROOT / "data" / "poly_cache" / "history"

STARTING_BALANCE = 1000.0
DEFAULT_THRESHOLD = 0.003  # 0.3%
DEFAULT_ENTRY_P = 0.55
DEFAULT_WINDOW_MIN = 5
DEFAULT_DAYS = 30
RISK_FRAC = 0.05
FIXED_CLIP = 50.0
MAX_CONCURRENT = 3

# Loose (legacy) timing
LOOSE_ENTRY_EARLY_SEC = 90
LOOSE_MIN_REMAINING_SEC = 120

# Realistic timing / gates
REALISTIC_MAX_ELAPSED_SEC = 60  # signal elapsed ≤ 60s
REALISTIC_MIN_REMAINING_AFTER_FILL = 90  # remaining after fill ≥ 90s
DEFAULT_CATCHUP = 0.70
DEFAULT_SLIP = 0.02
DEFAULT_MIN_EDGE = 0.04
FAIR_SCALE = 25.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def poly_taker_fee(p: float) -> float:
    """Official-style crypto taker: 0.07 * p * (1-p) per side. No rebate."""
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return 0.07 * p * (1.0 - p)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


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
    coin: str
    symbol: str
    window_start: datetime
    window_end: datetime
    signal_time: datetime
    fill_time: datetime
    direction: str  # UP | DOWN
    move_pct: float
    fair_up: float
    fair_side: float
    entry_p: float
    entry_source: str  # fixed | poly_mid | catchup
    shares: float
    cost: float
    resolved: str  # UP | DOWN | FLAT
    won: bool
    scratched: bool
    pnl: float
    fee: float
    pnl_after_fee: float
    equity_after: float

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("window_start", "window_end", "signal_time", "fill_time"):
            d[k] = _iso(getattr(self, k))
        return d


@dataclass
class DayRow:
    date: str
    trades: int
    wins: int
    losses: int
    scratches: int
    day_pnl: float
    day_fees: float
    equity_eod: float


# ---------------------------------------------------------------------------
# HTTP / klines
# ---------------------------------------------------------------------------


def _http_json(url: str, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def _dedupe_bars(bars: list[Bar]) -> list[Bar]:
    seen: set[datetime] = set()
    out: list[Bar] = []
    for b in sorted(bars, key=lambda x: x.open_time):
        if b.open_time in seen:
            continue
        if b.open <= 0 or b.close <= 0:
            continue
        seen.add(b.open_time)
        out.append(b)
    return out


def _interval_step_ms(interval: str) -> int:
    mapping = {
        "1m": 60_000,
        "5m": 300_000,
        "15m": 900_000,
        "1h": 3_600_000,
    }
    if interval not in mapping:
        raise ValueError(f"unsupported interval {interval}")
    return mapping[interval]


def fetch_klines(
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    max_pages: int = 160,
) -> tuple[list[Bar], str]:
    """Fetch real klines from Binance vision, then api.binance.com."""
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    step_ms = _interval_step_ms(interval)
    errors: list[str] = []

    for base in (VISION_KLINES, BINANCE_KLINES):
        try:
            out: list[Bar] = []
            cursor = start_ms
            pages = 0
            while cursor < end_ms and pages < max_pages:
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

    raise RuntimeError(f"{symbol} {interval} failed: {'; '.join(errors)}")


# ---------------------------------------------------------------------------
# Optional poly_cache mid lookup (loose mode only)
# ---------------------------------------------------------------------------


def _try_poly_mid(
    coin: str,
    window_min: int,
    window_start: datetime,
    signal_time: datetime,
    direction: str,
) -> float | None:
    """If event + history exist, return mid of favored token near signal_time."""
    start_unix = int(window_start.timestamp())
    slug = f"{coin.lower()}-updown-{window_min}m-{start_unix}"
    event_path = POLY_EVENTS / f"{slug}.json"
    if not event_path.is_file():
        return None
    try:
        event = json.loads(event_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    up_tid = down_tid = None
    if isinstance(event, dict):
        tokens = event.get("tokens") or event.get("clobTokenIds") or []
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except json.JSONDecodeError:
                tokens = []
        if isinstance(tokens, list) and len(tokens) >= 2:
            if isinstance(tokens[0], dict):
                for t in tokens:
                    out = str(t.get("outcome", "")).lower()
                    tid = t.get("token_id") or t.get("tokenId")
                    if out in ("up", "yes") and tid:
                        up_tid = str(tid)
                    elif out in ("down", "no") and tid:
                        down_tid = str(tid)
            else:
                up_tid, down_tid = str(tokens[0]), str(tokens[1])
        markets = event.get("markets") or []
        if (not up_tid or not down_tid) and markets and isinstance(markets[0], dict):
            m0 = markets[0]
            ids = m0.get("clobTokenIds") or m0.get("clob_token_ids")
            if isinstance(ids, str):
                try:
                    ids = json.loads(ids)
                except json.JSONDecodeError:
                    ids = None
            if isinstance(ids, list) and len(ids) >= 2:
                up_tid, down_tid = str(ids[0]), str(ids[1])

    tid = up_tid if direction == "UP" else down_tid
    if not tid:
        return None

    end_unix = start_unix + window_min * 60
    candidates = [
        POLY_HISTORY / f"{tid}_{start_unix}_{end_unix}.json",
        POLY_HISTORY / f"{tid}.json",
    ]
    points: list[tuple[int, float]] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict) and "history" in raw:
            raw = raw["history"]
        if not isinstance(raw, list):
            continue
        for row in raw:
            try:
                if isinstance(row, dict):
                    t = int(row.get("t") or row.get("timestamp") or 0)
                    p = float(row.get("p") or row.get("price") or 0)
                else:
                    t, p = int(row[0]), float(row[1])
                if t > 0 and 0.0 < p < 1.0:
                    points.append((t, p))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        if points:
            break
    if not points:
        return None

    sig_ts = int(signal_time.timestamp())
    eligible = [(t, p) for t, p in points if start_unix <= t <= sig_ts]
    if not eligible:
        eligible = [(t, p) for t, p in points if start_unix <= t <= end_unix]
    if not eligible:
        return None
    eligible.sort(key=lambda x: x[0])
    return eligible[-1][1]


# ---------------------------------------------------------------------------
# Strategy engine
# ---------------------------------------------------------------------------


def window_bounds(ts: datetime, window_sec: int) -> tuple[datetime, datetime]:
    unix = int(ts.timestamp())
    start = (unix // window_sec) * window_sec
    return (
        datetime.fromtimestamp(start, tz=timezone.utc),
        datetime.fromtimestamp(start + window_sec, tz=timezone.utc),
    )


def _window_open_close(
    bars: list[Bar], w_start: datetime, w_end: datetime
) -> tuple[float | None, float | None]:
    open_px = None
    close_px = None
    for b in bars:
        if b.open_time == w_start:
            open_px = b.open
        if b.open_time == w_end - timedelta(minutes=1):
            close_px = b.close
        if b.open_time >= w_end and open_px is not None and close_px is not None:
            break
    if open_px is None or close_px is None:
        in_win = [b for b in bars if w_start <= b.open_time < w_end]
        if not in_win:
            return None, None
        open_px = in_win[0].open
        close_px = in_win[-1].close
    return open_px, close_px


def run_backtest(
    bars_by_coin: dict[str, list[Bar]],
    *,
    mode: str = "realistic",
    threshold: float,
    entry_p: float = DEFAULT_ENTRY_P,
    window_min: int,
    start: datetime,
    end: datetime,
    starting_balance: float = STARTING_BALANCE,
    risk_frac: float = RISK_FRAC,
    fixed_clip: float = FIXED_CLIP,
    max_concurrent: int = MAX_CONCURRENT,
    use_poly_mid: bool = True,
    catchup: float = DEFAULT_CATCHUP,
    slip: float = DEFAULT_SLIP,
    min_edge: float = DEFAULT_MIN_EDGE,
) -> tuple[list[Trade], list[DayRow], dict[str, Any]]:
    mode = mode.lower().strip()
    if mode not in ("realistic", "loose"):
        raise ValueError(f"unknown mode {mode}")

    window_sec = window_min * 60
    cash = starting_balance
    equity_peak = starting_balance
    max_dd = 0.0
    max_dd_usd = 0.0
    trades: list[Trade] = []
    charge_fees = mode == "realistic"

    indexed: dict[str, list[Bar]] = {}
    for coin, bars in bars_by_coin.items():
        indexed[coin] = [b for b in bars if b.open_time < end]

    events: list[tuple[datetime, str, int]] = []
    for coin, bars in indexed.items():
        for i in range(1, len(bars)):
            st = bars[i].close_time
            if st < start or st >= end:
                continue
            events.append((st, coin, i))
    events.sort(key=lambda x: (x[0], x[1]))

    traded_windows: set[tuple[str, int]] = set()
    open_pos: dict[tuple[str, int], dict[str, Any]] = {}

    def mark_to_market(_now: datetime) -> float:
        locked = sum(p["cost"] for p in open_pos.values())
        return cash + locked

    def resolve_due(now: datetime) -> None:
        nonlocal cash, equity_peak, max_dd, max_dd_usd
        done = [k for k, p in open_pos.items() if p["window_end"] <= now]
        for key in sorted(done, key=lambda k: open_pos[k]["window_end"]):
            p = open_pos.pop(key)
            coin = p["coin"]
            bars = indexed[coin]
            w_start: datetime = p["window_start"]
            w_end: datetime = p["window_end"]
            open_px, close_px = _window_open_close(bars, w_start, w_end)
            if open_px is None or close_px is None:
                cash += p["cost"]
                continue

            if close_px > open_px:
                resolved = "UP"
            elif close_px < open_px:
                resolved = "DOWN"
            else:
                resolved = "FLAT"

            shares = p["shares"]
            ep = p["entry_p"]
            fee = shares * poly_taker_fee(ep)
            scratched = False

            if resolved == "FLAT":
                if mode == "realistic":
                    # Scratch: refund cost, do not count win/loss; still pay fee
                    scratched = True
                    won = False
                    pnl = 0.0
                    cash += p["cost"]
                    if charge_fees:
                        cash -= fee
                else:
                    # Loose legacy: flat = loss
                    scratched = False
                    won = False
                    pnl = shares * (-ep)
                    # cost already deducted; payout $0
                    if charge_fees:
                        cash -= fee
            else:
                won = resolved == p["direction"]
                if won:
                    pnl = shares * (1.0 - ep)
                    cash += shares * 1.0
                else:
                    pnl = shares * (-ep)
                    # payout $0; cost already deducted
                if charge_fees:
                    cash -= fee

            equity = cash + sum(x["cost"] for x in open_pos.values())
            equity_peak = max(equity_peak, equity)
            dd = (equity_peak - equity) / equity_peak if equity_peak > 0 else 0.0
            max_dd = max(max_dd, dd)
            max_dd_usd = max(max_dd_usd, equity_peak - equity)

            pnl_after = pnl - fee
            trades.append(
                Trade(
                    coin=coin,
                    symbol=p["symbol"],
                    window_start=w_start,
                    window_end=w_end,
                    signal_time=p["signal_time"],
                    fill_time=p["fill_time"],
                    direction=p["direction"],
                    move_pct=p["move_pct"],
                    fair_up=p.get("fair_up", 0.5),
                    fair_side=p.get("fair_side", 0.5),
                    entry_p=ep,
                    entry_source=p["entry_source"],
                    shares=shares,
                    cost=p["cost"],
                    resolved=resolved,
                    won=won and not scratched,
                    scratched=scratched,
                    pnl=pnl,
                    fee=fee,
                    pnl_after_fee=pnl_after,
                    equity_after=equity,
                )
            )

    for sig_time, coin, i in events:
        resolve_due(sig_time)
        bars = indexed[coin]
        prev = bars[i - 1]
        cur = bars[i]
        if prev.close <= 0:
            continue
        move = (cur.close - prev.close) / prev.close
        if abs(move) < threshold:
            continue
        direction = "UP" if move > 0 else "DOWN"

        w_start, w_end = window_bounds(cur.open_time, window_sec)
        if not (w_start <= cur.open_time < w_end):
            continue
        if w_end > end:
            continue
        if w_start < start:
            continue

        elapsed = (sig_time - w_start).total_seconds()
        remaining_at_sig = (w_end - sig_time).total_seconds()

        if mode == "realistic":
            # Stricter: only signals with elapsed ≤ 60s
            if not (0 < elapsed <= REALISTIC_MAX_ELAPSED_SEC):
                continue
            # Fill at next 1m bar close (t+1)
            if i + 1 >= len(bars):
                continue
            fill_bar = bars[i + 1]
            fill_time = fill_bar.close_time
            # Fill bar must still be inside the same window
            if not (w_start <= fill_bar.open_time < w_end):
                continue
            remaining_after_fill = (w_end - fill_time).total_seconds()
            if remaining_after_fill < REALISTIC_MIN_REMAINING_AFTER_FILL:
                continue
        else:
            # Loose legacy timing
            early_ok = 0 < elapsed <= LOOSE_ENTRY_EARLY_SEC
            remain_ok = remaining_at_sig >= LOOSE_MIN_REMAINING_SEC
            if not (early_ok or remain_ok):
                continue
            fill_time = sig_time
            fill_bar = cur

        key = (coin, int(w_start.timestamp()))
        if key in traded_windows or key in open_pos:
            continue
        if len(open_pos) >= max_concurrent:
            continue

        # Window open price for fair
        win_open_px, _ = _window_open_close(bars, w_start, w_end)
        if win_open_px is None or win_open_px <= 0:
            continue

        if mode == "realistic":
            spot_at_fill = fill_bar.close
            try:
                fair_up = crude_fair_up(spot_at_fill, win_open_px, scale=FAIR_SCALE)
            except ValueError:
                continue
            fair_side = fair_up if direction == "UP" else (1.0 - fair_up)
            ep = clamp(0.50 + catchup * (fair_side - 0.50) + slip, 0.51, 0.92)
            edge = fair_side - ep
            if edge < min_edge:
                continue
            entry_source = "catchup"
        else:
            fair_up = 0.5
            fair_side = 0.5
            ep = entry_p
            entry_source = "fixed"
            if use_poly_mid:
                mid = _try_poly_mid(coin, window_min, w_start, sig_time, direction)
                if mid is not None and 0.01 < mid < 0.99:
                    ep = mid
                    entry_source = "poly_mid"

        equity = mark_to_market(fill_time if mode == "realistic" else sig_time)
        clip = min(risk_frac * equity, fixed_clip)
        if clip < 1.0 or ep <= 0.0 or ep >= 1.0:
            continue
        shares = clip / ep
        cost = shares * ep  # == clip
        if cost > cash + 1e-9:
            continue

        cash -= cost
        traded_windows.add(key)
        open_pos[key] = {
            "coin": coin,
            "symbol": COIN_SYMBOL[coin],
            "window_start": w_start,
            "window_end": w_end,
            "signal_time": sig_time,
            "fill_time": fill_time,
            "direction": direction,
            "move_pct": move,
            "entry_p": ep,
            "entry_source": entry_source,
            "fair_up": fair_up,
            "fair_side": fair_side,
            "shares": shares,
            "cost": cost,
        }

    resolve_due(end + timedelta(seconds=1))
    for p in list(open_pos.values()):
        cash += p["cost"]
    open_pos.clear()

    # Day-by-day from trades — equity path uses pnl_after_fee when fees charged
    day_map: dict[str, dict[str, float]] = {}
    d0 = start.astimezone(timezone.utc).date()
    d1 = (end - timedelta(seconds=1)).astimezone(timezone.utc).date()
    cur_d = d0
    while cur_d <= d1:
        day_map[cur_d.isoformat()] = {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "scratches": 0,
            "day_pnl": 0.0,
            "day_fees": 0.0,
        }
        cur_d += timedelta(days=1)

    for t in trades:
        ds = _day(t.window_end - timedelta(seconds=1))
        if ds not in day_map:
            day_map[ds] = {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "scratches": 0,
                "day_pnl": 0.0,
                "day_fees": 0.0,
            }
        if t.scratched:
            day_map[ds]["scratches"] += 1
            # scratched still counted in trade list but not as win/loss
            day_map[ds]["trades"] += 1
        else:
            day_map[ds]["trades"] += 1
            if t.won:
                day_map[ds]["wins"] += 1
            else:
                day_map[ds]["losses"] += 1
        if charge_fees:
            day_map[ds]["day_pnl"] += t.pnl_after_fee
        else:
            day_map[ds]["day_pnl"] += t.pnl
        day_map[ds]["day_fees"] += t.fee

    days: list[DayRow] = []
    running = starting_balance
    for ds in sorted(day_map.keys()):
        row = day_map[ds]
        running += row["day_pnl"]
        days.append(
            DayRow(
                date=ds,
                trades=int(row["trades"]),
                wins=int(row["wins"]),
                losses=int(row["losses"]),
                scratches=int(row["scratches"]),
                day_pnl=row["day_pnl"],
                day_fees=row["day_fees"],
                equity_eod=running,
            )
        )

    peak = starting_balance
    max_dd_path = 0.0
    max_dd_usd_path = 0.0
    for d in days:
        eq = d.equity_eod
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd_path = max(max_dd_path, dd)
        max_dd_usd_path = max(max_dd_usd_path, peak - eq)

    # Count scored trades (exclude scratches from WR denom in realistic)
    scored = [t for t in trades if not t.scratched]
    n = len(scored)
    n_all = len(trades)
    wins = sum(1 for t in scored if t.won)
    n_scratch = sum(1 for t in trades if t.scratched)
    total_pnl = sum(t.pnl for t in trades)
    total_fees = sum(t.fee for t in trades)
    total_pnl_after = sum(t.pnl_after_fee for t in trades)

    final_equity = days[-1].equity_eod if days else starting_balance

    caveats = [
        "Viral tweet P&L ($68→$218k) is unverified hype; this is a mechanistic lag PROXY.",
        "No full CLOB ask history — entry is modeled (catch-up or fixed), not real asks.",
        "Latency / queue position / partial fills are NOT simulated.",
        "Resolution uses Binance spot OHLC (window close vs open), not Polymarket oracle.",
        "crude_fair_up (scale=25) is a sketch, not a vol-aware / time-to-expiry model.",
        "Results may be negative; fill rates that guarantee profit were not invented.",
        "PAPER ONLY — no live orders.",
    ]
    if mode == "realistic":
        caveats.insert(
            1,
            "Realistic mode: fill@t+1, elapsed≤60s, catch-up entry, min_edge gate, "
            "FLAT=scratch, fees charged into equity.",
        )
        caveats.append(
            "Still NOT proven CLOB lag — win rate can reflect momentum-in-window "
            "plus crude fair edge assumptions."
        )
    else:
        caveats.insert(
            1,
            "Loose mode: fixed entry_p; early≤90s OR remaining≥120s; FLAT=loss; "
            "fees sensitivity-only (NOT charged to equity).",
        )
        caveats.append(
            "High paper WR largely reflects 1m spot momentum inside the window; "
            "does NOT prove Polymarket asks stay near entry_p after the move."
        )

    summary: dict[str, Any] = {
        "strategy": "spot_lag",
        "mode": mode,
        "paper_only": True,
        "live_orders": False,
        "starting_balance": starting_balance,
        "final_equity": final_equity,
        "total_pnl": total_pnl,
        "total_pnl_after_fee": total_pnl_after,
        "total_fees": total_fees,
        "fees_charged_to_equity": charge_fees,
        "n_trades": n_all,
        "n_scored": n,
        "n_wins": wins,
        "n_losses": n - wins,
        "n_scratches": n_scratch,
        "win_rate": (wins / n) if n else 0.0,
        "max_drawdown": max(max_dd_path, max_dd),
        "max_drawdown_usd": max(max_dd_usd_path, max_dd_usd),
        "threshold": threshold,
        "entry_p_default": entry_p if mode == "loose" else None,
        "catchup": catchup if mode == "realistic" else None,
        "slip": slip if mode == "realistic" else None,
        "min_edge": min_edge if mode == "realistic" else None,
        "fair_scale": FAIR_SCALE if mode == "realistic" else None,
        "window_min": window_min,
        "coins": sorted(bars_by_coin.keys()),
        "risk_frac": risk_frac,
        "fixed_clip": fixed_clip,
        "max_concurrent": max_concurrent,
        "start": _iso(start),
        "end": _iso(end),
        "n_days": len(days),
        "entry_sources": {
            "fixed": sum(1 for t in trades if t.entry_source == "fixed"),
            "poly_mid": sum(1 for t in trades if t.entry_source == "poly_mid"),
            "catchup": sum(1 for t in trades if t.entry_source == "catchup"),
        },
        "caveats": caveats,
    }
    # Legacy alias for older report readers
    summary["total_fees_sensitivity"] = total_fees
    return trades, days, summary


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def make_tag(
    mode: str,
    *,
    days: int,
    window_min: int,
    catchup: float,
    slip: float,
    min_edge: float,
    entry_p: float,
    threshold: float,
    fixed_clip: float = FIXED_CLIP,
) -> str:
    clip_tag = f"_clip{fixed_clip:.0f}" if fixed_clip != FIXED_CLIP else ""
    if mode == "realistic":
        tag = (
            f"realistic_cu{catchup:.2f}_slip{slip:.2f}_me{min_edge:.2f}"
            f"_{window_min}m_{days}d{clip_tag}"
        )
    else:
        tag = (
            f"loose_ep{entry_p:.2f}_th{threshold:.4f}"
            f"_{window_min}m_{days}d{clip_tag}"
        )
    return tag.replace(".", "p")


def write_outputs(
    tag: str,
    trades: list[Trade],
    days: list[DayRow],
    summary: dict[str, Any],
    out_dir: Path,
) -> tuple[Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    day_csv = out_dir / f"daily_{tag}.csv"
    trades_csv = out_dir / f"trades_{tag}.csv"
    summary_json = out_dir / f"summary_{tag}.json"

    with day_csv.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "date",
                "trades",
                "wins",
                "losses",
                "scratches",
                "day_pnl",
                "day_fees",
                "equity_eod",
            ],
        )
        w.writeheader()
        for d in days:
            w.writerow(
                {
                    "date": d.date,
                    "trades": d.trades,
                    "wins": d.wins,
                    "losses": d.losses,
                    "scratches": d.scratches,
                    "day_pnl": f"{d.day_pnl:.6f}",
                    "day_fees": f"{d.day_fees:.6f}",
                    "equity_eod": f"{d.equity_eod:.6f}",
                }
            )

    with trades_csv.open("w", newline="") as f:
        if trades:
            fieldnames = list(trades[0].to_dict().keys())
        else:
            fieldnames = [
                "coin",
                "symbol",
                "window_start",
                "window_end",
                "signal_time",
                "fill_time",
                "direction",
                "move_pct",
                "fair_up",
                "fair_side",
                "entry_p",
                "entry_source",
                "shares",
                "cost",
                "resolved",
                "won",
                "scratched",
                "pnl",
                "fee",
                "pnl_after_fee",
                "equity_after",
            ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for t in trades:
            w.writerow(t.to_dict())

    summary_json.write_text(json.dumps(summary, indent=2) + "\n")
    return day_csv, trades_csv, summary_json


def print_report(
    tag: str,
    days: list[DayRow],
    summary: dict[str, Any],
    *,
    full_table: bool = True,
) -> None:
    print("=" * 72)
    print(f"spot_lag paper backtest  [{tag}]  mode={summary.get('mode')}")
    print("=" * 72)
    ep_note = summary.get("entry_p_default")
    if summary.get("mode") == "realistic":
        pricing = (
            f"catchup={summary.get('catchup')} slip={summary.get('slip')} "
            f"min_edge={summary.get('min_edge')} fair_scale={summary.get('fair_scale')}"
        )
    else:
        pricing = f"entry_p={ep_note}"
    print(
        f"window {summary['start']} → {summary['end']}  "
        f"coins={summary['coins']}  thresh={summary['threshold']:.4%}  "
        f"{pricing}  {summary['window_min']}m"
    )
    fees_note = (
        "fees CHARGED to equity"
        if summary.get("fees_charged_to_equity")
        else "fees sensitivity-only"
    )
    print(
        f"start=${summary['starting_balance']:.2f}  "
        f"final=${summary['final_equity']:.2f}  "
        f"PnL=${summary['total_pnl']:.2f}  "
        f"PnL_after_fee=${summary['total_pnl_after_fee']:.2f}  "
        f"({fees_note})"
    )
    print(
        f"trades={summary['n_trades']}  scored={summary['n_scored']}  "
        f"wins={summary['n_wins']}  losses={summary['n_losses']}  "
        f"scratches={summary['n_scratches']}  "
        f"win_rate={summary['win_rate']:.2%}  "
        f"maxDD={summary['max_drawdown']:.2%} (${summary['max_drawdown_usd']:.2f})"
    )
    es = summary["entry_sources"]
    print(
        f"entry sources: fixed={es.get('fixed', 0)}  "
        f"poly_mid={es.get('poly_mid', 0)}  catchup={es.get('catchup', 0)}"
    )
    print()
    if full_table:
        print(
            f"{'date':<12} {'tr':>4} {'W':>4} {'L':>4} {'sc':>3} "
            f"{'day_pnl':>10} {'fees':>8} {'equity_eod':>12}"
        )
        print("-" * 68)
        for d in days:
            print(
                f"{d.date:<12} {d.trades:4d} {d.wins:4d} {d.losses:4d} {d.scratches:3d} "
                f"{d.day_pnl:10.2f} {d.day_fees:8.2f} {d.equity_eod:12.2f}"
            )
        print("-" * 68)
    print("CAVEATS (printed every run):")
    for c in summary["caveats"]:
        print(f"  - {c}")
    print()


def print_summary_only(tag: str, summary: dict[str, Any]) -> None:
    print("=" * 72)
    print(f"SUMMARY [{tag}] mode={summary.get('mode')}")
    print(
        f"  trades={summary['n_trades']} scored={summary['n_scored']} "
        f"WR={summary['win_rate']:.2%}  "
        f"PnL=${summary['total_pnl']:.2f}  "
        f"PnL_af=${summary['total_pnl_after_fee']:.2f}  "
        f"final=${summary['final_equity']:.2f}  "
        f"maxDD={summary['max_drawdown']:.2%} (${summary['max_drawdown_usd']:.2f})  "
        f"scratches={summary['n_scratches']}"
    )
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument(
        "--mode",
        choices=("realistic", "loose"),
        default="realistic",
        help="realistic (default) or legacy loose",
    )
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--entry-p", type=float, default=DEFAULT_ENTRY_P, help="loose mode only")
    ap.add_argument("--catchup", type=float, default=DEFAULT_CATCHUP)
    ap.add_argument("--slip", type=float, default=DEFAULT_SLIP)
    ap.add_argument("--min-edge", type=float, default=DEFAULT_MIN_EDGE)
    ap.add_argument("--window-min", type=int, default=DEFAULT_WINDOW_MIN)
    ap.add_argument("--coins", type=str, default="btc,eth,sol")
    ap.add_argument("--balance", type=float, default=STARTING_BALANCE)
    ap.add_argument(
        "--fixed-clip",
        type=float,
        default=FIXED_CLIP,
        help="per-trade dollar clip cap (default 50; sizing is min(risk_frac*equity, fixed_clip))",
    )
    ap.add_argument(
        "--sensitivity",
        action="store_true",
        help="loose: also entry_p 0.50/0.60; realistic: also catchup 0.50/0.85",
    )
    ap.add_argument("--no-poly-mid", action="store_true")
    ap.add_argument("--out-dir", type=str, default=str(OUT_DIR))
    ap.add_argument(
        "--summary-only",
        action="store_true",
        help="print summary metrics only (no day table)",
    )
    args = ap.parse_args(argv)

    coins = [c.strip().lower() for c in args.coins.split(",") if c.strip()]
    for c in coins:
        if c not in COIN_SYMBOL:
            print(f"ERROR: unknown coin {c}", file=sys.stderr)
            return 2

    now = datetime.now(tz=timezone.utc)
    end = now.replace(second=0, microsecond=0)
    start = end - timedelta(days=args.days)

    print(f"Fetching {args.days}d of 1m klines for {coins} …")
    print(f"mode={args.mode}  PAPER ONLY — no live orders")
    bars_by_coin: dict[str, list[Bar]] = {}
    sources: dict[str, str] = {}
    fetch_from = start - timedelta(minutes=2)
    for coin in coins:
        sym = COIN_SYMBOL[coin]
        try:
            bars, src = fetch_klines(sym, "1m", fetch_from, end)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR fetching {sym}: {exc}", file=sys.stderr)
            return 1
        bars = [b for b in bars if b.close_time < now]
        bars_by_coin[coin] = bars
        sources[coin] = src
        print(f"  {sym}: {len(bars)} bars from {src}")

    if not any(bars_by_coin.values()):
        print("ERROR: no klines fetched; refusing to invent fills/PnL", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    all_summaries: dict[str, Any] = {
        "fetched_at": _iso(now),
        "data_sources": sources,
        "mode": args.mode,
        "runs": {},
    }

    # Build run configs
    runs: list[dict[str, Any]] = []
    if args.mode == "realistic":
        catchups = [args.catchup]
        if args.sensitivity:
            for c in (0.50, 0.85):
                if abs(c - args.catchup) > 1e-9:
                    catchups.append(c)
        for cu in catchups:
            runs.append(
                {
                    "catchup": cu,
                    "slip": args.slip,
                    "min_edge": args.min_edge,
                    "entry_p": args.entry_p,
                    "full_table": (cu == args.catchup) and not args.summary_only,
                }
            )
    else:
        eps = [args.entry_p]
        if args.sensitivity:
            for p in (0.50, 0.60):
                if abs(p - args.entry_p) > 1e-9:
                    eps.append(p)
        for ep in eps:
            runs.append(
                {
                    "catchup": args.catchup,
                    "slip": args.slip,
                    "min_edge": args.min_edge,
                    "entry_p": ep,
                    "full_table": (ep == args.entry_p) and not args.summary_only,
                }
            )

    for cfg in runs:
        tag = make_tag(
            args.mode,
            days=args.days,
            window_min=args.window_min,
            catchup=cfg["catchup"],
            slip=cfg["slip"],
            min_edge=cfg["min_edge"],
            entry_p=cfg["entry_p"],
            threshold=args.threshold,
            fixed_clip=args.fixed_clip,
        )
        trades, days, summary = run_backtest(
            bars_by_coin,
            mode=args.mode,
            threshold=args.threshold,
            entry_p=cfg["entry_p"],
            window_min=args.window_min,
            start=start,
            end=end,
            starting_balance=args.balance,
            use_poly_mid=not args.no_poly_mid,
            catchup=cfg["catchup"],
            slip=cfg["slip"],
            min_edge=cfg["min_edge"],
            fixed_clip=args.fixed_clip,
        )
        summary["data_sources"] = sources
        day_csv, trades_csv, summary_json = write_outputs(
            tag, trades, days, summary, out_dir
        )
        if cfg["full_table"]:
            print_report(tag, days, summary, full_table=True)
        else:
            print_summary_only(tag, summary)
        print(f"wrote {day_csv}")
        print(f"wrote {trades_csv}")
        print(f"wrote {summary_json}")
        print()
        all_summaries["runs"][tag] = {
            "summary_path": str(summary_json),
            "daily_path": str(day_csv),
            "trades_path": str(trades_csv),
            "metrics": {
                "mode": args.mode,
                "catchup": cfg["catchup"] if args.mode == "realistic" else None,
                "slip": cfg["slip"] if args.mode == "realistic" else None,
                "min_edge": cfg["min_edge"] if args.mode == "realistic" else None,
                "entry_p": cfg["entry_p"] if args.mode == "loose" else None,
                "n_trades": summary["n_trades"],
                "n_scored": summary["n_scored"],
                "n_scratches": summary["n_scratches"],
                "win_rate": summary["win_rate"],
                "total_pnl": summary["total_pnl"],
                "total_pnl_after_fee": summary["total_pnl_after_fee"],
                "max_drawdown": summary["max_drawdown"],
                "final_equity": summary["final_equity"],
            },
        }

    combo_name = f"summary_all_{args.mode}_{args.days}d.json"
    combo_path = out_dir / combo_name
    combo_path.write_text(json.dumps(all_summaries, indent=2) + "\n")
    print(f"wrote {combo_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
