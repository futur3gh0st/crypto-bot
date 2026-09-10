#!/usr/bin/env python3
"""Honest multi-strategy Polymarket/crypto paper sleeve hunt.

Screens pair_lock / spot_lag_realistic / dan_desk / dip_arb / spot_momentum_poly
on the SAME 30d then 90d windows. Fees charged into equity. No invented fills.
PAPER ONLY — no live orders, no private keys.

Ideas (not code) from MrFadiAi/Polymarket-bot (classic arb, DipArb, risk caps)
and Dan1ro0 desk pipeline (SPOTTER→PRIOR→EDGE→KELLY→TAKER→CLOSER).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from spot_lag_backtest import (  # noqa: E402
    Bar,
    COIN_SYMBOL as BASE_COINS,
    DayRow,
    STARTING_BALANCE,
    Trade,
    _day,
    _iso,
    _window_open_close,
    clamp,
    crude_fair_up,
    fetch_klines,
    poly_taker_fee,
    run_backtest as run_spot_lag,
    window_bounds,
    write_outputs as write_spot_outputs,
)

from stablebot.poly.replay import (  # noqa: E402
    aligned_pairs,
    filter_window_history,
    first_lock,
    winning_side,
)

OUT_DIR = ROOT / "data" / "sleeve_hunt"
KLINE_CACHE = ROOT / "data" / "klines_cache"
POLY_EVENTS = ROOT / "data" / "poly_cache" / "events"
POLY_HISTORY = ROOT / "data" / "poly_cache" / "history"

COIN_SYMBOL = {
    **BASE_COINS,
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
}

FAIR_SCALE = 25.0
REALISTIC_MAX_ELAPSED = 60
REALISTIC_MIN_REMAIN = 90


@dataclass
class SleeveResult:
    name: str
    family: str
    window_tag: str
    start: str
    end: str
    final_equity: float
    starting_balance: float
    n_trades: int
    n_wins: int
    n_losses: int
    n_scratches: int
    win_rate: float
    max_drawdown: float
    max_drawdown_usd: float
    total_pnl: float
    total_pnl_after_fee: float
    total_fees: float
    honesty_flags: list[str]
    params: dict[str, Any]
    daily_path: str = ""
    trades_path: str = ""
    summary_path: str = ""
    hits_5x: bool = False
    fantasy: bool = False
    notes: str = ""

    def metrics(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "family": self.family,
            "window_tag": self.window_tag,
            "start": self.start,
            "end": self.end,
            "final_equity": self.final_equity,
            "starting_balance": self.starting_balance,
            "n_trades": self.n_trades,
            "n_wins": self.n_wins,
            "n_losses": self.n_losses,
            "n_scratches": self.n_scratches,
            "win_rate": self.win_rate,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_usd": self.max_drawdown_usd,
            "total_pnl": self.total_pnl,
            "total_pnl_after_fee": self.total_pnl_after_fee,
            "total_fees": self.total_fees,
            "honesty_flags": self.honesty_flags,
            "params": self.params,
            "daily_path": self.daily_path,
            "trades_path": self.trades_path,
            "summary_path": self.summary_path,
            "hits_5x": self.hits_5x,
            "fantasy": self.fantasy,
            "notes": self.notes,
        }


def days_from_trades(
    trades: list[Trade],
    start: datetime,
    end: datetime,
    starting_balance: float,
) -> tuple[list[DayRow], float, float]:
    day_map: dict[str, dict[str, float]] = {}
    d0 = start.astimezone(timezone.utc).date()
    d1 = (end - timedelta(seconds=1)).astimezone(timezone.utc).date()
    cur = d0
    while cur <= d1:
        day_map[cur.isoformat()] = {
            "trades": 0, "wins": 0, "losses": 0, "scratches": 0,
            "day_pnl": 0.0, "day_fees": 0.0,
        }
        cur += timedelta(days=1)
    for t in trades:
        ds = _day(t.window_end - timedelta(seconds=1))
        if ds not in day_map:
            day_map[ds] = {
                "trades": 0, "wins": 0, "losses": 0, "scratches": 0,
                "day_pnl": 0.0, "day_fees": 0.0,
            }
        day_map[ds]["trades"] += 1
        if t.scratched:
            day_map[ds]["scratches"] += 1
        elif t.won:
            day_map[ds]["wins"] += 1
        else:
            day_map[ds]["losses"] += 1
        day_map[ds]["day_pnl"] += t.pnl_after_fee
        day_map[ds]["day_fees"] += t.fee
    days: list[DayRow] = []
    running = starting_balance
    peak = starting_balance
    max_dd = 0.0
    max_dd_usd = 0.0
    for ds in sorted(day_map.keys()):
        row = day_map[ds]
        running += row["day_pnl"]
        peak = max(peak, running)
        dd = (peak - running) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        max_dd_usd = max(max_dd_usd, peak - running)
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
    return days, max_dd, max_dd_usd


def pack_result(
    name: str,
    family: str,
    window_tag: str,
    trades: list[Trade],
    days: list[DayRow],
    summary: dict[str, Any],
    honesty_flags: list[str],
    params: dict[str, Any],
    out_dir: Path,
    *,
    fantasy: bool = False,
    notes: str = "",
) -> SleeveResult:
    tag = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)
    day_csv, trades_csv, summary_json = write_spot_outputs(
        tag,
        trades,
        days,
        {**summary, "honesty_flags": honesty_flags, "params": params, "fantasy": fantasy},
        out_dir,
    )
    fe = float(summary["final_equity"])
    return SleeveResult(
        name=name,
        family=family,
        window_tag=window_tag,
        start=summary["start"],
        end=summary["end"],
        final_equity=fe,
        starting_balance=float(summary["starting_balance"]),
        n_trades=int(summary["n_trades"]),
        n_wins=int(summary["n_wins"]),
        n_losses=int(summary["n_losses"]),
        n_scratches=int(summary["n_scratches"]),
        win_rate=float(summary["win_rate"]),
        max_drawdown=float(summary["max_drawdown"]),
        max_drawdown_usd=float(summary["max_drawdown_usd"]),
        total_pnl=float(summary["total_pnl"]),
        total_pnl_after_fee=float(summary["total_pnl_after_fee"]),
        total_fees=float(summary["total_fees"]),
        honesty_flags=honesty_flags,
        params=params,
        daily_path=str(day_csv),
        trades_path=str(trades_csv),
        summary_path=str(summary_json),
        hits_5x=fe >= 5000.0,
        fantasy=fantasy,
        notes=notes,
    )


def load_or_fetch_klines(
    coins: list[str],
    start: datetime,
    end: datetime,
) -> tuple[dict[str, list[Bar]], dict[str, str]]:
    bars_by_coin: dict[str, list[Bar]] = {}
    sources: dict[str, str] = {}
    KLINE_CACHE.mkdir(parents=True, exist_ok=True)
    fetch_from = start - timedelta(minutes=5)
    for coin in coins:
        sym = COIN_SYMBOL[coin]
        want_lo = fetch_from
        want_hi = end
        bars: list[Bar] | None = None
        best: Path | None = None
        for p in KLINE_CACHE.glob(f"{sym}_1m_*.json"):
            best = p
            break
        # Prefer exact-ish coverage caches; scan all
        candidates = sorted(KLINE_CACHE.glob(f"{sym}_1m_*.json"))
        for p in candidates:
            try:
                raw = json.loads(p.read_text())
                rows = raw.get("bars", [])
                if len(rows) < 100:
                    continue
                t0 = datetime.fromisoformat(rows[0]["open_time"].replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(rows[-1]["close_time"].replace("Z", "+00:00"))
                if t0 <= want_lo + timedelta(hours=2) and t1 >= want_hi - timedelta(hours=2):
                    parsed = []
                    for r in rows:
                        parsed.append(
                            Bar(
                                open_time=datetime.fromisoformat(
                                    r["open_time"].replace("Z", "+00:00")
                                ),
                                close_time=datetime.fromisoformat(
                                    r["close_time"].replace("Z", "+00:00")
                                ),
                                open=float(r["open"]),
                                high=float(r["high"]),
                                low=float(r["low"]),
                                close=float(r["close"]),
                                volume=float(r["volume"]),
                            )
                        )
                    bars = [b for b in parsed if b.open_time < end]
                    sources[coin] = f"cache:{p.name}"
                    print(f"  {sym}: {len(bars)} bars from cache {p.name}")
                    break
            except Exception:
                continue
        if bars is None:
            bars, src = fetch_klines(sym, "1m", fetch_from, end)
            sources[coin] = src
            payload = {
                "symbol": sym,
                "interval": "1m",
                "source": src,
                "bars": [
                    {
                        "open_time": _iso(b.open_time),
                        "close_time": _iso(b.close_time),
                        "open": b.open,
                        "high": b.high,
                        "low": b.low,
                        "close": b.close,
                        "volume": b.volume,
                    }
                    for b in bars
                ],
            }
            out_p = KLINE_CACHE / (
                f"{sym}_1m_{fetch_from.strftime('%Y%m%d%H%M')}_"
                f"{end.strftime('%Y%m%d%H%M')}.json"
            )
            out_p.write_text(json.dumps(payload))
            print(f"  {sym}: {len(bars)} bars from {src} (cached)")
        bars_by_coin[coin] = bars
    return bars_by_coin, sources


# ---------------------------------------------------------------------------
# A. pair_lock — mid-history replay from poly_cache
# ---------------------------------------------------------------------------


def _parse_token_ids(event: dict[str, Any]) -> tuple[str | None, str | None]:
    markets = event.get("markets") or []
    if not markets or not isinstance(markets[0], dict):
        return None, None
    m0 = markets[0]
    ids = m0.get("clobTokenIds") or m0.get("clob_token_ids")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except json.JSONDecodeError:
            ids = None
    if isinstance(ids, list) and len(ids) >= 2:
        return str(ids[0]), str(ids[1])
    return None, None


def _load_hist(token_id: str, start_unix: int, end_unix: int) -> list[tuple[int, float]]:
    safe = token_id[:80]
    candidates = [
        POLY_HISTORY / f"{safe}_{start_unix}_{end_unix}.json",
        POLY_HISTORY / f"{token_id}_{start_unix}_{end_unix}.json",
        POLY_HISTORY / f"{safe}.json",
    ]
    # truncated glob fallback
    for p in POLY_HISTORY.glob(f"{token_id[:48]}*_{start_unix}_{end_unix}.json"):
        candidates.append(p)
    for path in candidates:
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        hist = raw.get("history", raw) if isinstance(raw, dict) else raw
        if not isinstance(hist, list):
            continue
        out: list[tuple[int, float]] = []
        for row in hist:
            try:
                if isinstance(row, dict):
                    t = int(row.get("t") or row.get("timestamp") or 0)
                    p = float(row.get("p") or row.get("price") or 0)
                else:
                    t, p = int(row[0]), float(row[1])
                if t > 0 and 0.0 < p < 1.0:
                    out.append((t, p))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        if out:
            return out
    return []


def run_pair_lock(
    *,
    start: datetime,
    end: datetime,
    coins: list[str],
    window_min: int,
    pair_gate: float,
    starting_balance: float,
    shares_mode: str,  # fixed20 | equity_scaled
    fixed_shares: float = 20.0,
    risk_frac: float = 0.05,
    max_concurrent: int = 5,
) -> tuple[list[Trade], list[DayRow], dict[str, Any], list[str]]:
    """Replay poly mid history. HONESTY: mid≠ask; live asks often sum≥1.01."""
    honesty = [
        "MID_NOT_ASK: prices-history last/mid ≠ live ask; live books often ask-sum≥1.01",
        "Fees charged: 0.07*p*(1-p) per side into equity",
        "PAPER ONLY",
    ]
    start_u = int(start.timestamp())
    end_u = int(end.timestamp())
    interval = window_min * 60
    min_lock = 1.0 - pair_gate  # gate 0.97 => min_lock 0.03

    # Index event files for requested coins/window
    events_meta: list[dict[str, Any]] = []
    n_events = 0
    n_with_hist = 0
    n_resolved = 0
    for coin in coins:
        pattern = f"{coin.lower()}-updown-{window_min}m-*.json"
        for path in POLY_EVENTS.glob(pattern):
            m = re.search(rf"-(\d+)\.json$", path.name)
            if not m:
                continue
            w_start = int(m.group(1))
            w_end = w_start + interval
            if w_end <= start_u or w_start >= end_u:
                continue
            if w_start < start_u or w_end > end_u:
                continue
            n_events += 1
            try:
                ev = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if ev.get("_missing"):
                continue
            up_id, down_id = _parse_token_ids(ev)
            if not up_id or not down_id:
                continue
            markets = ev.get("markets") or [{}]
            m0 = markets[0] if markets else {}
            events_meta.append(
                {
                    "slug": path.stem,
                    "coin": coin,
                    "start": w_start,
                    "end": w_end,
                    "up_id": up_id,
                    "down_id": down_id,
                    "outcomes": m0.get("outcomes"),
                    "outcome_prices": m0.get("outcomePrices"),
                }
            )

    events_meta.sort(key=lambda x: x["start"])
    if n_events == 0:
        honesty.append("MID_CACHE_MISSING: no poly events in window for requested coins/minutes")
    elif len(events_meta) < n_events * 0.5:
        honesty.append("MID_CACHE_PARTIAL: fewer parseable events than slug count")

    cash = starting_balance
    trades: list[Trade] = []
    open_pos: dict[tuple[str, int], dict[str, Any]] = {}
    equity_peak = starting_balance
    max_dd = 0.0
    max_dd_usd = 0.0

    def mark() -> float:
        return cash + sum(p["cost"] for p in open_pos.values())

    def resolve_due(now_u: int) -> None:
        nonlocal cash, equity_peak, max_dd, max_dd_usd
        done = [k for k, p in open_pos.items() if p["window_end_u"] <= now_u]
        for key in sorted(done, key=lambda k: open_pos[k]["window_end_u"]):
            p = open_pos.pop(key)
            # Locked pairs: always +shares at resolution (both sides pay $1 total)
            if p["kind"] == "lock":
                shares = p["shares"]
                payout = shares * 1.0  # one of YES/NO pays $1
                fee = p["fee"]
                pnl = shares * (1.0 - p["entry_p"])  # entry_p = sum
                cash += payout
                cash -= fee
                pnl_after = pnl - fee
                equity = mark()
                equity_peak = max(equity_peak, equity)
                dd = (equity_peak - equity) / equity_peak if equity_peak else 0.0
                max_dd = max(max_dd, dd)
                max_dd_usd = max(max_dd_usd, equity_peak - equity)
                trades.append(
                    Trade(
                        coin=p["coin"],
                        symbol=COIN_SYMBOL.get(p["coin"], p["coin"]),
                        window_start=datetime.fromtimestamp(p["window_start_u"], tz=timezone.utc),
                        window_end=datetime.fromtimestamp(p["window_end_u"], tz=timezone.utc),
                        signal_time=datetime.fromtimestamp(p["fill_u"], tz=timezone.utc),
                        fill_time=datetime.fromtimestamp(p["fill_u"], tz=timezone.utc),
                        direction="LOCK",
                        move_pct=0.0,
                        fair_up=0.5,
                        fair_side=0.5,
                        entry_p=p["entry_p"],
                        entry_source="poly_mid_lock",
                        shares=shares,
                        cost=p["cost"],
                        resolved="LOCK",
                        won=pnl_after > 0,
                        scratched=False,
                        pnl=pnl,
                        fee=fee,
                        pnl_after_fee=pnl_after,
                        equity_after=equity,
                    )
                )

    for ev in events_meta:
        resolve_due(ev["start"])
        if len(open_pos) >= max_concurrent:
            continue
        key = (ev["coin"], ev["start"])
        if key in open_pos:
            continue
        hist_up = _load_hist(ev["up_id"], ev["start"], ev["end"])
        hist_down = _load_hist(ev["down_id"], ev["start"], ev["end"])
        if not hist_up or not hist_down:
            continue
        n_with_hist += 1
        winner = winning_side(ev["outcomes"], ev["outcome_prices"])
        if winner is None:
            # Fallback: if closed market without clean 0/1, skip (honest)
            continue
        n_resolved += 1
        up = filter_window_history(hist_up, ev["start"], ev["end"])
        down = filter_window_history(hist_down, ev["start"], ev["end"])
        pairs = aligned_pairs(up, down, align_tol_sec=15)
        lock_pr = first_lock(pairs, min_lock=min_lock)
        if lock_pr is None:
            continue
        if lock_pr.sum_p > pair_gate + 1e-12:
            continue
        # size
        equity = mark()
        if shares_mode == "fixed20":
            shares = fixed_shares
        else:
            # equity-scaled: spend up to risk_frac * equity on the pair cost
            budget = min(risk_frac * equity, 50.0)
            if lock_pr.sum_p <= 0:
                continue
            shares = budget / lock_pr.sum_p
        cost = shares * lock_pr.sum_p
        if cost > cash + 1e-9 or shares < 0.5:
            continue
        fee = shares * (poly_taker_fee(lock_pr.p_up) + poly_taker_fee(lock_pr.p_down))
        cash -= cost
        open_pos[key] = {
            "kind": "lock",
            "coin": ev["coin"],
            "window_start_u": ev["start"],
            "window_end_u": ev["end"],
            "fill_u": lock_pr.t,
            "shares": shares,
            "entry_p": lock_pr.sum_p,
            "cost": cost,
            "fee": fee,
        }

    resolve_due(end_u + 1)
    for p in list(open_pos.values()):
        cash += p["cost"]
    open_pos.clear()

    days, max_dd_path, max_dd_usd_path = days_from_trades(
        trades, start, end, starting_balance
    )
    scored = [t for t in trades if not t.scratched]
    wins = sum(1 for t in scored if t.won)
    n = len(scored)
    final_equity = days[-1].equity_eod if days else starting_balance
    honesty.append(
        f"cache_coverage: events_in_window={n_events} with_hist≈{n_with_hist} "
        f"resolved_traded_base={n_resolved} locks_taken={len(trades)}"
    )
    if n_events == 0:
        honesty.append("NO_FILLS_POSSIBLE_IN_WINDOW")

    summary = {
        "strategy": "pair_lock",
        "mode": "mid_replay",
        "paper_only": True,
        "live_orders": False,
        "starting_balance": starting_balance,
        "final_equity": final_equity,
        "total_pnl": sum(t.pnl for t in trades),
        "total_pnl_after_fee": sum(t.pnl_after_fee for t in trades),
        "total_fees": sum(t.fee for t in trades),
        "fees_charged_to_equity": True,
        "n_trades": len(trades),
        "n_scored": n,
        "n_wins": wins,
        "n_losses": n - wins,
        "n_scratches": 0,
        "win_rate": (wins / n) if n else 0.0,
        "max_drawdown": max(max_dd, max_dd_path),
        "max_drawdown_usd": max(max_dd_usd, max_dd_usd_path),
        "start": _iso(start),
        "end": _iso(end),
        "n_days": len(days),
        "pair_gate": pair_gate,
        "shares_mode": shares_mode,
        "window_min": window_min,
        "coins": coins,
        "caveats": honesty,
        "entry_sources": {"poly_mid_lock": len(trades)},
    }
    return trades, days, summary, honesty


# ---------------------------------------------------------------------------
# Shared directional + pair-complete hybrid engine (dan_desk / dip / momentum)
# ---------------------------------------------------------------------------


def run_hybrid_lag(
    bars_by_coin: dict[str, list[Bar]],
    *,
    start: datetime,
    end: datetime,
    threshold: float,
    window_min: int,
    starting_balance: float,
    catchup: float,
    slip: float,
    min_edge: float,
    max_elapsed: float = REALISTIC_MAX_ELAPSED,
    min_remain_after_fill: float = REALISTIC_MIN_REMAIN,
    pair_complete_gate: float | None = 0.98,
    pair_complete_window_sec: float = 180.0,
    kelly_frac: float = 0.0,
    risk_frac: float = 0.05,
    fixed_clip: float = 50.0,
    max_concurrent: int = 3,
    fill_delay_bars: int = 1,
    mode_label: str = "hybrid",
    dip_drop_pct: float | None = None,
    dip_lookback_bars: int = 3,
) -> tuple[list[Trade], list[DayRow], dict[str, Any], list[str]]:
    """Catch-up lag entry; optional async pair-complete; optional dip trigger.

    Pricing model is catch-up (NOT live asks). Fees charged to equity.
    """
    honesty = [
        "ENTRY_MODELED: catch-up/slip proxy — not real CLOB asks",
        "Fees charged: 0.07*p*(1-p) per side into equity",
        "Resolution = Binance window close vs open (not Polymarket oracle)",
        "PAPER ONLY",
    ]
    if pair_complete_gate is not None:
        honesty.append(
            f"ASYNC_PAIR: second leg if modeled opp ask makes sum<{pair_complete_gate}"
        )
    if dip_drop_pct is not None:
        honesty.append(
            f"DIP_ARB: trigger when modeled mid drops >={dip_drop_pct:.0%} "
            f"over {dip_lookback_bars}m (1m-bar proxy for panic); not true 3s tape"
        )

    window_sec = window_min * 60
    cash = starting_balance
    trades: list[Trade] = []
    indexed: dict[str, list[Bar]] = {
        c: [b for b in bars if b.open_time < end] for c, bars in bars_by_coin.items()
    }
    # pre-index by open_time for speed
    by_time: dict[str, dict[datetime, int]] = {}
    for c, bars in indexed.items():
        by_time[c] = {b.open_time: i for i, b in enumerate(bars)}

    events: list[tuple[datetime, str, int]] = []
    for coin, bars in indexed.items():
        for i in range(1, len(bars)):
            st = bars[i].close_time
            if start <= st < end:
                events.append((st, coin, i))
    events.sort(key=lambda x: (x[0], x[1]))

    traded_windows: set[tuple[str, int]] = set()
    open_pos: dict[tuple[str, int], dict[str, Any]] = {}
    equity_peak = starting_balance
    max_dd = 0.0
    max_dd_usd = 0.0

    def mark() -> float:
        return cash + sum(p["cost"] for p in open_pos.values())

    def model_entry(fair_side: float) -> float:
        return clamp(0.50 + catchup * (fair_side - 0.50) + slip, 0.51, 0.92)

    def try_pair_complete(p: dict[str, Any], now: datetime, bars: list[Bar]) -> bool:
        """If opposite modeled ask makes sum < gate, lock and return True."""
        nonlocal cash
        if pair_complete_gate is None:
            return False
        if p.get("paired"):
            return False
        elapsed_since_fill = (now - p["fill_time"]).total_seconds()
        if elapsed_since_fill < 0 or elapsed_since_fill > pair_complete_window_sec:
            return False
        # find bar at now
        floored = now.replace(second=0, microsecond=0)
        idx = None
        for cand in (floored - timedelta(minutes=1), floored):
            idx = by_time[p["coin"]].get(cand)
            if idx is not None:
                break
        if idx is None:
            return False
        spot = bars[idx].close
        win_open = p["win_open"]
        try:
            fair_up = crude_fair_up(spot, win_open, scale=FAIR_SCALE)
        except ValueError:
            return False
        fair_opp = (1.0 - fair_up) if p["direction"] == "UP" else fair_up
        opp_ask = model_entry(fair_opp)
        # For opposite (unfavored) side after a move, catch-up from 0.5 may still
        # be high; also allow a softer quote: min(opp_ask, 1.0 - p["entry_p"] + 0.02)
        # Stay honest: only use modeled catch-up, no free lunch haircut.
        sum_p = p["entry_p"] + opp_ask
        if sum_p >= pair_complete_gate:
            return False
        # Buy opp with same share count
        shares = p["shares"]
        cost2 = shares * opp_ask
        if cost2 > cash + 1e-9:
            return False
        fee2 = shares * poly_taker_fee(opp_ask)
        cash -= cost2
        p["paired"] = True
        p["opp_p"] = opp_ask
        p["cost"] += cost2
        p["fee"] = p.get("fee", 0.0) + fee2
        p["entry_sum"] = sum_p
        return True

    def resolve_due(now: datetime) -> None:
        nonlocal cash, equity_peak, max_dd, max_dd_usd
        done = [k for k, p in open_pos.items() if p["window_end"] <= now]
        for key in sorted(done, key=lambda k: open_pos[k]["window_end"]):
            p = open_pos.pop(key)
            coin = p["coin"]
            bars = indexed[coin]
            open_px, close_px = _window_open_close(bars, p["window_start"], p["window_end"])
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
            if p.get("paired"):
                # Locked: payout $1 per share (one side wins)
                ep = p["entry_sum"]
                fee = p.get("fee", 0.0) + shares * poly_taker_fee(p["entry_p"])
                # fee already has opp; add leg1 if not stored
                if "fee_leg1" in p:
                    fee = p["fee_leg1"] + p.get("fee", 0.0) - p.get("fee_leg1_dup", 0.0)
                # recompute cleanly
                fee = shares * (poly_taker_fee(p["entry_p"]) + poly_taker_fee(p["opp_p"]))
                pnl = shares * (1.0 - ep)
                cash += shares * 1.0
                cash -= fee
                scratched = False
                won = pnl - fee > 0
                entry_source = "dan_pair_lock"
                direction = "LOCK"
            else:
                ep = p["entry_p"]
                fee = shares * poly_taker_fee(ep)
                scratched = False
                if resolved == "FLAT":
                    scratched = True
                    won = False
                    pnl = 0.0
                    cash += p["cost"]
                    cash -= fee
                else:
                    won = resolved == p["direction"]
                    if won:
                        pnl = shares * (1.0 - ep)
                        cash += shares * 1.0
                    else:
                        pnl = shares * (-ep)
                    cash -= fee
                entry_source = p["entry_source"]
                direction = p["direction"]

            equity = mark()
            equity_peak = max(equity_peak, equity)
            dd = (equity_peak - equity) / equity_peak if equity_peak else 0.0
            max_dd = max(max_dd, dd)
            max_dd_usd = max(max_dd_usd, equity_peak - equity)
            trades.append(
                Trade(
                    coin=coin,
                    symbol=COIN_SYMBOL.get(coin, coin),
                    window_start=p["window_start"],
                    window_end=p["window_end"],
                    signal_time=p["signal_time"],
                    fill_time=p["fill_time"],
                    direction=direction,
                    move_pct=p["move_pct"],
                    fair_up=p.get("fair_up", 0.5),
                    fair_side=p.get("fair_side", 0.5),
                    entry_p=ep if not p.get("paired") else p["entry_sum"],
                    entry_source=entry_source,
                    shares=shares,
                    cost=p["cost"],
                    resolved=resolved if not p.get("paired") else "LOCK",
                    won=won and not scratched,
                    scratched=scratched,
                    pnl=pnl,
                    fee=fee,
                    pnl_after_fee=pnl - fee,
                    equity_after=equity,
                )
            )

    for sig_time, coin, i in events:
        resolve_due(sig_time)
        # Attempt pair-complete on open positions for this coin
        if pair_complete_gate is not None:
            for p in list(open_pos.values()):
                if p["coin"] == coin and not p.get("paired"):
                    try_pair_complete(p, sig_time, indexed[coin])

        bars = indexed[coin]
        prev = bars[i - 1]
        cur = bars[i]
        if prev.close <= 0:
            continue

        w_start, w_end = window_bounds(cur.open_time, window_sec)
        if not (w_start <= cur.open_time < w_end):
            continue
        if w_end > end or w_start < start:
            continue

        key = (coin, int(w_start.timestamp()))
        if key in traded_windows or key in open_pos:
            continue
        if len(open_pos) >= max_concurrent:
            continue

        open_idx = by_time[coin].get(w_start)
        if open_idx is None:
            continue
        win_open_px = bars[open_idx].open
        if win_open_px is None or win_open_px <= 0:
            continue

        # --- signal detection ---
        direction: str | None = None
        move = (cur.close - prev.close) / prev.close
        entry_source = "catchup"
        move_pct = move

        if dip_drop_pct is not None:
            # Dip: modeled fair_side path drops sharply vs recent peak in-window
            # Use crude fair of UP; detect relative drop on the expensive side.
            try:
                fair_up_now = crude_fair_up(cur.close, win_open_px, scale=FAIR_SCALE)
            except ValueError:
                continue
            # look back within window
            peak_up = fair_up_now
            peak_down = 1.0 - fair_up_now
            for j in range(max(0, i - dip_lookback_bars), i + 1):
                b = bars[j]
                if b.open_time < w_start:
                    continue
                try:
                    fu = crude_fair_up(b.close, win_open_px, scale=FAIR_SCALE)
                except ValueError:
                    continue
                peak_up = max(peak_up, fu)
                peak_down = max(peak_down, 1.0 - fu)
            drop_up = (peak_up - fair_up_now) / peak_up if peak_up > 1e-9 else 0.0
            drop_down = (
                (peak_down - (1.0 - fair_up_now)) / peak_down if peak_down > 1e-9 else 0.0
            )
            # Buy the side that panicked (dropped)
            if drop_up >= dip_drop_pct and drop_up >= drop_down:
                direction = "UP"
                move_pct = -drop_up
                entry_source = "dip_arb"
            elif drop_down >= dip_drop_pct:
                direction = "DOWN"
                move_pct = -drop_down
                entry_source = "dip_arb"
            else:
                continue
        else:
            if abs(move) < threshold:
                continue
            direction = "UP" if move > 0 else "DOWN"

        elapsed = (sig_time - w_start).total_seconds()
        if not (0 < elapsed <= max_elapsed):
            continue
        if i + fill_delay_bars >= len(bars):
            continue
        fill_bar = bars[i + fill_delay_bars]
        fill_time = fill_bar.close_time
        if not (w_start <= fill_bar.open_time < w_end):
            continue
        remaining_after_fill = (w_end - fill_time).total_seconds()
        if remaining_after_fill < min_remain_after_fill:
            continue

        try:
            fair_up = crude_fair_up(fill_bar.close, win_open_px, scale=FAIR_SCALE)
        except ValueError:
            continue
        fair_side = fair_up if direction == "UP" else (1.0 - fair_up)
        ep = model_entry(fair_side)
        edge = fair_side - ep
        if edge < min_edge:
            continue

        equity = mark()
        if kelly_frac > 0:
            # edge-sized Kelly clip: f = kelly_frac * edge / p_lose approx
            # size = equity * min(risk_frac, kelly_frac * edge)
            clip = equity * min(risk_frac, kelly_frac * max(edge, 0.0))
            clip = min(clip, risk_frac * equity, fixed_clip)
        else:
            clip = min(risk_frac * equity, fixed_clip)
        if clip < 1.0 or ep <= 0 or ep >= 1:
            continue
        shares = clip / ep
        cost = shares * ep
        if cost > cash + 1e-9:
            continue

        cash -= cost
        traded_windows.add(key)
        open_pos[key] = {
            "coin": coin,
            "window_start": w_start,
            "window_end": w_end,
            "signal_time": sig_time,
            "fill_time": fill_time,
            "direction": direction,
            "move_pct": move_pct,
            "entry_p": ep,
            "entry_source": entry_source,
            "fair_up": fair_up,
            "fair_side": fair_side,
            "shares": shares,
            "cost": cost,
            "win_open": win_open_px,
            "paired": False,
            "fee_leg1": shares * poly_taker_fee(ep),
        }

    resolve_due(end + timedelta(seconds=1))
    for p in list(open_pos.values()):
        cash += p["cost"]
    open_pos.clear()

    days, max_dd_path, max_dd_usd_path = days_from_trades(
        trades, start, end, starting_balance
    )
    scored = [t for t in trades if not t.scratched]
    wins = sum(1 for t in scored if t.won)
    n = len(scored)
    final_equity = days[-1].equity_eod if days else starting_balance
    n_paired = sum(1 for t in trades if t.entry_source == "dan_pair_lock")
    honesty.append(f"paired_locks={n_paired} directional_or_other={len(trades) - n_paired}")

    summary = {
        "strategy": mode_label,
        "mode": "realistic_hybrid",
        "paper_only": True,
        "live_orders": False,
        "starting_balance": starting_balance,
        "final_equity": final_equity,
        "total_pnl": sum(t.pnl for t in trades),
        "total_pnl_after_fee": sum(t.pnl_after_fee for t in trades),
        "total_fees": sum(t.fee for t in trades),
        "fees_charged_to_equity": True,
        "n_trades": len(trades),
        "n_scored": n,
        "n_wins": wins,
        "n_losses": n - wins,
        "n_scratches": sum(1 for t in trades if t.scratched),
        "win_rate": (wins / n) if n else 0.0,
        "max_drawdown": max(max_dd, max_dd_path),
        "max_drawdown_usd": max(max_dd_usd, max_dd_usd_path),
        "start": _iso(start),
        "end": _iso(end),
        "n_days": len(days),
        "threshold": threshold,
        "catchup": catchup,
        "slip": slip,
        "min_edge": min_edge,
        "window_min": window_min,
        "coins": sorted(bars_by_coin.keys()),
        "caveats": honesty,
        "entry_sources": {
            "catchup": sum(1 for t in trades if t.entry_source == "catchup"),
            "dip_arb": sum(1 for t in trades if t.entry_source == "dip_arb"),
            "dan_pair_lock": n_paired,
        },
    }
    return trades, days, summary, honesty


def rank_key(r: SleeveResult) -> tuple:
    return (0 if r.fantasy else 1, r.final_equity, -r.max_drawdown, r.n_trades)


def run_spot_lag_variant(
    bars: dict[str, list[Bar]],
    *,
    name: str,
    window_tag: str,
    start: datetime,
    end: datetime,
    catchup: float,
    threshold: float = 0.003,
    window_min: int = 5,
    out_dir: Path,
    fantasy: bool = False,
    notes: str = "",
) -> SleeveResult:
    trades, days, summary = run_spot_lag(
        bars,
        mode="realistic",
        threshold=threshold,
        window_min=window_min,
        start=start,
        end=end,
        starting_balance=STARTING_BALANCE,
        catchup=catchup,
        slip=0.02,
        min_edge=0.04,
    )
    flags = [
        "ENTRY_MODELED: catch-up/slip — not real CLOB asks",
        "Fees charged into equity",
        "PAPER ONLY",
    ]
    if fantasy or catchup <= 0.55:
        flags.append("AGGRESSIVE_CATCHUP: cu<=0.55 often unrealistic vs live books")
        fantasy = True
    return pack_result(
        name, "spot_lag_realistic", window_tag, trades, days, summary, flags,
        {"catchup": catchup, "slip": 0.02, "min_edge": 0.04,
         "threshold": threshold, "window_min": window_min},
        out_dir, fantasy=fantasy, notes=notes,
    )


def screen_round(
    *,
    window_tag: str,
    start: datetime,
    end: datetime,
    bars: dict[str, list[Bar]],
    coins: list[str],
    out_dir: Path,
    iteration: int,
    loose_catchup: bool = False,
    pair_gate_extra: float | None = None,
    windows: list[int] | None = None,
) -> list[SleeveResult]:
    results: list[SleeveResult] = []
    windows = windows or [5]
    print(f"\n=== SCREEN {window_tag} iter={iteration} coins={coins} windows={windows} ===")
    print(f"    {_iso(start)} → {_iso(end)}")

    for cu in (0.70, 0.50, 0.85):
        name = f"spot_lag_cu{cu:.2f}_{window_tag}"
        if iteration > 1:
            name += f"_i{iteration}"
        r = run_spot_lag_variant(
            bars, name=name, window_tag=window_tag, start=start, end=end,
            catchup=cu, out_dir=out_dir, fantasy=(cu <= 0.55),
            notes="baseline realistic spot_lag",
        )
        results.append(r)
        print(f"  {r.name}: eq=${r.final_equity:.2f} trades={r.n_trades} WR={r.win_rate:.1%} DD={r.max_drawdown:.1%} fantasy={r.fantasy}")

    gates = [0.97, 0.95]
    if pair_gate_extra is not None:
        gates.append(pair_gate_extra)
    for gate in gates:
        for smode in ("fixed20", "equity_scaled"):
            use_min = 15
            name = f"pair_lock_g{gate:.2f}_{smode}_{use_min}m_{window_tag}"
            if iteration > 1:
                name += f"_i{iteration}"
            trades, days, summary, flags = run_pair_lock(
                start=start, end=end, coins=coins, window_min=use_min,
                pair_gate=gate, starting_balance=STARTING_BALANCE, shares_mode=smode,
            )
            r = pack_result(
                name, "pair_lock", window_tag, trades, days, summary, flags,
                {"pair_gate": gate, "shares_mode": smode, "window_min": use_min},
                out_dir, notes="mid-based pair complete; MID_NOT_ASK",
            )
            results.append(r)
            print(f"  {r.name}: eq=${r.final_equity:.2f} trades={r.n_trades} WR={r.win_rate:.1%} DD={r.max_drawdown:.1%}")

    for wmin in windows:
        cu = 0.50 if loose_catchup else 0.70
        gate = 0.98
        name = f"dan_desk_cu{cu:.2f}_pg{gate:.2f}_{wmin}m_{window_tag}"
        if iteration > 1:
            name += f"_i{iteration}"
        trades, days, summary, flags = run_hybrid_lag(
            bars, start=start, end=end, threshold=0.003, window_min=wmin,
            starting_balance=STARTING_BALANCE, catchup=cu, slip=0.02, min_edge=0.04,
            pair_complete_gate=gate, kelly_frac=0.25, mode_label="dan_desk",
        )
        fant = cu <= 0.55
        if fant:
            flags.append("AGGRESSIVE_CATCHUP")
        r = pack_result(
            name, "dan_desk", window_tag, trades, days, summary, flags,
            {"catchup": cu, "pair_gate": gate, "window_min": wmin, "kelly_frac": 0.25},
            out_dir, fantasy=fant, notes="SPOTTER+PRIOR+EDGE+KELLY+TAKER+async CLOSER",
        )
        results.append(r)
        print(f"  {r.name}: eq=${r.final_equity:.2f} trades={r.n_trades} WR={r.win_rate:.1%} fantasy={r.fantasy}")

    for drop in (0.10, 0.15, 0.20):
        for wmin in windows:
            name = f"dip_arb_d{drop:.2f}_{wmin}m_{window_tag}"
            if iteration > 1:
                name += f"_i{iteration}"
            trades, days, summary, flags = run_hybrid_lag(
                bars, start=start, end=end, threshold=0.0, window_min=wmin,
                starting_balance=STARTING_BALANCE, catchup=0.70, slip=0.02, min_edge=0.03,
                pair_complete_gate=0.98, dip_drop_pct=drop, dip_lookback_bars=3,
                max_elapsed=120, min_remain_after_fill=60, mode_label="dip_arb",
            )
            r = pack_result(
                name, "dip_arb", window_tag, trades, days, summary, flags,
                {"drop": drop, "window_min": wmin, "lookback_bars": 3},
                out_dir, notes="MrFadiAi DipArb idea; 1m-bar proxy",
            )
            results.append(r)
            print(f"  {r.name}: eq=${r.final_equity:.2f} trades={r.n_trades} WR={r.win_rate:.1%}")

    for th in (0.005, 0.008):
        for wmin in windows:
            name = f"spot_mom_th{th:.4f}_{wmin}m_{window_tag}"
            if iteration > 1:
                name += f"_i{iteration}"
            trades, days, summary, flags = run_hybrid_lag(
                bars, start=start, end=end, threshold=th, window_min=wmin,
                starting_balance=STARTING_BALANCE, catchup=0.70, slip=0.02, min_edge=0.05,
                max_elapsed=45, pair_complete_gate=None, kelly_frac=0.25, risk_frac=0.05,
                mode_label="spot_momentum_poly",
            )
            r = pack_result(
                name, "spot_momentum_poly", window_tag, trades, days, summary, flags,
                {"threshold": th, "window_min": wmin, "kelly_frac": 0.25, "max_elapsed": 45},
                out_dir, notes="higher impulse + kelly clip",
            )
            results.append(r)
            print(f"  {r.name}: eq=${r.final_equity:.2f} trades={r.n_trades} WR={r.win_rate:.1%}")

    return results


def write_leaderboard(results: list[SleeveResult], path: Path) -> None:
    ranked = sorted(results, key=rank_key, reverse=True)
    payload = {
        "generated_at": _iso(datetime.now(tz=timezone.utc)),
        "paper_only": True,
        "target": "1000 to 5000 in 30d",
        "ranking_rule": "non-fantasy first, then final_equity, then lower DD, then trade count",
        "leaderboard": [r.metrics() for r in ranked],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {path}")


def re_run_90d(t: SleeveResult, bars, coins, start, end, out_dir: Path) -> SleeveResult:
    fam = t.family
    p = t.params
    name = t.name.replace("30d", "90d")
    if fam == "spot_lag_realistic":
        return run_spot_lag_variant(
            bars, name=name, window_tag="90d", start=start, end=end,
            catchup=float(p.get("catchup", 0.70)),
            threshold=float(p.get("threshold", 0.003)),
            window_min=int(p.get("window_min", 5)),
            out_dir=out_dir, fantasy=t.fantasy,
        )
    if fam == "pair_lock":
        trades, days, summary, flags = run_pair_lock(
            start=start, end=end, coins=coins,
            window_min=int(p.get("window_min", 15)),
            pair_gate=float(p.get("pair_gate", 0.97)),
            starting_balance=STARTING_BALANCE,
            shares_mode=str(p.get("shares_mode", "fixed20")),
        )
        return pack_result(name, fam, "90d", trades, days, summary, flags, p, out_dir)
    th = float(p.get("threshold", 0.003))
    if fam == "dip_arb":
        th = 0.0
    trades, days, summary, flags = run_hybrid_lag(
        bars, start=start, end=end, threshold=th,
        window_min=int(p.get("window_min", 5)),
        starting_balance=STARTING_BALANCE,
        catchup=float(p.get("catchup", 0.70)),
        slip=0.02,
        min_edge=0.03 if fam == "dip_arb" else 0.04,
        pair_complete_gate=(None if fam == "spot_momentum_poly" else float(p.get("pair_gate", 0.98))),
        kelly_frac=float(p.get("kelly_frac", 0.0 if fam == "dip_arb" else 0.25)),
        dip_drop_pct=(float(p["drop"]) if fam == "dip_arb" and "drop" in p else None),
        max_elapsed=(45 if fam == "spot_momentum_poly" else (120 if fam == "dip_arb" else 60)),
        min_remain_after_fill=(60 if fam == "dip_arb" else 90),
        mode_label=fam,
    )
    return pack_result(name, fam, "90d", trades, days, summary, flags, p, out_dir, fantasy=t.fantasy)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=str, default=str(OUT_DIR))
    ap.add_argument("--end", type=str, default="")
    ap.add_argument("--skip-90d", action="store_true")
    ap.add_argument("--force-iter2", action="store_true")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.end:
        end = datetime.fromisoformat(args.end.replace("Z", "+00:00")).astimezone(timezone.utc)
    else:
        end = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)

    start_30 = end - timedelta(days=30)
    start_90 = end - timedelta(days=90)
    coins_base = ["btc", "eth", "sol"]

    print("Loading/fetching klines for base coins (90d)...")
    bars90, _sources = load_or_fetch_klines(coins_base, start_90, end)
    bars30 = {
        c: [b for b in bars if b.open_time >= start_30 - timedelta(minutes=5)]
        for c, bars in bars90.items()
    }

    results_30 = screen_round(
        window_tag="30d", start=start_30, end=end, bars=bars30,
        coins=coins_base, out_dir=out_dir, iteration=1, windows=[5],
    )
    write_leaderboard(results_30, out_dir / "leaderboard_30d.json")

    honest30 = sorted([r for r in results_30 if not r.fantasy], key=rank_key, reverse=True)
    top3 = honest30[:3]
    print("Top 3 honest 30d:", [t.name for t in top3])

    results_90: list[SleeveResult] = []
    if not args.skip_90d and top3:
        print("90d for top 3 honest + baselines...")
        for t in top3:
            r = re_run_90d(t, bars90, coins_base, start_90, end, out_dir)
            results_90.append(r)
            print(f"  {r.name}: eq=${r.final_equity:.2f}")
        for cu in (0.70, 0.85):
            nm = f"spot_lag_cu{cu:.2f}_90d"
            if any(x.name == nm for x in results_90):
                continue
            results_90.append(
                run_spot_lag_variant(
                    bars90, name=nm, window_tag="90d", start=start_90, end=end,
                    catchup=cu, out_dir=out_dir, fantasy=False,
                )
            )
        write_leaderboard(results_90, out_dir / "leaderboard_90d.json")

    iteration2_ran = False
    need_i2 = args.force_iter2 or not any(r.hits_5x and not r.fantasy for r in results_30)
    if need_i2:
        print("ITERATION 2: widen coins + windows + looser catchup (fee-aware)...")
        iteration2_ran = True
        coins2 = ["btc", "eth", "sol", "xrp", "doge", "bnb"]
        bars2_90, _ = load_or_fetch_klines(coins2, start_90, end)
        bars2_30 = {
            c: [b for b in bars if b.open_time >= start_30 - timedelta(minutes=5)]
            for c, bars in bars2_90.items()
        }
        extra = screen_round(
            window_tag="30d", start=start_30, end=end, bars=bars2_30,
            coins=coins2, out_dir=out_dir, iteration=2, loose_catchup=True,
            pair_gate_extra=0.98, windows=[5, 15],
        )
        results_30.extend(extra)
        write_leaderboard(results_30, out_dir / "leaderboard_30d.json")

        honest30 = sorted([r for r in results_30 if not r.fantasy], key=rank_key, reverse=True)
        for t in honest30[:3]:
            nm90 = t.name.replace("30d", "90d")
            if any(x.name == nm90 for x in results_90):
                continue
            use = {c: bars2_90[c] for c in coins2 if c in bars2_90}
            r = re_run_90d(t, use, coins2, start_90, end, out_dir)
            results_90.append(r)
            print(f"  i2-90d {r.name}: eq=${r.final_equity:.2f}")
        write_leaderboard(results_90, out_dir / "leaderboard_90d.json")

    # Persist iteration flag for report builder
    (out_dir / "meta.json").write_text(json.dumps({
        "iteration2_ran": iteration2_ran,
        "end": _iso(end),
        "start_30": _iso(start_30),
        "start_90": _iso(start_90),
    }, indent=2) + "\n")

    # Inline minimal report (full prose via hunt_report if available)
    try:
        from hunt_report import build_report
        rp = build_report(out_dir, iteration2_ran)
        print(f"wrote {rp}")
    except Exception as exc:
        print(f"hunt_report unavailable ({exc}); writing stub REPORT.md")
        honest = sorted([r for r in results_30 if not r.fantasy], key=rank_key, reverse=True)
        b = honest[0] if honest else None
        lines = ["# Sleeve Hunt Report", "", "PAPER ONLY", ""]
        if b:
            lines.append("Best honest 30d: %s $%.2f" % (b.name, b.final_equity))
            lines.append("hits_5x=%s fantasy_top=%s" % (
                any(r.hits_5x and not r.fantasy for r in results_30),
                any(r.hits_5x and r.fantasy for r in results_30),
            ))
            lines.append("daily: %s" % b.daily_path)
        lines.append("iteration2=%s" % iteration2_ran)
        (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n")

    honest30 = sorted([r for r in results_30 if not r.fantasy], key=rank_key, reverse=True)
    print("=" * 72)
    print("HUNT COMPLETE")
    if honest30:
        b = honest30[0]
        print("Best honest 30d: %s $%.2f trades=%d" % (b.name, b.final_equity, b.n_trades))
        print("Hits 5k honest? %s" % ("YES" if b.hits_5x else "NO"))
    print("Out: %s" % out_dir)
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
