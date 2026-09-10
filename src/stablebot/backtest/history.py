from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx

from stablebot.config import AppConfig, data_dir
from stablebot.exchanges.base import USER_AGENT, split_concat_symbol
from stablebot.market.book import Quote, as_utc, quotes_from_mid

BINANCE_KLINES = (
    "https://api.binance.com/api/v3/klines",
    "https://data-api.binance.vision/api/v3/klines",
)
BYBIT_KLINES = (
    "https://api.bybit.com/v5/market/kline",
    "https://api.bytick.com/v5/market/kline",
)
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/{pid}/candles"


@dataclass(frozen=True)
class Bar:
    venue: str
    base: str
    quote: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def pair(self) -> str:
        return f"{self.base}/{self.quote}"


@dataclass
class HistoryBook:
    bars: list[Bar] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_time(self) -> dict[datetime, list[Bar]]:
        out: dict[datetime, list[Bar]] = {}
        for b in self.bars:
            key = b.open_time.replace(minute=0, second=0, microsecond=0)
            out.setdefault(key, []).append(b)
        return out


def parse_pair(pair: str) -> tuple[str, str]:
    a, b = pair.upper().replace("-", "/").split("/")
    return a, b


def venue_symbol(venue: str, base: str, quote: str) -> str:
    if venue == "coinbase":
        return f"{base}-{quote}"
    if venue == "kraken":
        return f"{base}{quote}"
    return f"{base}{quote}"


def _ms(dt: datetime) -> int:
    return int(as_utc(dt).timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


async def _get_json(client: httpx.AsyncClient, url: str, params: dict | None = None) -> object:
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


async def fetch_binance(
    client: httpx.AsyncClient, base: str, quote: str, start: datetime, end: datetime
) -> list[Bar]:
    symbol = f"{base}{quote}"
    params = {
        "symbol": symbol,
        "interval": "1h",
        "startTime": _ms(start),
        "endTime": _ms(end),
        "limit": 1000,
    }
    last_err: Exception | None = None
    data = None
    for url in BINANCE_KLINES:
        try:
            data = await _get_json(client, url, params)
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    if data is None:
        raise last_err or RuntimeError("binance klines failed")
    if isinstance(data, dict) and data.get("code"):
        raise RuntimeError(f"binance {symbol}: {data}")
    bars: list[Bar] = []
    for row in data:
        bars.append(
            Bar(
                venue="binance",
                base=base,
                quote=quote,
                open_time=_from_ms(int(row[0])),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
            )
        )
    return bars


async def fetch_bybit(
    client: httpx.AsyncClient, base: str, quote: str, start: datetime, end: datetime
) -> list[Bar]:
    params = {
        "category": "spot",
        "symbol": f"{base}{quote}",
        "interval": "60",
        "start": _ms(start),
        "end": _ms(end),
        "limit": 1000,
    }
    last_err = None
    payload = None
    for url in BYBIT_KLINES:
        try:
            payload = await _get_json(client, url, params)
            break
        except Exception as exc:
            last_err = exc
    if payload is None:
        raise last_err or RuntimeError("bybit kline failed")
    rows = ((payload.get("result") or {}).get("list")) or []
    bars: list[Bar] = []
    for row in rows:
        # Bybit returns newest-first: [start, open, high, low, close, volume, turnover]
        bars.append(
            Bar(
                venue="bybit",
                base=base,
                quote=quote,
                open_time=_from_ms(int(row[0])),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
            )
        )
    bars.sort(key=lambda b: b.open_time)
    return bars


async def fetch_kraken(
    client: httpx.AsyncClient, base: str, quote: str, start: datetime, end: datetime
) -> list[Bar]:
    params = {"pair": f"{base}{quote}", "interval": 60, "since": int(start.timestamp())}
    payload = await _get_json(client, KRAKEN_OHLC, params)
    if payload.get("error"):
        raise RuntimeError(f"kraken {base}{quote}: {payload['error']}")
    result = payload.get("result") or {}
    rows = []
    for key, val in result.items():
        if key == "last":
            continue
        rows = val
        break
    bars: list[Bar] = []
    end_ts = end.timestamp()
    for row in rows:
        ot = datetime.fromtimestamp(int(row[0]), tz=timezone.utc)
        if ot.timestamp() > end_ts:
            continue
        bars.append(
            Bar(
                venue="kraken",
                base=base,
                quote=quote,
                open_time=ot.replace(minute=0, second=0, microsecond=0),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
            )
        )
    return bars


async def fetch_coinbase(
    client: httpx.AsyncClient, base: str, quote: str, start: datetime, end: datetime
) -> list[Bar]:
    # Coinbase caps ~300 candles; page 7d chunks for 30d.
    from datetime import timedelta

    pid = f"{base}-{quote}"
    bars: list[Bar] = []
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + timedelta(days=10))
        params = {
            "granularity": 3600,
            "start": as_utc(cursor).isoformat(),
            "end": as_utc(chunk_end).isoformat(),
        }
        url = COINBASE_CANDLES.format(pid=pid)
        data = await _get_json(client, url, params)
        if isinstance(data, dict) and data.get("message"):
            raise RuntimeError(f"coinbase {pid}: {data['message']}")
        for row in data:
            # [time, low, high, open, close, volume]
            ot = datetime.fromtimestamp(int(row[0]), tz=timezone.utc)
            bars.append(
                Bar(
                    venue="coinbase",
                    base=base,
                    quote=quote,
                    open_time=ot.replace(minute=0, second=0, microsecond=0),
                    open=float(row[3]),
                    high=float(row[2]),
                    low=float(row[1]),
                    close=float(row[4]),
                )
            )
        cursor = chunk_end
    bars.sort(key=lambda b: b.open_time)
    # de-dupe
    seen: set[datetime] = set()
    uniq: list[Bar] = []
    for b in bars:
        if b.open_time in seen:
            continue
        seen.add(b.open_time)
        uniq.append(b)
    return uniq


