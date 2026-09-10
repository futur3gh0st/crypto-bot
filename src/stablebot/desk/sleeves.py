"""Sleeve adapters. Each wraps an existing paper engine and reports into DeskState.

The engines are untouched — a sleeve drives one, translates its output into
book rows / positions / tape events, and lets the allocator push sizing and
entry thresholds back down into it before each cycle.

Every sleeve here is PAPER. None of them can post an order.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from stablebot.config import AppConfig
from stablebot.desk.risk import RiskGovernor
from stablebot.desk.state import BookRow, DeskState, PositionRow, SleeveStat, TapeEvent, utcnow


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seed_stat_from_ledger(
    stat: SleeveStat, path: Path, fill_kind: str, key_field: str
) -> set[str]:
    """Rebuild a sleeve's scoreboard from its on-disk ledger.

    Realized PnL lives in a session file that survives a restart, but the trade
    counters used to be in-memory only. That split let the auto-tuner read
    "0 trades" for a sleeve that had already traded and lost, and keep walking
    its entry gate down toward the floor. Replaying the ledger closes the gap.

    Returns the resolve keys already accounted for, so a resolve that is still
    in flight when the desk restarts is not counted a second time.
    """
    seen: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return seen
    resolve_kind = f"{fill_kind}_resolve"
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        kind = rec.get("kind")
        if kind == fill_kind:
            stat.trades += 1
        elif kind == resolve_kind:
            key = str(rec.get(key_field) or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            stat.record_result(
                float(rec.get("pnl_after_fee") or 0.0),
                rec.get("entry_p"),
                scratched=bool(rec.get("scratched")),
            )
    return seen


class Sleeve:
    """One strategy running on its own clock inside the desk event loop."""

    name = "sleeve"
    label = "Sleeve"
    venue = "—"
    interval = 15.0
    max_backoff = 120.0
    # Sleeves sharing a pot_id draw on the same paper pot, so the desk counts
    # that stake once no matter how many sleeves are trading against it.
    pot_id = "default"

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.http: httpx.AsyncClient | None = None
        self._backoff = 0.0

    # ---- lifecycle -----------------------------------------------------

    def stat(self, state: DeskState) -> SleeveStat:
        s = state.sleeves.get(self.name)
        if s is None:
            s = SleeveStat(name=self.name, label=self.label)
            state.sleeves[self.name] = s
        return s

    async def setup(self, state: DeskState) -> None:  # pragma: no cover - trivial
        return None

    async def teardown(self) -> None:
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    async def cycle(self, state: DeskState) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # ---- pot reporting -------------------------------------------------
    #
    # pot_start is the stake this sleeve's pot began with; pot_pnl is what this
    # sleeve has added to it. The desk sums each distinct pot's start once and
    # then adds every sleeve's pnl, so two sleeves on one pot cannot double the
    # apparent bankroll.

    @property
    def pot_start(self) -> float:
        return 0.0

    @property
    def pot_pnl(self) -> float:
        return 0.0

    @property
    def starting_equity(self) -> float:
        return self.pot_start

    @property
    def equity(self) -> float:
        return self.pot_start + self.pot_pnl

    @property
    def open_cost(self) -> float:
        return 0.0

    @property
    def cash(self) -> float:
        return self.equity - self.open_cost

    # ---- driver --------------------------------------------------------

    async def run(self, state: DeskState) -> None:
        """Own clock, own error handling. Never lets one sleeve kill the desk."""
        stat = self.stat(state)
        try:
            await self.setup(state)
        except Exception as exc:  # noqa: BLE001
            stat.status = "error"
            stat.last_error = f"setup: {type(exc).__name__}: {exc}"
            stat.errors += 1
            state.note("error", f"{self.label} setup failed: {exc}")
        while not state.quit:
            if not stat.enabled:
                stat.status = "off"
                stat.detail = stat.disabled_reason or "disabled"
                await self._nap(state, stat, min(10.0, self.interval))
                continue
            started = time.monotonic()
            try:
                stat.status = "scanning"
                await self.cycle(state)
                stat.cycles += 1
                stat.last_cycle_ts = utcnow()
                stat.last_cycle_mono = time.monotonic()
                stat.last_error = ""
                self._backoff = 0.0
                stat.backoff = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                stat.errors += 1
                stat.last_error = f"{type(exc).__name__}: {exc}"
                stat.status = "error"
                self._backoff = min(self.max_backoff, max(self.interval, self._backoff * 2 or self.interval))
                stat.backoff = self._backoff
                state.note("error", f"{self.label}: {stat.last_error}")
            elapsed = time.monotonic() - started
            wait = max(1.0, (self._backoff or self.interval) - elapsed)
            await self._nap(state, stat, wait)

    async def _nap(self, state: DeskState, stat: SleeveStat, seconds: float) -> None:
        """Sleep in slices so the countdown ticks and quit is responsive."""
        end = time.monotonic() + seconds
        while not state.quit:
            left = end - time.monotonic()
            stat.next_cycle_in = max(0.0, left)
            if left <= 0:
                return
            await asyncio.sleep(min(0.5, left))


# ---------------------------------------------------------------------------
# spot-lag: Binance move -> Polymarket Up/Down catch-up (paper, directional)
# ---------------------------------------------------------------------------


class SpotLagSleeve(Sleeve):
    name = "spot_lag"
    label = "Spot-Lag"
    venue = "poly"
    interval = 15.0
    pot_id = "spot_lag"

    def __init__(
        self,
        cfg: AppConfig,
        coins: list[str] | None = None,
        windows: list[int] | None = None,
        balance: float = 1000.0,
        params: Any = None,
        signal: Any = None,
        legacy: bool = False,
    ):
        super().__init__(cfg)
        from stablebot.desk.signal import SignalCfg
        from stablebot.desk.spot_lag_vol import VolSpotLagPaper
        from stablebot.poly.spot_lag import DEFAULT_COINS, DEFAULT_WINDOWS, SpotLagPaper, SpotLagParams

        self.coins = coins or list(DEFAULT_COINS)
        self.windows = windows or list(DEFAULT_WINDOWS)
        self.legacy = legacy
        self.signal = signal or SignalCfg()
        if legacy:
            self.engine = SpotLagPaper(
                params=params or SpotLagParams(), starting_balance=balance
            )
        else:
            self.engine = VolSpotLagPaper(
                params=params or SpotLagParams(),
                starting_balance=balance,
                signal=self.signal,
            )
        self._base_max_concurrent = self.engine.params.max_concurrent
        self._seen_resolves: set[str] = set()

    @property
    def pot_start(self) -> float:
        return float(self.engine.sess.get("starting_equity") or 0.0)

    @property
    def pot_pnl(self) -> float:
        return self.engine.equity - self.pot_start

    @property
    def cash(self) -> float:
        return self.engine.cash

    @property
    def open_cost(self) -> float:
        return float(self.engine.sess.get("open_cost") or 0.0)

    def _replay_history(self, state: DeskState, stat: SleeveStat) -> None:
        from stablebot.poly.spot_lag import ledger_path

        self._seen_resolves = seed_stat_from_ledger(
            stat, ledger_path(), self.name, "slug"
        )
        # The tuner should remember yesterday; the bench should not. Start the
        # loss baseline at whatever is already lost, so a restart gives the
        # sleeve its full allowance again rather than benching it on history.
        stat.bench_baseline = stat.realized
        if stat.trades:
            state.note(
                "info",
                f"{self.label}: replayed {stat.trades} prior fills / "
                f"{stat.decided} resolved (realized {stat.realized:+,.2f})",
            )

    async def setup(self, state: DeskState) -> None:
        from stablebot.poly.client import _new_http

        self.http = _new_http()
        stat = self.stat(state)
        p = self.engine.params
        self._replay_history(state, stat)
        if self.legacy:
            stat.params.setdefault("min_edge", p.min_edge)
            stat.params.setdefault("threshold", p.threshold)
            stat.detail = f"legacy model / {len(self.coins)} coins"
            return
        stat.params.setdefault("min_edge", self.signal.min_edge)
        stat.params.setdefault("z_entry", self.signal.z_entry)
        state.note("info", f"{self.label}: seeding volatility from history…")
        sigmas = await self.engine.seed_vol(self.http, self.coins)
        got = {k: v for k, v in sigmas.items() if v}
        if got:
            worst = min(got.values())
            best = max(got.values())
            state.note(
                "info",
                f"{self.label}: vol seeded for {len(got)} coins "
                f"(sigma1m {worst*100:.4f}%–{best*100:.4f}%)",
            )
        stat.detail = f"{len(self.coins)} coins / {self.windows}m / {self.signal.describe()}"

    async def cycle(self, state: DeskState) -> None:
        stat = self.stat(state)
        p = self.engine.params

        # allocator + governor push sizing and entry bar down before the scan
        cap = RiskGovernor.clip_cap(state)
        if stat.clip > 0:
            p.fixed_clip = min(stat.clip, cap) if cap > 0 else 0.0
        if self.legacy:
            p.min_edge = float(stat.params.get("min_edge", p.min_edge))
            p.threshold = float(stat.params.get("threshold", p.threshold))
        else:
            self.signal.min_edge = float(stat.params.get("min_edge", self.signal.min_edge))
            self.signal.z_entry = float(stat.params.get("z_entry", self.signal.z_entry))
        trading = RiskGovernor.may_trade(state)
        # max_concurrent 0 still resolves open positions but arms nothing new
        p.max_concurrent = self._base_max_concurrent if trading else 0

        events, notes = await self.engine.step(
            self.coins, self.windows, http=self.http
        )

        rows: list[BookRow] = []
        for n in notes:
            if n.action == "FILL":
                st = "ARMED"
            elif n.action == "no_signal":
                st = "COLD"
            elif n.action == "skip" and "already open" in n.detail:
                st = "HELD"
            elif n.action == "skip" and n.detail.startswith("klines"):
                st = "ERR"
            else:
                st = "WATCH"
            detail = n.detail
            if n.move_pct is not None:
                detail = f"move {n.move_pct*100:+.3f}%  {n.detail}"
            rows.append(
                BookRow(
                    sleeve=self.name,
                    symbol=n.coin.upper(),
                    venue="poly",
                    window=f"{self.windows[0]}m" if self.windows else "—",
                    expires_min=(n.remaining / 60.0) if n.remaining is not None else None,
                    bid=None,
                    ask=n.live_ask,
                    fair=n.fair_side,
                    edge=n.edge,
                    state=st,
                    detail=detail,
                )
            )
        state.set_book(self.name, rows)

        positions = [
            PositionRow(
                sleeve=self.name,
                symbol=f"{pos.coin.upper()} {pos.direction}",
                side=pos.direction,
                qty=pos.shares,
                entry=pos.entry_p,
                cost=pos.cost,
                expires_in=max(0.0, pos.window_end - time.time()),
                mark=None,
            )
            for pos in self.engine.open.values()
        ]
        state.set_positions(self.name, positions)
        stat.open_count = len(positions)
        stat.open_cost = sum(pos.cost for pos in positions)

        for e in events:
            kind = e.get("kind")
            if kind == "spot_lag":
                stat.trades += 1
                state.push_tape(
                    TapeEvent(
                        ts=_now(),
                        sleeve=self.label,
                        kind="fill",
                        symbol=str(e.get("coin", "")).upper(),
                        side=str(e.get("direction", "")),
                        qty=float(e.get("shares") or 0.0),
                        price=float(e.get("fill_p") or 0.0),
                        pnl=None,
                        detail=(
                            f"edge {float(e.get('edge') or 0):+.4f} "
                            f"({e.get('entry_source')}) fee {float(e.get('fee') or 0):.3f}"
                        ),
                    )
                )
            elif kind == "spot_lag_resolve":
                slug = str(e.get("slug") or "")
                if slug and slug in self._seen_resolves:
                    continue
                self._seen_resolves.add(slug)
                pnl = float(e.get("pnl_after_fee") or 0.0)
                scratched = bool(e.get("scratched"))
                stat.record_result(pnl, e.get("entry_p"), scratched=scratched)
                flag = "SCRATCH" if scratched else ("WIN" if e.get("won") else "LOSS")
                state.push_tape(
                    TapeEvent(
                        ts=_now(),
                        sleeve=self.label,
                        kind="resolve",
                        symbol=str(e.get("coin", "")).upper(),
                        side=str(e.get("direction", "")),
                        qty=float(e.get("shares") or 0.0),
                        price=float(e.get("entry_p") or 0.0),
                        pnl=pnl,
                        detail=f"{flag} settled {e.get('resolved')}",
                    )
                )

        armed = sum(1 for r in rows if r.state == "ARMED")
        watching = sum(1 for r in rows if r.state == "WATCH")
        stat.status = "armed" if armed else ("scanning" if trading else "cooling")
        if not trading:
            stat.detail = "risk gate closed — resolving only"
        elif self.legacy:
            stat.detail = f"{armed} armed / {watching} watching / {len(rows)} scanned"
        else:
            gates = self.engine.gates
            state.gates[self.name] = gates
            binding = gates.binding()
            bits = f"{armed} armed / {len(rows)} scanned"
            if binding:
                bits += f" / blocked mostly by {binding[0]} ({binding[1]})"
            stat.detail = bits


# ---------------------------------------------------------------------------
# pair-complete: buy both sides under $1 (paper lock)
# ---------------------------------------------------------------------------


class PairCompleteSleeve(Sleeve):
    name = "poly_lock"
    label = "Poly Lock"
    venue = "poly"
    interval = 15.0
    pot_id = "poly_shared"

    def __init__(
        self,
        cfg: AppConfig,
        coins: list[str] | None = None,
        windows: list[int] | None = None,
    ):
        super().__init__(cfg)
        from stablebot.poly.paper import PolyLedger, PolyPaper

        self.coins = coins or [c.lower() for c in cfg.poly.coins]
        self.windows = windows or list(cfg.poly.windows)
        self.ledger = PolyLedger()
        self.engine = PolyPaper(cfg.poly, self.ledger, fade=False)
        self._pot_start = 1000.0
        self._realized = 0.0

    @property
    def pot_start(self) -> float:
        return self._pot_start

    @property
    def pot_pnl(self) -> float:
        return self._realized

    @property
    def open_cost(self) -> float:
        return sum(
            inv.up * inv.up_cost + inv.down * inv.down_cost
            for inv in self.engine.inv.values()
        )

    async def setup(self, state: DeskState) -> None:
        from stablebot.kalshi.session import recompute_shared_session

        sess = recompute_shared_session()
        self._pot_start = float(sess.get("starting_equity") or 1000.0)
        # replayed ledger PnL is history, not this session's result
        self._realized = 0.0
        stat = self.stat(state)
        stat.detail = f"{len(self.coins)} coins / {self.windows} windows"
        stat.params.setdefault("min_lock", self.cfg.poly.min_lock)

    async def cycle(self, state: DeskState) -> None:
        from stablebot.poly.scan import run_scan

        stat = self.stat(state)
        self.cfg.poly.min_lock = float(stat.params.get("min_lock", self.cfg.poly.min_lock))
        if stat.clip > 0:
            # shares such that a $1 pair costs about the clip
            self.cfg.poly.paper_shares = max(1.0, round(stat.clip))

        rows = await run_scan(self.cfg, self.coins, self.windows)

        book: list[BookRow] = []
        for r in rows:
            if r.error:
                st = "ERR"
            elif r.slug in self.engine.completed:
                st = "HELD"
            elif r.lock_edge is not None and r.lock_edge > self.cfg.poly.min_lock:
                st = "ARMED"
            elif r.lock_edge is not None and r.lock_edge > 0:
                st = "WATCH"
            else:
                st = "COLD"
            book.append(
                BookRow(
                    sleeve=self.name,
                    symbol=r.coin.upper(),
                    venue="poly",
                    window=f"{r.minutes}m {r.which[:4]}",
                    expires_min=r.minutes_left,
                    bid=r.up_bid,
                    ask=r.sum_asks,
                    fair=r.fair_up,
                    edge=r.lock_edge,
                    state=st,
                    detail=r.error or (
                        f"up {r.up_ask:.3f} / dn {r.down_ask:.3f}"
                        if r.up_ask is not None and r.down_ask is not None
                        else "no two-sided ask"
                    ),
                )
            )
        state.set_book(self.name, book)

        positions = []
        for slug, inv in self.engine.inv.items():
            paired = min(inv.up, inv.down)
            if paired <= 0:
                continue
            cost = paired * (inv.up_cost + inv.down_cost)
            positions.append(
                PositionRow(
                    sleeve=self.name,
                    symbol=slug.split("-updown-")[0].upper() or slug,
                    side="PAIR",
                    qty=paired,
                    entry=inv.up_cost + inv.down_cost,
                    cost=cost,
                    expires_in=None,
                    mark=1.0,
                )
            )
        state.set_positions(self.name, positions)
        stat.open_count = len(positions)
        stat.open_cost = sum(p.cost for p in positions)

        if RiskGovernor.may_trade(state):
            fills = self.engine.step(rows, _now())
        else:
            fills = []
            stat.status = "cooling"
            stat.detail = "risk gate closed"

        for f in fills:
            if f.skipped:
                continue
            stat.trades += 1
            # a completed lock books its edge immediately; it settles at $1
            stat.record_result(f.pnl, None)
            self._realized += f.pnl
            state.push_tape(
                TapeEvent(
                    ts=_now(),
                    sleeve=self.label,
                    kind="lock",
                    symbol=f.slug.split("-updown-")[0].upper() or f.slug,
                    side="PAIR",
                    qty=f.shares,
                    price=None,
                    pnl=f.pnl,
                    detail=f.reason,
                )
            )

        armed = sum(1 for r in book if r.state == "ARMED")
        if RiskGovernor.may_trade(state):
            stat.status = "armed" if armed else "scanning"
            best = max((r.edge for r in book if r.edge is not None), default=None)
            stat.detail = (
                f"{armed} lockable / {len(book)} books"
                + (f" / best {best:+.4f}" if best is not None else "")
            )


# ---------------------------------------------------------------------------
# kalshi: same lock, different venue
# ---------------------------------------------------------------------------


class KalshiSleeve(Sleeve):
    name = "kalshi_lock"
    label = "Kalshi Lock"
    venue = "kalshi"
    interval = 20.0
    pot_id = "poly_shared"   # shares the $1000 pot with the Polymarket locks

    def __init__(self, cfg: AppConfig, series: list[str] | None = None):
        super().__init__(cfg)
        from stablebot.kalshi.paper import KalshiLedger, KalshiPaper

        self.series = series or [s.upper() for s in cfg.kalshi.series]
        self.ledger = KalshiLedger()
        self.engine = KalshiPaper(cfg.kalshi, self.ledger, update_session=True)
        self.client: Any = None
        self._pot_start = 1000.0
        self._realized = 0.0

    @property
    def pot_start(self) -> float:
        return self._pot_start

    @property
    def pot_pnl(self) -> float:
        return self._realized

    async def setup(self, state: DeskState) -> None:
        from stablebot.exchanges.base import USER_AGENT
        from stablebot.kalshi.client import KalshiClient
        from stablebot.kalshi.session import recompute_shared_session

        sess = recompute_shared_session()
        self._pot_start = float(sess.get("starting_equity") or 1000.0)
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
        self.client = KalshiClient(self.http, throttle_ms=self.cfg.kalshi.throttle_ms)
        stat = self.stat(state)
        stat.detail = f"{len(self.series)} series"
        stat.params.setdefault("min_lock", self.cfg.kalshi.min_lock)

    async def cycle(self, state: DeskState) -> None:
        from stablebot.kalshi.scan import run_scan

        stat = self.stat(state)
        self.cfg.kalshi.min_lock = float(stat.params.get("min_lock", self.cfg.kalshi.min_lock))
        if stat.clip > 0:
            self.cfg.kalshi.paper_shares = max(1.0, round(stat.clip))

        rows = await run_scan(self.cfg, self.series, client=self.client)

        book: list[BookRow] = []
        for r in rows:
            if r.error:
                st = "ERR"
            elif r.ticker in self.engine.completed:
                st = "HELD"
            elif r.lock_edge is not None and r.lock_edge > self.cfg.kalshi.min_lock:
                st = "ARMED"
            elif r.lock_edge is not None and r.lock_edge > 0:
                st = "WATCH"
            else:
                st = "COLD"
            book.append(
                BookRow(
                    sleeve=self.name,
                    symbol=r.series.replace("KX", "").replace("15M", ""),
                    venue="kalshi",
                    window="15m",
                    expires_min=r.minutes_left,
                    bid=r.yes_bid,
                    ask=r.sum_asks,
                    fair=None,
                    edge=r.lock_edge,
                    state=st,
                    detail=r.error or (
                        f"yes {r.yes_ask:.3f} / no {r.no_ask:.3f}"
                        if r.yes_ask is not None and r.no_ask is not None
                        else "no two-sided ask"
                    ),
                )
            )
        state.set_book(self.name, book)

        if RiskGovernor.may_trade(state):
            fills = self.engine.step(rows, _now())
        else:
            fills = []
            stat.status = "cooling"
            stat.detail = "risk gate closed"

        for f in fills:
            if f.skipped:
                continue
            stat.trades += 1
            stat.record_result(f.pnl, None)
            self._realized += f.pnl
            state.push_tape(
                TapeEvent(
                    ts=_now(),
                    sleeve=self.label,
                    kind="lock",
                    symbol=f.ticker,
                    side="PAIR",
                    qty=f.shares,
                    price=None,
                    pnl=f.pnl,
                    detail=f.reason,
                )
            )

        armed = sum(1 for r in book if r.state == "ARMED")
        if RiskGovernor.may_trade(state):
            stat.status = "armed" if armed else "scanning"
            best = max((r.edge for r in book if r.edge is not None), default=None)
            stat.detail = (
                f"{armed} lockable / {len(book)} books"
                + (f" / best {best:+.4f}" if best is not None else "")
            )


# ---------------------------------------------------------------------------
# kalshi spot-lag: the directional sleeve on a venue that settles in USD
# ---------------------------------------------------------------------------


class KalshiLagSleeve(Sleeve):
    name = "kalshi_lag"
    label = "Kalshi Lag"
    venue = "kalshi"
    interval = 20.0
    pot_id = "kalshi_lag"

    def __init__(
        self,
        cfg: AppConfig,
        coins: list[str] | None = None,
        balance: float = 1000.0,
        params: Any = None,
    ):
        super().__init__(cfg)
        from stablebot.desk.kalshi_lag import COIN_SERIES, KalshiLagPaper, LagParams

        self.coins = coins or list(COIN_SERIES)
        self.engine = KalshiLagPaper(
            params=params or LagParams(), starting_balance=balance
        )
        self.client: Any = None
        self._base_max_concurrent = self.engine.params.max_concurrent
        self._seen_resolves: set[str] = set()

    @property
    def pot_start(self) -> float:
        return float(self.engine.sess.get("starting_equity") or 0.0)

    @property
    def pot_pnl(self) -> float:
        return self.engine.equity - self.pot_start

    @property
    def cash(self) -> float:
        return self.engine.cash

    @property
    def open_cost(self) -> float:
        return float(self.engine.sess.get("open_cost") or 0.0)

    def _replay_history(self, state: DeskState, stat: SleeveStat) -> None:
        from stablebot.desk.kalshi_lag import ledger_path

        self._seen_resolves = seed_stat_from_ledger(
            stat, ledger_path(), self.name, "ticker"
        )
        # The tuner should remember yesterday; the bench should not. Start the
        # loss baseline at whatever is already lost, so a restart gives the
        # sleeve its full allowance again rather than benching it on history.
        stat.bench_baseline = stat.realized
        if stat.trades:
            state.note(
                "info",
                f"{self.label}: replayed {stat.trades} prior fills / "
                f"{stat.decided} resolved (realized {stat.realized:+,.2f})",
            )

    async def setup(self, state: DeskState) -> None:
        from stablebot.exchanges.base import USER_AGENT
        from stablebot.kalshi.client import KalshiClient

        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
        self.client = KalshiClient(self.http, throttle_ms=self.cfg.kalshi.throttle_ms)
        stat = self.stat(state)
        sig = self.engine.params.signal
        stat.params.setdefault("min_edge", sig.min_edge)
        stat.params.setdefault("z_entry", sig.z_entry)
        self._replay_history(state, stat)
        state.note("info", f"{self.label}: seeding volatility from history…")
        sigmas = await self.engine.seed_vol(self.http, self.coins)
        got = {k: v for k, v in sigmas.items() if v}
        if got:
            state.note(
                "info",
                f"{self.label}: vol seeded for {len(got)} coins "
                f"(sigma1m {min(got.values())*100:.4f}%–{max(got.values())*100:.4f}%)",
            )
        stat.detail = f"{len(self.coins)} coins / 15m / {sig.describe()}"

    async def cycle(self, state: DeskState) -> None:
        stat = self.stat(state)
        p = self.engine.params
        sig = p.signal
        cap = RiskGovernor.clip_cap(state)
        if stat.clip > 0:
            p.fixed_clip = min(stat.clip, cap) if cap > 0 else 0.0
        sig.min_edge = float(stat.params.get("min_edge", sig.min_edge))
        sig.z_entry = float(stat.params.get("z_entry", sig.z_entry))
        trading = RiskGovernor.may_trade(state)
        p.max_concurrent = self._base_max_concurrent if trading else 0

        events, notes = await self.engine.step(self.client, self.http, self.coins)

        rows: list[BookRow] = []
        for n in notes:
            if n.action == "FILL":
                st = "ARMED"
            elif n.action == "no_signal":
                st = "COLD"
            elif n.action == "skip" and "already open" in n.detail:
                st = "HELD"
            elif n.action == "skip" and n.detail.startswith(("klines", "markets", "orderbook")):
                st = "ERR"
            else:
                st = "WATCH"
            ask = None
            if n.side == "no" and n.no_ask is not None:
                ask = n.no_ask
            elif n.yes_ask is not None:
                ask = n.yes_ask
            rows.append(
                BookRow(
                    sleeve=self.name,
                    symbol=n.coin.upper(),
                    venue="kalshi",
                    window="15m",
                    expires_min=n.minutes_left,
                    bid=None,
                    ask=ask,
                    fair=n.fair,
                    edge=n.edge,
                    state=st,
                    detail=(
                        f"z={n.z:+.2f} strike {n.strike:,.2f} {n.detail}"
                        if n.z is not None and n.strike is not None
                        else n.detail
                    ),
                )
            )
        state.set_book(self.name, rows)

        positions = [
            PositionRow(
                sleeve=self.name,
                symbol=f"{pos.coin.upper()} {pos.side.upper()}",
                side=pos.side.upper(),
                qty=pos.shares,
                entry=pos.entry_p,
                cost=pos.cost,
                expires_in=max(0.0, pos.close_ts - time.time()),
                mark=None,
            )
            for pos in self.engine.open.values()
        ]
        state.set_positions(self.name, positions)
        stat.open_count = len(positions)
        stat.open_cost = sum(pos.cost for pos in positions)

        for e in events:
            kind = e.get("kind")
            if kind == "kalshi_lag":
                stat.trades += 1
                state.push_tape(
                    TapeEvent(
                        ts=_now(),
                        sleeve=self.label,
                        kind="fill",
                        symbol=str(e.get("coin", "")).upper(),
                        side=str(e.get("side", "")).upper(),
                        qty=float(e.get("shares") or 0.0),
                        price=float(e.get("entry_p") or 0.0),
                        pnl=None,
                        detail=(
                            f"edge {float(e.get('edge') or 0):+.4f} "
                            f"fair {float(e.get('fair') or 0):.3f} "
                            f"ref {e.get('spot_source')}"
                        ),
                    )
                )
            elif kind == "kalshi_lag_resolve":
                ticker = str(e.get("ticker") or "")
                if ticker and ticker in self._seen_resolves:
                    continue
                self._seen_resolves.add(ticker)
                pnl = float(e.get("pnl_after_fee") or 0.0)
                scratched = bool(e.get("scratched"))
                stat.record_result(pnl, e.get("entry_p"), scratched=scratched)
                flag = "SCRATCH" if scratched else ("WIN" if e.get("won") else "LOSS")
                state.push_tape(
                    TapeEvent(
                        ts=_now(),
                        sleeve=self.label,
                        kind="resolve",
                        symbol=str(e.get("coin", "")).upper(),
                        side=str(e.get("side", "")).upper(),
                        qty=float(e.get("shares") or 0.0),
                        price=float(e.get("entry_p") or 0.0),
                        pnl=pnl,
                        detail=f"{flag} settled {e.get('resolved')} via {e.get('settle_source')}",
                    )
                )

        armed = sum(1 for r in rows if r.state == "ARMED")
        state.gates[self.name] = self.engine.gates
        if not trading:
            stat.status = "cooling"
            stat.detail = "risk gate closed — resolving only"
        else:
            binding = self.engine.gates.binding()
            stat.status = "armed" if armed else "scanning"
            bits = f"{armed} armed / {len(rows)} scanned"
            if binding:
                bits += f" / blocked mostly by {binding[0]} ({binding[1]})"
            stat.detail = bits
