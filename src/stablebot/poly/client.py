"""Public Polymarket + Binance vision fetches. Paper research only — no orders."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from stablebot.exchanges.base import USER_AGENT
from stablebot.poly.fair import crude_fair_up, dislocation_bps
from stablebot.poly.markets import COIN_SPOT, ScanRow, WindowRef, current_and_next
from stablebot.poly.strategy import lock_edge

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
VISION = "https://data-api.binance.vision"


def _new_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(12.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


def _as_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _token_map(market: dict[str, Any]) -> dict[str, str]:
    outcomes = [str(x).strip() for x in _as_list(market.get("outcomes"))]
    ids = [str(x).strip() for x in _as_list(market.get("clobTokenIds"))]
    out: dict[str, str] = {}
    for name, tid in zip(outcomes, ids):
        key = name.lower()
        if key in {"up", "down"} and tid:
            out[key] = tid
    return out


async def fetch_event(http: httpx.AsyncClient, slug: str) -> dict[str, Any] | None:
    resp = await http.get(f"{GAMMA}/events", params={"slug": slug})
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


async def fetch_clob_price(http: httpx.AsyncClient, token_id: str, side: str) -> float | None:
    """side=buy -> bid, side=sell -> ask. Do not use /book (wing junk)."""
    resp = await http.get(f"{CLOB}/price", params={"token_id": token_id, "side": side})
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("price") if isinstance(data, dict) else None
    if raw is None:
        return None
    px = float(raw)
    return px if px > 0 else None


async def fetch_spot(http: httpx.AsyncClient, symbol: str) -> float | None:
    resp = await http.get(f"{VISION}/api/v3/ticker/price", params={"symbol": symbol})
    resp.raise_for_status()
    data = resp.json()
    px = float(data["price"])
    return px if px > 0 else None


async def fetch_window_open(
    http: httpx.AsyncClient,
    symbol: str,
    minutes: int,
    start_unix: int,
    now_ts: float,
) -> float | None:
    """Open print of the kline that starts at the window. No lookahead.

    Refuses a bar whose openTime is not exactly start_unix, and refuses
    if the window has not opened yet (next window).
    """
    if start_unix > now_ts:
        return None
    resp = await http.get(
        f"{VISION}/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": f"{int(minutes)}m",
            "startTime": int(start_unix) * 1000,
            "limit": 1,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or not data:
        return None
    row = data[0]
    open_time = int(row[0])
    if open_time != int(start_unix) * 1000:
        return None
    open_px = float(row[1])
    return open_px if open_px > 0 else None


async def _safe(coro, label: str) -> tuple[Any, str | None]:
    try:
        return await coro, None
    except Exception as exc:  # noqa: BLE001
        return None, f"{label}: {type(exc).__name__}: {exc}"


async def hydrate_row(
    http: httpx.AsyncClient,
    ref: WindowRef,
    now_ts: float,
    taker_fee_bps: float,
    fair_scale: float,
    spots: dict[str, float | None],
) -> ScanRow:
    row = ScanRow(
        coin=ref.coin,
        minutes=ref.minutes,
        which=ref.which,
        slug=ref.slug,
        start_unix=ref.start_unix,
        end_unix=ref.end_unix,
        minutes_left=ref.minutes_left(now_ts),
        spot=spots.get(ref.coin),
    )
    event, err = await _safe(fetch_event(http, ref.slug), "gamma")
    if err:
        row.error = err
        return row
    if not event:
        row.error = "no event"
        return row
    row.title = event.get("title")
    markets = event.get("markets") or []
    if not markets:
        row.error = "no markets"
        return row
    tokens = _token_map(markets[0])
    if "up" not in tokens or "down" not in tokens:
        row.error = "missing Up/Down token ids"
        return row

    up_id, down_id = tokens["up"], tokens["down"]
    row.up_token_id = up_id
    row.down_token_id = down_id
    (row.up_bid, e1) = await _safe(fetch_clob_price(http, up_id, "buy"), "up_bid")
    (row.up_ask, e2) = await _safe(fetch_clob_price(http, up_id, "sell"), "up_ask")
    (row.down_bid, e3) = await _safe(fetch_clob_price(http, down_id, "buy"), "down_bid")
    (row.down_ask, e4) = await _safe(fetch_clob_price(http, down_id, "sell"), "down_ask")
    errs = [e for e in (e1, e2, e3, e4) if e]
    if errs:
        row.error = "; ".join(errs)

    if row.up_ask is not None and row.down_ask is not None:
        row.sum_asks = row.up_ask + row.down_ask
        row.lock_edge = lock_edge(row.up_ask, row.down_ask, taker_fee_bps)

    symbol = COIN_SPOT.get(ref.coin)
    if symbol and ref.which == "current":
        open_px, oerr = await _safe(
            fetch_window_open(http, symbol, ref.minutes, ref.start_unix, now_ts),
            "open",
        )
        if oerr:
            row.notes.append(oerr)
        row.open_px = open_px
        if row.open_px and row.spot:
            row.fair_up = crude_fair_up(row.spot, row.open_px, scale=fair_scale)
            mid = row.up_mid
            if mid is not None:
                row.dislocation_bps = dislocation_bps(mid, row.fair_up)
    elif ref.which == "next":
        row.notes.append("no open print yet (next window)")
    return row


async def scan_windows(
    coins: list[str],
    windows: list[int],
    now_ts: float,
    taker_fee_bps: float = 0.0,
    fair_scale: float = 25.0,
    http: httpx.AsyncClient | None = None,
) -> list[ScanRow]:
    own = http is None
    client = http or _new_http()
    try:
        spots: dict[str, float | None] = {}
        for coin in coins:
            symbol = COIN_SPOT.get(coin)
            if not symbol:
                spots[coin] = None
                continue
            px, err = await _safe(fetch_spot(client, symbol), f"spot:{coin}")
            spots[coin] = px
            if err:
                spots[coin] = None
        rows: list[ScanRow] = []
        for coin in coins:
            for minutes in windows:
                cur, nxt = current_and_next(coin, minutes, now_ts)
                for ref in (cur, nxt):
                    rows.append(
                        await hydrate_row(
                            client, ref, now_ts, taker_fee_bps, fair_scale, spots
                        )
                    )
        return rows
    finally:
        if own:
            await client.aclose()

@dataclass(frozen=True)
class BestLevel:
    """Best bid/ask only. Wing levels on /book are ignored."""

    bid: float | None = None
    bid_size: float | None = None
    ask: float | None = None
    ask_size: float | None = None


def _level_px_sz(level: Any) -> tuple[float | None, float | None]:
    if level is None:
        return None, None
    if isinstance(level, dict):
        raw_px = level.get("price")
        raw_sz = level.get("size")
    else:
        raw_px = getattr(level, "price", None)
        raw_sz = getattr(level, "size", None)
    if raw_px is None or raw_sz is None:
        return None, None
    try:
        px = float(raw_px)
        sz = float(raw_sz)
    except (TypeError, ValueError):
        return None, None
    if px <= 0 or sz <= 0:
        return None, None
    return px, sz


def parse_best_book(data: Any) -> BestLevel:
    """Best bid = max bid price; best ask = min ask price. Ignore other levels."""
    if not isinstance(data, dict):
        return BestLevel()
    best_bid = None
    best_bid_sz = None
    for level in data.get("bids") or []:
        px, sz = _level_px_sz(level)
        if px is None or sz is None:
            continue
        if best_bid is None or px > best_bid:
            best_bid, best_bid_sz = px, sz
    best_ask = None
    best_ask_sz = None
    for level in data.get("asks") or []:
        px, sz = _level_px_sz(level)
        if px is None or sz is None:
            continue
        if best_ask is None or px < best_ask:
            best_ask, best_ask_sz = px, sz
    return BestLevel(best_bid, best_bid_sz, best_ask, best_ask_sz)


async def fetch_best_book(http: httpx.AsyncClient, token_id: str) -> BestLevel:
    resp = await http.get(f"{CLOB}/book", params={"token_id": token_id})
    resp.raise_for_status()
    return parse_best_book(resp.json())


def fetch_best_book_sync(token_id: str, timeout: float = 12.0) -> BestLevel:
    with httpx.Client(
        timeout=httpx.Timeout(timeout),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    ) as http:
        resp = http.get(f"{CLOB}/book", params={"token_id": token_id})
        resp.raise_for_status()
        return parse_best_book(resp.json())
