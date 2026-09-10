"""Reference price built the way the settlement index is built, not from one venue.

Kalshi settles its crypto contracts on CF Benchmarks. CF's real-time indices are
a composite of USD spot books — Coinbase, Kraken, Bitstamp, Gemini, itBit and
LMAX — and notably do **not** include Binance. Reading Binance USDT and pricing
a contract that settles on a USD composite carries a small persistent basis:
measured live on BTC it was +0.9 bp, which is about 6% of a 15-minute sigma, and
it widens whenever USDT itself moves.

So this module takes the median of the CF constituent venues that answer. The
median is deliberate — it is what makes one venue printing a bad tick, or one
API returning stale data, unable to move the reference.

This is a *methodology match*, not the index itself. CF's published rate uses
volume weighting over an averaging window and their own outlier rules; a median
of last-trade prices is a close approximation, not a reproduction. Where an
actual CF Benchmarks API key is configured the real index is used instead — see
`CFBenchmarksSource`, which is wired but unverified, because the public API
returns 401 without credentials and no endpoint could be confirmed.

Public endpoints only. No keys required for the composite path.
"""

from __future__ import annotations

import asyncio
import os
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

MIN_SOURCES = 2

# CF Benchmarks BRTI-style constituents, per coin, by venue.
VENUE_PAIRS: dict[str, dict[str, str]] = {
    "btc": {"coinbase": "BTC-USD", "kraken": "XBTUSD", "bitstamp": "btcusd", "gemini": "btcusd"},
    "eth": {"coinbase": "ETH-USD", "kraken": "ETHUSD", "bitstamp": "ethusd", "gemini": "ethusd"},
    "sol": {"coinbase": "SOL-USD", "kraken": "SOLUSD", "bitstamp": "solusd", "gemini": "solusd"},
    "xrp": {"coinbase": "XRP-USD", "kraken": "XRPUSD", "bitstamp": "xrpusd", "gemini": "xrpusd"},
    "doge": {"coinbase": "DOGE-USD", "kraken": "XDGUSD", "bitstamp": "dogeusd", "gemini": "dogeusd"},
    "bnb": {"coinbase": "BNB-USD", "kraken": "BNBUSD", "bitstamp": "bnbusd", "gemini": "bnbusd"},
    # Probed venue by venue; a pair that 404s would silently thin the composite
    # and widen the dispersion floor, so only the ones that answer are listed.
    "ada": {"coinbase": "ADA-USD", "kraken": "ADAUSD", "bitstamp": "adausd"},
    "bch": {"coinbase": "BCH-USD", "kraken": "BCHUSD", "bitstamp": "bchusd", "gemini": "bchusd"},
    "near": {"coinbase": "NEAR-USD", "kraken": "NEARUSD", "bitstamp": "nearusd"},
    "ton": {"coinbase": "TON-USD", "kraken": "TONUSD", "bitstamp": "tonusd"},
    "zec": {"coinbase": "ZEC-USD", "kraken": "ZECUSD", "bitstamp": "zecusd", "gemini": "zecusd"},
}

BINANCE_FALLBACK = {
    "btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
    "xrp": "XRPUSDT", "doge": "DOGEUSDT", "bnb": "BNBUSDT",
    "ada": "ADAUSDT", "bch": "BCHUSDT", "near": "NEARUSDT",
    "ton": "TONUSDT", "zec": "ZECUSDT",
}
VISION = "https://data-api.binance.vision"


@dataclass
class RefQuote:
    """A reference price and an honest account of where it came from."""

    price: float | None
    method: str                       # composite | binance_fallback | cf_benchmarks | none
    sources: dict[str, float] = field(default_factory=dict)
    dispersion_bp: float | None = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.price is not None and self.price > 0

    def label(self) -> str:
        if not self.ok:
            return f"no reference ({self.note})"
        n = len(self.sources)
        disp = f", spread {self.dispersion_bp:.1f}bp" if self.dispersion_bp is not None else ""
        return f"{self.method} of {n}{disp}"


def _median(values: list[float]) -> float:
    return statistics.median(values)


def _dispersion_bp(values: list[float], mid: float) -> float | None:
    if len(values) < 2 or mid <= 0:
        return None
    return (max(values) - min(values)) / mid * 10_000.0


# ---------------------------------------------------------------------------
# per-venue readers — live last trade
# ---------------------------------------------------------------------------


