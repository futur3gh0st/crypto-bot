
"""Slug + window math for Polymarket crypto Up/Down markets.

Slug unix is floor(now / interval) * interval, UTC.
Example: btc-updown-15m-1786747500 -> Bitcoin Up or Down - August 14, 6:45PM-7:00PM ET.
"""

from __future__ import annotations

from dataclasses import dataclass, field

COIN_SPOT = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
    "hype": "HYPEUSDT",
    "ada": "ADAUSDT", "bch": "BCHUSDT", "near": "NEARUSDT",
    "ton": "TONUSDT", "zec": "ZECUSDT",
}

# minutes -> seconds
WINDOW_SECONDS = {5: 300, 15: 900}


def interval_seconds(minutes: int) -> int:
    if minutes in WINDOW_SECONDS:
        return WINDOW_SECONDS[minutes]
    if minutes <= 0:
        raise ValueError(f"window minutes must be positive, got {minutes}")
    return int(minutes) * 60


def window_start_unix(now_ts: float, minutes: int) -> int:
    """Floor now to the current window open. Never returns a future start."""
    interval = interval_seconds(minutes)
    ts = int(now_ts)
    start = (ts // interval) * interval
    if start > ts:
        raise ValueError("window start in the future (clock/math bug)")
    return start


def slug_for(coin: str, minutes: int, start_unix: int) -> str:
    return f"{coin.lower()}-updown-{minutes}m-{start_unix}"


def slug_window_end(slug: str) -> int | None:
    """When the market behind a slug stops trading.

    `btc-updown-15m-1788984900` carries the window *start* and its length, so
    the end is start + length. Used to tell a live position from one that
    settled hours ago — a settled lock is not open risk, and counting it as
    such is what silently drove the desk's spare capacity to zero.
    """
    parts = slug.rsplit("-", 2)
    if len(parts) < 3:
        return None
    span, start = parts[-2], parts[-1]
    if not span.endswith("m"):
        return None
    try:
        return int(start) + int(span[:-1]) * 60
    except (TypeError, ValueError):
        return None


def slug_is_settled(slug: str, now_ts: float) -> bool:
    end = slug_window_end(slug)
    return end is not None and now_ts >= end


def minutes_left(end_unix: int, now_ts: float) -> float:
    return (end_unix - now_ts) / 60.0


@dataclass(frozen=True)
class WindowRef:
    coin: str
    minutes: int
    which: str  # current | next
    start_unix: int
    end_unix: int
    slug: str

    def minutes_left(self, now_ts: float) -> float:
        return minutes_left(self.end_unix, now_ts)


def current_and_next(coin: str, minutes: int, now_ts: float) -> tuple[WindowRef, WindowRef]:
    interval = interval_seconds(minutes)
    cur = window_start_unix(now_ts, minutes)
    nxt = cur + interval
    return (
        WindowRef(coin.lower(), minutes, "current", cur, cur + interval, slug_for(coin, minutes, cur)),
        WindowRef(coin.lower(), minutes, "next", nxt, nxt + interval, slug_for(coin, minutes, nxt)),
    )


def parse_minutes_list(raw: str | None, default: list[int]) -> list[int]:
    if not raw:
        return list(default)
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip().lower().rstrip("m")
        if not part:
            continue
        n = int(part)
        if n <= 0:
            raise ValueError(f"bad window: {part}")
        if n not in out:
            out.append(n)
    return out or list(default)


def parse_coins(raw: str | None, default: list[str]) -> list[str]:
    if not raw:
        return [c.lower() for c in default]
    out: list[str] = []
    for part in raw.split(","):
        c = part.strip().lower()
        if not c:
            continue
        if c not in COIN_SPOT:
            raise ValueError(f"unknown coin {c}; known: {sorted(COIN_SPOT)}")
        if c not in out:
            out.append(c)
    return out or [c.lower() for c in default]


@dataclass
class ScanRow:
    coin: str
    minutes: int
    which: str
    slug: str
    start_unix: int
    end_unix: int
    minutes_left: float
    title: str | None = None
    up_bid: float | None = None
    up_ask: float | None = None
    down_bid: float | None = None
    down_ask: float | None = None
    sum_asks: float | None = None
    lock_edge: float | None = None
    spot: float | None = None
    open_px: float | None = None
    fair_up: float | None = None
    dislocation_bps: float | None = None
    up_ask_size: float | None = None
    down_ask_size: float | None = None
    up_token_id: str | None = None
    down_token_id: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def up_mid(self) -> float | None:
        if self.up_bid is not None and self.up_ask is not None:
            return (self.up_bid + self.up_ask) / 2.0
        return self.up_ask if self.up_ask is not None else self.up_bid