_FETCHERS = {
    "binance": fetch_binance,
    "bybit": fetch_bybit,
    "kraken": fetch_kraken,
    "coinbase": fetch_coinbase,
}


async def fetch_history(
    cfg: AppConfig,
    start: datetime,
    end: datetime,
    venues: Iterable[str] | None = None,
) -> HistoryBook:
    book = HistoryBook()
    venues = list(venues or [v for v, c in cfg.venues.items() if c.enabled])
    pairs = cfg.backtest.pairs or [
        f"{a}/USDT" for a in cfg.watchlist.stables if a != "USDT"
    ]
    timeout = httpx.Timeout(20.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    ) as client:
        for venue in venues:
            fetcher = _FETCHERS.get(venue)
            if not fetcher:
                book.skipped.append(f"{venue}: no historical fetcher")
                continue
            for pair in pairs:
                try:
                    base, quote = parse_pair(pair)
                except ValueError:
                    book.skipped.append(f"{venue} {pair}: bad pair")
                    continue
                try:
                    bars = await fetcher(client, base, quote, start, end)
                except Exception as exc:  # noqa: BLE001
                    book.skipped.append(f"{venue} {pair}: {type(exc).__name__}: {exc}")
                    continue
                if not bars:
                    book.skipped.append(f"{venue} {pair}: no history")
                    continue
                book.bars.extend(bars)
                book.notes.append(f"{venue} {pair}: {len(bars)} hourly bars")
    return book


def bars_to_quotes(bars: list[Bar], cfg: AppConfig, use_open: bool = False) -> list[Quote]:
    half = cfg.strategy.hist_half_spread_bps
    out: list[Quote] = []
    for b in bars:
        px = b.open if use_open else b.close
        out.append(quotes_from_mid(b.venue, b.base, b.quote, px, b.open_time, half))
    return out


def load_fixture(path: Path) -> HistoryBook:
    raw = json.loads(path.read_text())
    book = HistoryBook(notes=list(raw.get("notes") or []), skipped=list(raw.get("skipped") or []))
    for row in raw.get("bars") or []:
        ot = datetime.fromisoformat(row["open_time"].replace("Z", "+00:00"))
        book.bars.append(
            Bar(
                venue=row["venue"],
                base=row["base"],
                quote=row["quote"],
                open_time=as_utc(ot).replace(minute=0, second=0, microsecond=0),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
            )
        )
    return book


def default_fixture_path() -> Path:
    from stablebot.config import find_project_root

    candidates = [
        find_project_root() / "tests" / "fixtures" / "klines_sample.json",
        Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "klines_sample.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def cache_path() -> Path:
    p = data_dir() / "backtests" / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


# keep helper imported for tests / symbol checks
_ = split_concat_symbol