async def _coinbase_spot(http: httpx.AsyncClient, sym: str) -> float:
    r = await http.get(f"https://api.exchange.coinbase.com/products/{sym}/ticker")
    r.raise_for_status()
    return float(r.json()["price"])


async def _kraken_spot(http: httpx.AsyncClient, sym: str) -> float:
    r = await http.get("https://api.kraken.com/0/public/Ticker", params={"pair": sym})
    r.raise_for_status()
    result = r.json()["result"]
    return float(next(iter(result.values()))["c"][0])


async def _bitstamp_spot(http: httpx.AsyncClient, sym: str) -> float:
    r = await http.get(f"https://www.bitstamp.net/api/v2/ticker/{sym}/")
    r.raise_for_status()
    return float(r.json()["last"])


async def _gemini_spot(http: httpx.AsyncClient, sym: str) -> float:
    r = await http.get(f"https://api.gemini.com/v1/pubticker/{sym}")
    r.raise_for_status()
    return float(r.json()["last"])


SPOT_READERS: dict[str, Callable] = {
    "coinbase": _coinbase_spot,
    "kraken": _kraken_spot,
    "bitstamp": _bitstamp_spot,
    "gemini": _gemini_spot,
}


# ---------------------------------------------------------------------------
# per-venue readers — the close of one specific minute
# ---------------------------------------------------------------------------


async def _coinbase_close(http: httpx.AsyncClient, sym: str, minute_start: int) -> float | None:
    r = await http.get(
        f"https://api.exchange.coinbase.com/products/{sym}/candles",
        params={"granularity": 60, "start": minute_start, "end": minute_start + 60},
    )
    r.raise_for_status()
    rows = r.json()
    for row in rows:
        if int(row[0]) == minute_start:
            return float(row[4])
    return float(rows[0][4]) if rows else None


async def _kraken_close(http: httpx.AsyncClient, sym: str, minute_start: int) -> float | None:
    r = await http.get(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": sym, "interval": 1, "since": minute_start - 120},
    )
    r.raise_for_status()
    result = r.json().get("result", {})
    for key, rows in result.items():
        if key == "last" or not isinstance(rows, list):
            continue
        for row in rows:
            if int(row[0]) == minute_start:
                return float(row[4])
    return None


async def _bitstamp_close(http: httpx.AsyncClient, sym: str, minute_start: int) -> float | None:
    r = await http.get(
        f"https://www.bitstamp.net/api/v2/ohlc/{sym}/",
        params={"step": 60, "limit": 20, "start": minute_start - 120},
    )
    r.raise_for_status()
    for row in r.json().get("data", {}).get("ohlc", []):
        if int(row["timestamp"]) == minute_start:
            return float(row["close"])
    return None


