"""Public Kalshi GETs only. Paper research — no orders, no auth, no keys."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from stablebot.exchanges.base import USER_AGENT
from stablebot.kalshi.strategy import lock_edge, pair_curve_fee

HOST = "https://external-api.kalshi.com/trade-api/v2"


class KalshiNotFound(Exception):
    """Series or market 404 — drop the series after one probe."""


class KalshiRateLimit(Exception):
    """429 after retries."""


def _new_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(12.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


def parse_series(raw: str | None, default: list[str]) -> list[str]:
    if not raw:
        return [s.upper() for s in default]
    out: list[str] = []
    for part in raw.split(","):
        s = part.strip().upper()
        if s and s not in out:
            out.append(s)
    return out or [s.upper() for s in default]


def _level_px_sz(level: Any) -> tuple[float | None, float | None]:
    if not isinstance(level, (list, tuple)) or len(level) < 2:
        return None, None
    try:
        px = float(level[0])
        sz = float(level[1])
    except (TypeError, ValueError):
        return None, None
    if px <= 0 or sz <= 0:
        return None, None
    return px, sz


def _best_bid(levels: Any) -> tuple[float | None, float | None]:
    """Kalshi books are bids only, typically ascending; best bid = highest price.

    User-verified 2026-08-15: arrays of [price_str, size_str], ascending,
    best bid = last. We take the max price (last wins on ties) so a reversed
    book still works.
    """
    best_px: float | None = None
    best_sz: float | None = None
    if not isinstance(levels, list):
        return None, None
    for level in levels:
        px, sz = _level_px_sz(level)
        if px is None or sz is None:
            continue
        if best_px is None or px >= best_px:
            best_px, best_sz = px, sz
    return best_px, best_sz


@dataclass(frozen=True)
class BookQuotes:
    yes_bid: float | None = None
    yes_bid_size: float | None = None
    no_bid: float | None = None
    no_bid_size: float | None = None
    yes_ask: float | None = None
    yes_ask_size: float | None = None
    no_ask: float | None = None
    no_ask_size: float | None = None


def parse_orderbook(data: Any) -> BookQuotes:
    """Derive asks from the opposite bid. Kalshi returns bids only.

    yes_ask = 1 - best_no_bid;  yes_ask_size = best_no_bid size (size you lift)
    no_ask  = 1 - best_yes_bid; no_ask_size  = best_yes_bid size
    """
    if not isinstance(data, dict):
        return BookQuotes()
    fp = data.get("orderbook_fp") or {}
    if not isinstance(fp, dict):
        return BookQuotes()
    yes_bid, yes_bid_sz = _best_bid(fp.get("yes_dollars"))
    no_bid, no_bid_sz = _best_bid(fp.get("no_dollars"))
    yes_ask = (1.0 - no_bid) if no_bid is not None else None
    no_ask = (1.0 - yes_bid) if yes_bid is not None else None
    return BookQuotes(
        yes_bid=yes_bid,
        yes_bid_size=yes_bid_sz,
        no_bid=no_bid,
        no_bid_size=no_bid_sz,
        yes_ask=yes_ask,
        yes_ask_size=no_bid_sz,
        no_ask=no_ask,
        no_ask_size=yes_bid_sz,
    )


def _minutes_left(market: dict[str, Any], now_ts: float) -> float | None:
    raw = market.get("close_time") or market.get("expected_expiration_time")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt.timestamp() - now_ts) / 60.0
    except (TypeError, ValueError):
        return None


@dataclass
class ScanRow:
    ticker: str
    series: str
    title: str | None = None
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    yes_ask_size: float | None = None
    no_ask_size: float | None = None
    sum_asks: float | None = None
    curve_fee: float | None = None
    lock_edge: float | None = None
    minutes_left: float | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


class KalshiClient:
    """One reused httpx client. Public GET only. Throttle + 429 backoff."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        throttle_ms: int = 180,
        max_retries: int = 5,
        backoff_start: float = 0.5,
    ):
        self.http = http
        self.throttle_s = max(0.0, throttle_ms / 1000.0)
        self.max_retries = max(1, max_retries)
        self.backoff_start = max(0.0, backoff_start)
        self._last = 0.0
        self.dropped_series: set[str] = set()
        self.drop_notes: list[str] = []

    async def _throttle(self) -> None:
        if self.throttle_s <= 0:
            return
        now = time.monotonic()
        wait = self._last + self.throttle_s - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last = time.monotonic()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else f"{HOST}{path}"
        delay = self.backoff_start
        last_status = None
        for _attempt in range(self.max_retries):
            await self._throttle()
            resp = await self.http.get(url, params=params)
            last_status = resp.status_code
            if resp.status_code == 429:
                if delay > 0:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2 if delay > 0 else 0.5, 16.0)
                continue
            if resp.status_code in {400, 404}:
                raise KalshiNotFound(f"{path} -> {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        raise KalshiRateLimit(f"{path} -> 429 after {self.max_retries} tries (last={last_status})")

    async def list_open_markets(self, series_ticker: str) -> list[dict[str, Any]]:
        series = series_ticker.upper()
        if series in self.dropped_series:
            return []
        try:
            data = await self.get(
                "/markets",
                params={"series_ticker": series, "status": "open", "limit": 10},
            )
        except KalshiNotFound:
            self.dropped_series.add(series)
            self.drop_notes.append(f"dropped {series} (404/400)")
            return []
        except KalshiRateLimit:
            return []
        markets = data.get("markets") if isinstance(data, dict) else None
        if not isinstance(markets, list):
            return []
        return [m for m in markets if isinstance(m, dict)]

    async def orderbook(self, ticker: str) -> BookQuotes:
        data = await self.get(f"/markets/{ticker}/orderbook")
        return parse_orderbook(data)


async def hydrate_row(
    client: KalshiClient,
    market: dict[str, Any],
    series: str,
    now_ts: float,
    apply_curve_fee: bool,
) -> ScanRow:
    ticker = str(market.get("ticker") or "")
    row = ScanRow(
        ticker=ticker,
        series=series,
        title=market.get("title") or market.get("subtitle"),
        minutes_left=_minutes_left(market, now_ts),
    )
    if not ticker:
        row.error = "no ticker"
        return row
    try:
        book = await client.orderbook(ticker)
    except KalshiNotFound as exc:
        row.error = f"orderbook 404: {exc}"
        return row
    except KalshiRateLimit as exc:
        row.error = f"orderbook 429: {exc}"
        return row
    except Exception as exc:  # noqa: BLE001
        row.error = f"orderbook: {type(exc).__name__}: {exc}"
        return row
    row.yes_bid = book.yes_bid
    row.no_bid = book.no_bid
    row.yes_ask = book.yes_ask
    row.no_ask = book.no_ask
    row.yes_ask_size = book.yes_ask_size
    row.no_ask_size = book.no_ask_size
    if row.yes_ask is not None and row.no_ask is not None:
        row.sum_asks = row.yes_ask + row.no_ask
        row.curve_fee = pair_curve_fee(row.yes_ask, row.no_ask) if apply_curve_fee else 0.0
        row.lock_edge = lock_edge(row.yes_ask, row.no_ask, apply_curve_fee)
    return row


async def scan_series(
    series: list[str],
    apply_curve_fee: bool = True,
    http: httpx.AsyncClient | None = None,
    client: KalshiClient | None = None,
    now_ts: float | None = None,
    throttle_ms: int = 180,
) -> list[ScanRow]:
    own = http is None and client is None
    http_client = None
    if client is None:
        http_client = http or _new_http()
        client = KalshiClient(http_client, throttle_ms=throttle_ms)
    now_ts = time.time() if now_ts is None else now_ts
    rows: list[ScanRow] = []
    try:
        for ser in series:
            ser_u = ser.upper()
            try:
                markets = await client.list_open_markets(ser_u)
            except Exception as exc:  # noqa: BLE001
                rows.append(ScanRow(ticker="", series=ser_u, error=f"{type(exc).__name__}: {exc}"))
                continue
            if not markets:
                # weekend / closed: skip quietly
                continue
            for market in markets:
                rows.append(
                    await hydrate_row(client, market, ser_u, now_ts, apply_curve_fee)
                )
        return rows
    finally:
        if own and http_client is not None:
            await http_client.aclose()
