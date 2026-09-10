"""Polymarket Up/Down paper backtest. Public history only — no live orders."""

from __future__ import annotations

import asyncio
import csv
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.table import Table

from stablebot.config import AppConfig, data_dir
from stablebot.exchanges.base import USER_AGENT
from stablebot.poly.client import CLOB, GAMMA, _token_map
from stablebot.poly.markets import interval_seconds, parse_coins, parse_minutes_list, slug_for
from stablebot.poly.replay import WindowResult, WindowTrade, replay_window

console = Console(width=200)

MID_ASK_CAVEAT = (
    "prices-history last/mid ≠ ask. Live 2026-08-14 books showed sum of asks "
    "= 1.01 while mids sat on 1.00. Lock fills on mids may be unfillable at ask."
)
FEE_NOTE = (
    "Main PnL assumes fee=0. 'fees' is a 0.07*p*(1-p) per-side taker sensitivity "
    "(not subtracted from equity). No rebate invented."
)


class Throttle:
    def __init__(self, min_interval: float = 0.11):
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._next = 0.0
        self.n_429 = 0
        self.n_calls = 0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
            self._next = time.monotonic() + self.min_interval
            self.n_calls += 1


def _iso(ts: int | float) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def _day(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")


def expected_starts(range_start: int, range_end: int, minutes: int) -> list[int]:
    """Completed windows only: start + interval <= range_end."""
    interval = interval_seconds(minutes)
    t = (int(range_start) // interval) * interval
    if t < range_start:
        t += interval
    out: list[int] = []
    while t + interval <= range_end:
        out.append(t)
        t += interval
    return out


def cache_dir() -> Path:
    d = data_dir() / "poly_cache"
    (d / "events").mkdir(parents=True, exist_ok=True)
    (d / "history").mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


async def _get_json(
    http: httpx.AsyncClient,
    throttle: Throttle,
    url: str,
    params: dict[str, Any] | None = None,
    retries: int = 5,
) -> Any:
    delay = 1.5
    last_exc: Exception | None = None
    for _ in range(retries):
        await throttle.wait()
        try:
            resp = await http.get(url, params=params)
            if resp.status_code == 429:
                throttle.n_429 += 1
                ra = resp.headers.get("Retry-After")
                wait = float(ra) if ra and ra.isdigit() else delay
                await asyncio.sleep(min(30.0, max(1.0, wait)))
                delay = min(20.0, delay * 1.8)
                continue
            # Gamma returns 422 for deep series offsets (offset>=2100). Don't retry.
            if resp.status_code == 422:
                raise httpx.HTTPStatusError(
                    f"Client error '422 Unprocessable Entity' for url '{resp.url}'",
                    request=resp.request,
                    response=resp,
                )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response is not None and exc.response.status_code == 422:
                raise
            await asyncio.sleep(delay)
            delay = min(20.0, delay * 1.8)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            last_exc = exc
            await asyncio.sleep(delay)
            delay = min(20.0, delay * 1.8)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"GET failed: {url}")


async def fetch_event_cached(
    http: httpx.AsyncClient, throttle: Throttle, slug: str
) -> dict[str, Any] | None:
    path = cache_dir() / "events" / f"{slug}.json"
    hit = _read_json(path)
    if isinstance(hit, dict):
        return hit
    data = await _get_json(http, throttle, f"{GAMMA}/events", {"slug": slug})
    ev = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None
    if ev:
        _write_json(path, ev)
    else:
        _write_json(path, {"_missing": True, "slug": slug})
    return ev


async def fetch_history_cached(
    http: httpx.AsyncClient,
    throttle: Throttle,
    token_id: str,
    window_start: int | None = None,
    window_end: int | None = None,
) -> list[dict[str, Any]]:
    """CLOB prices-history. interval=1d only covers ~last day; use startTs/endTs."""
    safe = token_id[:80]
    if window_start is not None and window_end is not None:
        path = cache_dir() / "history" / f"{safe}_{window_start}_{window_end}.json"
        params: dict[str, Any] = {
            "market": token_id,
            "startTs": int(window_start),
            "endTs": int(window_end),
            "fidelity": 1,
        }
    else:
        path = cache_dir() / "history" / f"{safe}_max.json"
        params = {"market": token_id, "interval": "max", "fidelity": 1}
    hit = _read_json(path)
    if isinstance(hit, dict) and isinstance(hit.get("history"), list):
        return hit["history"]
    data = await _get_json(http, throttle, f"{CLOB}/prices-history", params)
    hist = data.get("history") if isinstance(data, dict) else None
    if not isinstance(hist, list):
        hist = []
    _write_json(path, {"history": hist})
    return hist


async def fetch_series_page(
    http: httpx.AsyncClient,
    throttle: Throttle,
    series_id: str,
    offset: int,
    limit: int = 100,
) -> list[dict[str, Any]]:
    # Gamma /events rejects offset>=2100 with 422. Stop paging; caller hole-fills.
    if offset >= 2000:
        return []
    try:
        data = await _get_json(
            http,
            throttle,
            f"{GAMMA}/events",
            {
                "series_id": series_id,
                "limit": limit,
                "offset": offset,
                "closed": "true",
                "order": "id",
                "ascending": "false",
            },
        )
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 422:
            return []
        raise
    return data if isinstance(data, list) else []


def parse_slug_start(slug: str) -> int | None:
    try:
        return int(slug.rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return None


def event_tokens_and_resolution(
    event: dict[str, Any],
) -> tuple[dict[str, str], Any, Any]:
    markets = event.get("markets") or []
    if not markets:
        return {}, None, None
    m = markets[0]
    tokens = _token_map(m)
    return tokens, m.get("outcomes"), m.get("outcomePrices")


async def discover_series_id(
    http: httpx.AsyncClient, throttle: Throttle, coin: str, minutes: int, now_ts: float
) -> str | None:
    interval = interval_seconds(minutes)
    start = (int(now_ts) // interval) * interval - interval  # last completed
    slug = slug_for(coin, minutes, start)
    ev = await fetch_event_cached(http, throttle, slug)
    if not ev:
        # try one window earlier
        ev = await fetch_event_cached(http, throttle, slug_for(coin, minutes, start - interval))
    if not ev:
        return None
    series = ev.get("series") or []
    if series and isinstance(series[0], dict) and series[0].get("id"):
        return str(series[0]["id"])
    return None


async def load_events_for_coin(
    http: httpx.AsyncClient,
    throttle: Throttle,
    coin: str,
    minutes: int,
    starts: list[int],
    now_ts: float,
) -> tuple[dict[int, dict[str, Any]], list[int]]:
    """Return start_unix -> event, plus missing starts."""
    found: dict[int, dict[str, Any]] = {}
    # seed from cache
    for st in starts:
        slug = slug_for(coin, minutes, st)
        cached = _read_json(cache_dir() / "events" / f"{slug}.json")
        if isinstance(cached, dict) and cached.get("_missing"):
            continue
        if isinstance(cached, dict) and cached.get("markets"):
            found[st] = cached

    missing = [st for st in starts if st not in found]
    if not missing:
        return found, []

    series_id = await discover_series_id(http, throttle, coin, minutes, now_ts)
    if series_id:
        lo, hi = min(missing), max(missing)
        offset = 0
        pages = 0
        while pages < 80:
            page = await fetch_series_page(http, throttle, series_id, offset)
            pages += 1
            if not page:
                break
            oldest_on_page: int | None = None
            for ev in page:
                if not isinstance(ev, dict):
                    continue
                slug = str(ev.get("slug") or "")
                st = parse_slug_start(slug)
                if st is None:
                    continue
                oldest_on_page = st if oldest_on_page is None else min(oldest_on_page, st)
                if ev.get("markets"):
                    _write_json(cache_dir() / "events" / f"{slug}.json", ev)
                if st in found or st < lo or st > hi:
                    continue
                if ev.get("markets"):
                    found[st] = ev
            if oldest_on_page is not None and oldest_on_page < lo:
                break
            if len(page) < 100:
                break
            offset += len(page)

    still = [st for st in starts if st not in found]
    # hole-fill a limited number of missing slugs (don't explode on a bad series)
    for st in still:
        ev = await fetch_event_cached(http, throttle, slug_for(coin, minutes, st))
        if ev and ev.get("markets") and not ev.get("_missing"):
            found[st] = ev
    missing_final = [st for st in starts if st not in found]
    return found, missing_final


@dataclass
class DailyRow:
    date: str
    windows_seen: int = 0
    lock_trades: int = 0
    lock_pnl: float = 0.0
    fade_trades: int = 0
    fade_pnl: float = 0.0
    fees: float = 0.0
    end_equity: float = 0.0
    end_equity_lock: float = 0.0
    end_equity_lock_fade: float = 0.0


@dataclass
class PolyBacktestResult:
    start: datetime
    end: datetime
    minutes: int
    coins: list[str]
    starting_balance: float
    shares: float
    min_lock: float
    fade: bool
    days: list[DailyRow] = field(default_factory=list)
    locks: list[WindowTrade] = field(default_factory=list)
    fades: list[WindowTrade] = field(default_factory=list)
    windows_expected: int = 0
    windows_fetched: int = 0
    windows_missing: int = 0
    windows_unresolved: int = 0
    windows_no_history: int = 0
    windows_replayed: int = 0
    rate_limit_hits: int = 0
    api_calls: int = 0
    notes: list[str] = field(default_factory=list)
    ending_equity_lock: float = 0.0
    ending_equity_lock_fade: float = 0.0

    @property
    def ending_equity(self) -> float:
        return self.ending_equity_lock_fade if self.fade else self.ending_equity_lock

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "minutes": self.minutes,
            "coins": self.coins,
            "starting_balance": self.starting_balance,
            "shares": self.shares,
            "min_lock": self.min_lock,
            "fade_enabled_in_equity": self.fade,
            "caveats": [MID_ASK_CAVEAT, FEE_NOTE, *self.notes],
            "fetch": {
                "windows_expected": self.windows_expected,
                "windows_fetched": self.windows_fetched,
                "windows_missing": self.windows_missing,
                "windows_unresolved": self.windows_unresolved,
                "windows_no_history": self.windows_no_history,
                "windows_replayed": self.windows_replayed,
                "rate_limit_hits": self.rate_limit_hits,
                "api_calls": self.api_calls,
            },
            "summary": {
                "lock_trades": len(self.locks),
                "lock_pnl": round(sum(t.pnl for t in self.locks), 6),
                "lock_pnl_with_curve_fee": round(sum(t.pnl_fee for t in self.locks), 6),
                "fade_trades": len(self.fades),
                "fade_pnl": round(sum(t.pnl for t in self.fades), 6),
                "fade_pnl_with_curve_fee": round(sum(t.pnl_fee for t in self.fades), 6),
                "fees_curve": round(sum(t.fee for t in (*self.locks, *self.fades)), 6),
                "ending_equity_lock": round(self.ending_equity_lock, 6),
                "ending_equity_lock_fade": round(self.ending_equity_lock_fade, 6),
                "ending_equity": round(self.ending_equity, 6),
            },
            "daily": [asdict(d) for d in self.days],
            "lock_trades": [asdict(t) for t in self.locks],
            "fade_trades": [asdict(t) for t in self.fades],
        }


async def run_poly_backtest(
    cfg: AppConfig,
    *,
    days: int,
    balance: float,
    fade: bool = False,
    coins: list[str] | None = None,
    minutes: int = 15,
    shares: float | None = None,
    now_ts: float | None = None,
    sleep_s: float = 0.11,
) -> PolyBacktestResult:
    coins = coins or ["btc", "eth", "sol"]
    shares = 20.0 if shares is None else shares
    now_ts = time.time() if now_ts is None else now_ts
    range_end = int(now_ts)
    range_start = range_end - int(days) * 86400
    starts = expected_starts(range_start, range_end, minutes)
    interval = interval_seconds(minutes)
    min_lock = cfg.poly.min_lock
    fade_thr = cfg.poly.fade_threshold

    result = PolyBacktestResult(
        start=datetime.fromtimestamp(starts[0] if starts else range_start, timezone.utc),
        end=datetime.fromtimestamp(range_end, timezone.utc),
        minutes=minutes,
        coins=list(coins),
        starting_balance=balance,
        shares=shares,
        min_lock=min_lock,
        fade=fade,
    )
    result.windows_expected = len(starts) * len(coins)
    result.notes.append(
        f"align_tol=15s; fade early_frac=1/3; fade gate |p-0.5|>={fade_thr} "
        "(cheap side); one lock max per window; fade only if no lock."
    )
    result.notes.append(MID_ASK_CAVEAT)
    result.notes.append(FEE_NOTE)
    result.notes.append("Paper only. No live Polymarket orders.")
    result.notes.append(
        "History via prices-history startTs/endTs&fidelity=1. "
        "interval=1d only returns ~last day (empty for older resolved windows)."
    )

    throttle = Throttle(sleep_s)
    timeout = httpx.Timeout(20.0)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    windows: list[WindowResult] = []
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as http:
        coin_events: dict[str, dict[int, dict]] = {}
        for coin in coins:
            events, missing = await load_events_for_coin(
                http, throttle, coin, minutes, starts, now_ts
            )
            coin_events[coin] = events
            result.windows_missing += len(missing)
            result.windows_fetched += len(events)
            console.print(
                f"[dim]{coin} {minutes}m events {len(events)}/{len(starts)} "
                f"missing={len(missing)}[/dim]"
            )

        jobs: list[tuple[str, int, int]] = []
        seen_t: set[str] = set()
        for events in coin_events.values():
            for st, ev in events.items():
                tokens, _, _ = event_tokens_and_resolution(ev)
                for side in ("up", "down"):
                    tid = tokens.get(side)
                    if tid and tid not in seen_t:
                        seen_t.add(tid)
                        jobs.append((tid, st, st + interval))
        console.print(f"[dim]prefetch {len(jobs)} price-history series via startTs/endTs (throttle {sleep_s*1000:.0f}ms)[/dim]")

        async def _one_hist(job: tuple[str, int, int]) -> tuple[str, list, str | None]:
            tid, w0, w1 = job
            try:
                hist = await fetch_history_cached(http, throttle, tid, w0, w1)
                return tid, hist, None
            except Exception as exc:  # noqa: BLE001
                return tid, [], f"{type(exc).__name__}: {exc}"

        async def _progress() -> None:
            while True:
                await asyncio.sleep(15)
                console.print(
                    f"[dim]  history calls={throttle.n_calls} 429s={throttle.n_429}[/dim]"
                )

        prog = asyncio.create_task(_progress())
        try:
            fetched = await asyncio.gather(*[_one_hist(job) for job in jobs])
        finally:
            prog.cancel()
        hist_map: dict[str, list] = {}
        for tid, hist, err in fetched:
            hist_map[tid] = hist
            if err:
                result.notes.append(f"history fail {tid[:16]}: {err}")

        for coin in coins:
            events = coin_events[coin]
            for st in starts:
                ev = events.get(st)
                slug = slug_for(coin, minutes, st)
                if ev is None:
                    continue
                tokens, outcomes, prices = event_tokens_and_resolution(ev)
                if "up" not in tokens or "down" not in tokens:
                    result.windows_unresolved += 1
                    continue
                hist_up = hist_map.get(tokens["up"], [])
                hist_down = hist_map.get(tokens["down"], [])
                wr = replay_window(
                    slug=slug,
                    coin=coin,
                    minutes=minutes,
                    start_unix=st,
                    end_unix=st + interval,
                    hist_up=hist_up,
                    hist_down=hist_down,
                    outcomes=outcomes,
                    outcome_prices=prices,
                    shares=shares,
                    min_lock=min_lock,
                    fade_threshold=fade_thr,
                    allow_fade=True,
                )
                if not wr.resolved:
                    result.windows_unresolved += 1
                if wr.n_up == 0 and wr.n_down == 0:
                    result.windows_no_history += 1
                windows.append(wr)
                result.windows_replayed += 1

    result.rate_limit_hits = throttle.n_429
    result.api_calls = throttle.n_calls

    # cash-aware books
    cash_lock = balance
    cash_both = balance
    by_day: dict[str, DailyRow] = {}
    # one row per UTC day covering the span
    if starts:
        d0 = datetime.fromtimestamp(starts[0] + interval, timezone.utc).date()
        d1 = datetime.fromtimestamp(range_end, timezone.utc).date()
        cur = datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc)
        end_d = datetime(d1.year, d1.month, d1.day, tzinfo=timezone.utc)
        while cur <= end_d:
            by_day[cur.strftime("%Y-%m-%d")] = DailyRow(date=cur.strftime("%Y-%m-%d"))
            cur = datetime.fromtimestamp(cur.timestamp() + 86400, timezone.utc)

    windows.sort(key=lambda w: (w.end_unix, w.coin))
    for wr in windows:
        day = _day(wr.end_unix)
        row = by_day.setdefault(day, DailyRow(date=day))
        row.windows_seen += 1
        if wr.lock:
            cost = wr.lock.shares * (wr.lock.p_up or 0) + wr.lock.shares * (wr.lock.p_down or 0)
            if cash_lock >= cost:
                cash_lock += wr.lock.pnl
                result.locks.append(wr.lock)
                row.lock_trades += 1
                row.lock_pnl += wr.lock.pnl
                row.fees += wr.lock.fee
            if cash_both >= cost:
                cash_both += wr.lock.pnl
        elif wr.fade:
            cost = wr.fade.shares * wr.fade.fill_px
            result.fades.append(wr.fade)
            row.fade_trades += 1
            row.fade_pnl += wr.fade.pnl
            row.fees += wr.fade.fee
            if cash_both >= cost:
                cash_both += wr.fade.pnl
        # mark equity at end of this window's day (overwrite; chronological)
        row.end_equity_lock = cash_lock
        row.end_equity_lock_fade = cash_both
        row.end_equity = cash_both if fade else cash_lock

    # forward-fill equity on quiet days
    last_l, last_b = balance, balance
    ordered = [by_day[k] for k in sorted(by_day)]
    for row in ordered:
        if row.lock_trades == 0 and row.fade_trades == 0 and row.windows_seen == 0:
            row.end_equity_lock = last_l
            row.end_equity_lock_fade = last_b
            row.end_equity = last_b if fade else last_l
        else:
            if row.end_equity_lock == 0 and row.lock_trades == 0:
                row.end_equity_lock = last_l
            if row.end_equity_lock_fade == 0 and row.fade_trades == 0 and row.lock_trades == 0:
                row.end_equity_lock_fade = last_b
            row.end_equity = row.end_equity_lock_fade if fade else row.end_equity_lock
            last_l = row.end_equity_lock
            last_b = row.end_equity_lock_fade
    result.days = ordered
    result.ending_equity_lock = cash_lock
    result.ending_equity_lock_fade = cash_both
    return result


def print_poly_backtest(result: PolyBacktestResult) -> None:
    span = max(1, (result.end - result.start).total_seconds() / 86400)
    console.rule(
        f"[bold]poly backtest[/bold]  {result.start.date()} → {result.end.date()}  "
        f"({span:.0f}d)  {result.minutes}m  coins={','.join(result.coins)}  "
        f"start ${result.starting_balance:,.2f}"
    )
    for n in result.notes:
        console.print(f"[dim]{n}[/dim]")
    console.print(
        f"windows expected={result.windows_expected} fetched={result.windows_fetched} "
        f"missing={result.windows_missing} unresolved={result.windows_unresolved} "
        f"no_history={result.windows_no_history} replayed={result.windows_replayed} "
        f"429s={result.rate_limit_hits} api_calls={result.api_calls}"
    )

    daily = Table(title="Day-by-day (UTC)")
    for col in (
        "date",
        "windows_seen",
        "lock_trades",
        "lock_pnl",
        "fade_trades",
        "fade_pnl",
        "fees",
        "end_equity",
    ):
        daily.add_column(col, justify="right" if col != "date" else "left", no_wrap=True)
    for d in result.days:
        daily.add_row(
            d.date,
            str(d.windows_seen),
            str(d.lock_trades),
            f"{d.lock_pnl:+,.4f}",
            str(d.fade_trades),
            f"{d.fade_pnl:+,.4f}",
            f"{d.fees:,.4f}",
            f"{d.end_equity:,.2f}",
        )
    console.print(daily)

    sm = Table(title="Summary (fee=0 PnL; fees col = curve sensitivity)")
    sm.add_column("metric")
    sm.add_column("value", justify="right")
    sm.add_row("starting balance", f"${result.starting_balance:,.2f}")
    sm.add_row("lock trades", str(len(result.locks)))
    sm.add_row("lock PnL (fee=0)", f"${sum(t.pnl for t in result.locks):+,.4f}")
    sm.add_row("lock PnL (curve fee)", f"${sum(t.pnl_fee for t in result.locks):+,.4f}")
    sm.add_row("fade trades (no-lock windows)", str(len(result.fades)))
    sm.add_row("fade PnL (fee=0)", f"${sum(t.pnl for t in result.fades):+,.4f}")
    sm.add_row("fade PnL (curve fee)", f"${sum(t.pnl_fee for t in result.fades):+,.4f}")
    sm.add_row("curve fees (sensitivity)", f"${sum(t.fee for t in (*result.locks, *result.fades)):,.4f}")
    sm.add_row("end equity lock-only", f"${result.ending_equity_lock:,.2f}")
    sm.add_row("end equity lock+fade", f"${result.ending_equity_lock_fade:,.2f}")
    sm.add_row(
        "end equity (this run)",
        f"${result.ending_equity:,.2f}  ({'lock+fade' if result.fade else 'lock-only'})",
    )
    console.print(sm)
    if len(result.locks) == 0:
        console.print("[yellow]0 lock trades — valid. Mids rarely sum ≤ 0.995 when aligned.[/yellow]")
    console.print(f"[yellow]{MID_ASK_CAVEAT}[/yellow]")


def save_poly_chart(result: PolyBacktestResult, path: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    dates = [d.date for d in result.days]
    eq_l = [d.end_equity_lock for d in result.days]
    eq_b = [d.end_equity_lock_fade for d in result.days]
    lock_p = [d.lock_pnl for d in result.days]
    fade_p = [d.fade_pnl for d in result.days]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 7.5), sharex=True, gridspec_kw={"height_ratios": [2, 1.4]}
    )
    ax1.plot(dates, eq_l, color="#1f4e79", linewidth=2.0, label="lock-only")
    ax1.plot(dates, eq_b, color="#e76f51", linewidth=1.6, label="lock+fade")
    ax1.axhline(result.starting_balance, color="#888", linestyle="--", linewidth=1, label="start")
    ax1.set_ylabel("Equity (USD)")
    ax1.set_title(
        f"Poly Up/Down {result.minutes}m  {result.start.date()} → {result.end.date()}  "
        f"start ${result.starting_balance:,.0f}  lock ${result.ending_equity_lock:,.2f}  "
        f"lock+fade ${result.ending_equity_lock_fade:,.2f}"
    )
    ax1.legend(loc="best")
    ax1.grid(True, alpha=0.3)

    x = np.arange(len(dates))
    ax2.bar(x, lock_p, 0.8, label="lock", color="#2a9d8f")
    ax2.bar(x, fade_p, 0.8, label="fade (directional)", color="#e9c46a", bottom=None)
    ax2.axhline(0.0, color="#333", linewidth=0.8)
    ax2.set_ylabel("Daily PnL (fee=0)")
    step = max(1, len(x) // 10)
    ax2.set_xticks(x[::step])
    ax2.set_xticklabels([dates[i] for i in range(0, len(dates), step)], rotation=30, ha="right")
    ax2.legend(loc="best")
    ax2.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def save_poly_backtest(result: PolyBacktestResult) -> tuple[Path, Path, Path]:
    out_dir = data_dir() / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = (
        f"poly_{result.minutes}m_{result.start.date()}_{result.end.date()}_"
        f"{int(result.starting_balance)}_{stamp}"
    )
    json_path = out_dir / f"{tag}.json"
    csv_path = out_dir / f"{tag}_daily.csv"
    png_path = out_dir / f"{tag}.png"
    json_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "date",
                "windows_seen",
                "lock_trades",
                "lock_pnl",
                "fade_trades",
                "fade_pnl",
                "fees",
                "end_equity",
            ]
        )
        for d in result.days:
            w.writerow(
                [
                    d.date,
                    d.windows_seen,
                    d.lock_trades,
                    f"{d.lock_pnl:.6f}",
                    d.fade_trades,
                    f"{d.fade_pnl:.6f}",
                    f"{d.fees:.6f}",
                    f"{d.end_equity:.6f}",
                ]
            )
    try:
        save_poly_chart(result, png_path)
        console.print(f"wrote {png_path}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]chart failed: {exc}[/yellow]")
        png_path = Path()
    console.print(f"wrote {json_path}")
    console.print(f"wrote {csv_path}")
    return json_path, csv_path, png_path


async def cmd_poly_backtest(
    cfg: AppConfig,
    *,
    days: int,
    balance: float,
    fade: bool,
    coins_raw: str | None,
    windows_raw: str | None,
    shares: float,
) -> None:
    coins = parse_coins(coins_raw, ["btc", "eth", "sol"])
    coins = [c for c in coins if c != "xrp"]
    mins = parse_minutes_list(windows_raw, [15])
    minutes = mins[0] if mins else 15
    console.print(
        f"poly-backtest days={days} balance={balance} fade={fade} "
        f"coins={coins} minutes={minutes} shares={shares}  [yellow]no live orders[/yellow]"
    )
    result = await run_poly_backtest(
        cfg,
        days=days,
        balance=balance,
        fade=fade,
        coins=coins,
        minutes=minutes,
        shares=shares,
    )
    print_poly_backtest(result)
    save_poly_backtest(result)
