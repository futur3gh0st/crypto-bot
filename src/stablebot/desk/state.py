"""Shared, in-memory desk state. One event loop mutates it; the renderer reads it."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Iterable

MAX_TAPE = 400
MAX_LOG = 300
MAX_CURVE = 720


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hhmmss(ts: datetime | None = None) -> str:
    return (ts or utcnow()).strftime("%H:%M:%S")


@dataclass
class TapeEvent:
    """One line on the fill tape."""

    ts: datetime
    sleeve: str
    kind: str          # fill | resolve | lock | skip | info
    symbol: str        # coin / slug / ticker
    side: str          # UP / DOWN / PAIR / —
    qty: float
    price: float | None
    pnl: float | None
    detail: str = ""

    @property
    def won(self) -> bool | None:
        if self.pnl is None:
            return None
        return self.pnl > 0


@dataclass
class BookRow:
    """One row of the live opportunity book."""

    sleeve: str
    symbol: str
    venue: str
    window: str
    expires_min: float | None
    bid: float | None
    ask: float | None
    fair: float | None
    edge: float | None       # signed edge in probability units
    state: str               # ARMED | WATCH | COLD | HELD | ERR
    detail: str = ""

    @property
    def sort_key(self) -> float:
        return -(self.edge if self.edge is not None else -9.0)


@dataclass
class PositionRow:
    sleeve: str
    symbol: str
    side: str
    qty: float
    entry: float
    cost: float
    expires_in: float | None   # seconds
    mark: float | None = None

    @property
    def unrealized(self) -> float | None:
        if self.mark is None:
            return None
        return self.qty * (self.mark - self.entry)


@dataclass
class SleeveStat:
    """Rolling scoreboard for one strategy sleeve. Drives the allocator."""

    name: str
    label: str
    enabled: bool = True
    status: str = "idle"            # idle | scanning | armed | cooling | error | off
    detail: str = ""
    allocation: float = 0.0         # dollars of the pot this sleeve may deploy
    alloc_frac: float = 0.0
    clip: float = 0.0               # per-trade dollar clip the allocator set
    trades: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    realized: float = 0.0           # net realized PnL since session start
    open_count: int = 0
    open_cost: float = 0.0
    cycles: int = 0
    errors: int = 0
    last_error: str = ""
    last_cycle_ts: datetime | None = None
    last_cycle_mono: float = 0.0    # monotonic clock, for elapsed maths
    next_cycle_in: float = 0.0
    bench_baseline: float = 0.0     # realized PnL when the sleeve last came back
    stale: bool = False             # enabled but not completing cycles
    stale_for: float = 0.0          # seconds of silence, when stale
    backoff: float = 0.0
    # rolling window used by the allocator
    recent: Deque[float] = field(default_factory=lambda: deque(maxlen=50))
    recent_entry: Deque[float] = field(default_factory=lambda: deque(maxlen=50))
    disabled_reason: str = ""
    probe_at: float = 0.0           # monotonic time when a disabled sleeve may re-probe
    params: dict[str, Any] = field(default_factory=dict)
    tuning: str = ""

    @property
    def decided(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float | None:
        if self.decided == 0:
            return None
        return self.wins / self.decided

    @property
    def expectancy(self) -> float | None:
        """Mean realized PnL per resolved trade over the rolling window."""
        if not self.recent:
            return None
        return sum(self.recent) / len(self.recent)

    @property
    def avg_entry(self) -> float | None:
        if not self.recent_entry:
            return None
        return sum(self.recent_entry) / len(self.recent_entry)

    @property
    def breakeven_hit_rate(self) -> float | None:
        """For a binary bought at p, you need to win more than p of the time."""
        return self.avg_entry

    def record_result(self, pnl: float, entry: float | None, scratched: bool = False) -> None:
        self.realized += pnl
        self.recent.append(pnl)
        if entry is not None:
            self.recent_entry.append(entry)
        if scratched:
            self.scratches += 1
        elif pnl > 0:
            self.wins += 1
        elif pnl < 0:
            self.losses += 1
        else:
            self.scratches += 1


@dataclass
class RiskState:
    mode: str = "RUNNING"           # RUNNING | THROTTLED | HALTED | PAUSED
    reason: str = ""
    day_start_equity: float = 0.0
    day_key: str = ""
    peak_equity: float = 0.0
    daily_stop_pct: float = 0.02
    max_drawdown_pct: float = 0.10
    throttle_mult: float = 1.0
    # Dollars of loss still allowed today before the stop trips, and how much of
    # that is already committed to open positions.
    budget_total: float = 0.0
    budget_remaining: float = 0.0
    capacity: float = 0.0

    @property
    def day_pnl(self) -> float:
        return 0.0


@dataclass
class DeskState:
    """Everything the dashboard draws and the autopilot reasons about."""

    mode: str = "PAPER"
    started_at: datetime = field(default_factory=utcnow)
    started_mono: float = field(default_factory=time.monotonic)
    starting_equity: float = 0.0
    equity: float = 0.0
    cash: float = 0.0
    open_cost: float = 0.0
    day_start_equity: float = 0.0
    peak_equity: float = 0.0
    paused: bool = False
    autopilot: bool = True
    quit: bool = False
    focus: str = "all"              # "all" or a sleeve name
    sleeves: dict[str, SleeveStat] = field(default_factory=dict)
    book: list[BookRow] = field(default_factory=list)
    positions: list[PositionRow] = field(default_factory=list)
    tape: Deque[TapeEvent] = field(default_factory=lambda: deque(maxlen=MAX_TAPE))
    log: Deque[tuple[datetime, str, str]] = field(default_factory=lambda: deque(maxlen=MAX_LOG))
    curve: Deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=MAX_CURVE))
    risk: RiskState = field(default_factory=RiskState)
    gates: dict[str, Any] = field(default_factory=dict)
    venues: dict[str, Any] = field(default_factory=dict)
    notice: str = ""
    notice_until: float = 0.0

    # ---- helpers -------------------------------------------------------

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started_mono

    @property
    def session_pnl(self) -> float:
        return self.equity - self.starting_equity

    @property
    def session_pnl_pct(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return self.session_pnl / self.starting_equity

    @property
    def day_pnl(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return self.equity - self.day_start_equity

    @property
    def day_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return self.day_pnl / self.day_start_equity

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.equity - self.peak_equity) / self.peak_equity

    @property
    def total_trades(self) -> int:
        return sum(s.trades for s in self.sleeves.values())

    @property
    def total_wins(self) -> int:
        return sum(s.wins for s in self.sleeves.values())

    @property
    def total_losses(self) -> int:
        return sum(s.losses for s in self.sleeves.values())

    @property
    def hit_rate(self) -> float | None:
        decided = self.total_wins + self.total_losses
        if decided == 0:
            return None
        return self.total_wins / decided

    @property
    def active_sleeves(self) -> list[SleeveStat]:
        return [s for s in self.sleeves.values() if s.enabled]

    def note(self, level: str, msg: str) -> None:
        self.log.append((utcnow(), level, msg))

    def flash(self, msg: str, seconds: float = 4.0) -> None:
        self.notice = msg
        self.notice_until = time.monotonic() + seconds

    def flash_active(self) -> str:
        if self.notice and time.monotonic() < self.notice_until:
            return self.notice
        return ""

    def push_tape(self, ev: TapeEvent) -> None:
        self.tape.append(ev)

    def set_book(self, sleeve: str, rows: Iterable[BookRow]) -> None:
        """Replace this sleeve's slice of the book, keep everyone else's."""
        keep = [r for r in self.book if r.sleeve != sleeve]
        keep.extend(rows)
        keep.sort(key=lambda r: r.sort_key)
        self.book = keep

    def set_positions(self, sleeve: str, rows: Iterable[PositionRow]) -> None:
        keep = [p for p in self.positions if p.sleeve != sleeve]
        keep.extend(rows)
        self.positions = keep

    def mark_equity(self, equity: float, cash: float, open_cost: float) -> None:
        self.equity = equity
        self.cash = cash
        self.open_cost = open_cost
        self.peak_equity = max(self.peak_equity, equity)
        self.curve.append((time.monotonic() - self.started_mono, equity))