async def _gemini_close(http: httpx.AsyncClient, sym: str, minute_start: int) -> float | None:
    r = await http.get(f"https://api.gemini.com/v2/candles/{sym}/1m")
    r.raise_for_status()
    for row in r.json():
        if int(row[0] // 1000) == minute_start:
            return float(row[4])
    return None


CLOSE_READERS: dict[str, Callable] = {
    "coinbase": _coinbase_close,
    "kraken": _kraken_close,
    "bitstamp": _bitstamp_close,
    "gemini": _gemini_close,
}


# ---------------------------------------------------------------------------
# CF Benchmarks — real index, only with credentials
# ---------------------------------------------------------------------------


class CFBenchmarksSource:
    """The actual settlement index, when an API key is configured.

    UNVERIFIED. The public API answered 401 on /assets and "Unknown id" for every
    index ticker tried without credentials, so the exact index ids and response
    shape could not be confirmed here. With a key set, this is attempted first
    and any failure falls through to the composite rather than blocking a cycle.
    Set CF_BENCHMARKS_API_KEY and CF_BENCHMARKS_INDEX_<COIN> to use it.
    """

    BASE = "https://www.cfbenchmarks.com/api/v1"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("CF_BENCHMARKS_API_KEY")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def index_id(self, coin: str) -> str | None:
        return os.environ.get(f"CF_BENCHMARKS_INDEX_{coin.upper()}")

    async def spot(self, http: httpx.AsyncClient, coin: str) -> float | None:
        if not self.enabled:
            return None
        index = self.index_id(coin)
        if not index:
            return None
        try:
            r = await http.get(
                f"{self.BASE}/values",
                params={"id": index},
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            r.raise_for_status()
            payload = r.json().get("payload") or []
            if isinstance(payload, list) and payload:
                latest = payload[-1]
                value = latest.get("value") if isinstance(latest, dict) else None
                return float(value) if value is not None else None
        except Exception:  # noqa: BLE001 - never let the index block a cycle
            return None
        return None


# ---------------------------------------------------------------------------
# the provider
# ---------------------------------------------------------------------------


class ReferencePrice:
    """Composite reference price with a Binance fallback of last resort."""

    def __init__(
        self,
        min_sources: int = MIN_SOURCES,
        allow_binance_fallback: bool = True,
        cf: CFBenchmarksSource | None = None,
    ):
        self.min_sources = max(1, min_sources)
        self.allow_binance_fallback = allow_binance_fallback
        self.cf = cf or CFBenchmarksSource()
        self.last: dict[str, RefQuote] = {}

    async def _gather(
        self,
        http: httpx.AsyncClient,
        coin: str,
        readers: dict[str, Callable],
        *extra: Any,
    ) -> dict[str, float]:
        pairs = VENUE_PAIRS.get(coin, {})
        names = [v for v in pairs if v in readers]
        if not names:
            return {}
        results = await asyncio.gather(
            *(readers[v](http, pairs[v], *extra) for v in names),
            return_exceptions=True,
        )
        out: dict[str, float] = {}
        for venue, res in zip(names, results):
            if isinstance(res, BaseException) or res is None:
                continue
            try:
                px = float(res)
            except (TypeError, ValueError):
                continue
            if px > 0:
                out[venue] = px
        return out

    async def _binance_spot(self, http: httpx.AsyncClient, coin: str) -> float | None:
        sym = BINANCE_FALLBACK.get(coin)
        if not sym:
            return None
        try:
            r = await http.get(f"{VISION}/api/v3/ticker/price", params={"symbol": sym})
            r.raise_for_status()
            px = float(r.json()["price"])
            return px if px > 0 else None
        except Exception:  # noqa: BLE001
            return None

    async def _binance_close(
        self, http: httpx.AsyncClient, coin: str, minute_start: int
    ) -> float | None:
        sym = BINANCE_FALLBACK.get(coin)
        if not sym:
            return None
        try:
            r = await http.get(
                f"{VISION}/api/v3/klines",
                params={"symbol": sym, "interval": "1m",
                        "startTime": minute_start * 1000, "limit": 1},
            )
            r.raise_for_status()
            rows = r.json()
            if isinstance(rows, list) and rows and int(rows[0][0]) == minute_start * 1000:
                px = float(rows[0][4])
                return px if px > 0 else None
        except Exception:  # noqa: BLE001
            return None
        return None

    def _build(self, sources: dict[str, float], fallback: float | None, what: str) -> RefQuote:
        if len(sources) >= self.min_sources:
            vals = list(sources.values())
            mid = _median(vals)
            return RefQuote(mid, "composite", sources, _dispersion_bp(vals, mid))
        if fallback is not None and self.allow_binance_fallback:
            return RefQuote(
                fallback,
                "binance_fallback",
                dict(sources),
                None,
                note=f"only {len(sources)} CF-constituent venue(s) answered for {what}; "
                     "using Binance USDT, which carries a basis to the settlement index",
            )
        return RefQuote(None, "none", dict(sources), None,
                        note=f"{len(sources)} source(s) for {what}")

    async def spot(self, http: httpx.AsyncClient, coin: str) -> RefQuote:
        cf_px = await self.cf.spot(http, coin)
        if cf_px:
            q = RefQuote(cf_px, "cf_benchmarks", {"cf_benchmarks": cf_px})
            self.last[coin] = q
            return q
        sources = await self._gather(http, coin, SPOT_READERS)
        fallback = None
        if len(sources) < self.min_sources:
            fallback = await self._binance_spot(http, coin)
        q = self._build(sources, fallback, "spot")
        self.last[coin] = q
        return q

    async def close_at(self, http: httpx.AsyncClient, coin: str, ts: float) -> RefQuote:
        """Composite close of the minute that ends at `ts`."""
        minute_start = int(ts) - 60
        sources = await self._gather(http, coin, CLOSE_READERS, minute_start)
        fallback = None
        if len(sources) < self.min_sources:
            fallback = await self._binance_close(http, coin, minute_start)
        return self._build(sources, fallback, "settlement close")
