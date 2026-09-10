"""Live PAPER spot_lag sleeve — Binance move → Polymarket Up/Down catch-up entry.

Paper only. Never posts CLOB orders. Separate session/ledger from pair-complete.
Params match hunt-winner realistic spot_lag: catchup=0.70, slip=0.02, min_edge=0.04,
threshold=0.003, window=5m.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console

from stablebot.config import AppConfig, data_dir
from stablebot.exchanges.base import USER_AGENT
from stablebot.poly.client import (
    _new_http,
    _safe,
    fetch_clob_price,
    fetch_event,
    fetch_spot,
    fetch_window_open,
)
from stablebot.poly.fair import crude_fair_up
from stablebot.poly.markets import (
    COIN_SPOT,
    current_and_next,
    parse_coins,
    parse_minutes_list,
)
from stablebot.poly.replay import poly_taker_fee

console = Console(width=200)

DEFAULT_COINS = ["btc", "eth", "sol", "xrp", "doge", "bnb"]
DEFAULT_WINDOWS = [5]
DEFAULT_CATCHUP = 0.70
DEFAULT_SLIP = 0.02
DEFAULT_MIN_EDGE = 0.04
DEFAULT_THRESHOLD = 0.003
DEFAULT_BALANCE = 1000.0
RISK_FRAC = 0.05
FIXED_CLIP = 15.0
MAX_CONCURRENT = 4
MAX_ELAPSED_SEC = 90.0  # live: allow ~60–90s into window
MIN_REMAINING_SEC = 90.0
FAIR_SCALE = 25.0
VISION = "https://data-api.binance.vision"
BINANCE = "https://api.binance.com"


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _iso(ts: datetime | None = None) -> str:
    return (ts or datetime.now(timezone.utc)).isoformat()


def session_path() -> Path:
    return data_dir() / "spot_lag_session.json"


def ledger_path() -> Path:
    return data_dir() / "spot_lag_ledger.jsonl"


# ---------------------------------------------------------------------------
# Session + ledger
# ---------------------------------------------------------------------------


def load_session(path: Path | None = None, starting: float = DEFAULT_BALANCE) -> dict[str, Any]:
    path = path or session_path()
    if not path.exists():
        out = {
            "starting_equity": starting,
            "equity": starting,
            "cash": starting,
            "open_cost": 0.0,
            "fills": 0,
            "resolves": 0,
            "last_ts": None,
            "note": "spot_lag paper session; separate from poly_session.json",
            "live": False,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("starting_equity", starting)
    raw.setdefault("equity", float(raw.get("starting_equity") or starting))
    raw.setdefault("cash", float(raw.get("equity") or starting))
    raw.setdefault("open_cost", 0.0)
    raw.setdefault("fills", 0)
    raw.setdefault("resolves", 0)
    raw.setdefault("live", False)
    return raw


def save_session(sess: dict[str, Any], path: Path | None = None) -> None:
    path = path or session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    sess = dict(sess)
    sess["live"] = False
    sess["last_ts"] = _iso()
    # equity = cash + locked cost of open paper positions
    sess["equity"] = float(sess.get("cash") or 0.0) + float(sess.get("open_cost") or 0.0)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sess, indent=2), encoding="utf-8")
    tmp.replace(path)


class SpotLagLedger:
    def __init__(self, path: Path | None = None):
        self.path = path or ledger_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, rec: dict[str, Any]) -> None:
        rec = dict(rec)
        rec.setdefault("ts", _iso())
        rec.setdefault("live", False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out


# ---------------------------------------------------------------------------
# Market data helpers
# ---------------------------------------------------------------------------


async def fetch_1m_klines(
    http: httpx.AsyncClient,
    symbol: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Recent 1m bars: open_time_ms, open, high, low, close, close_time_ms."""
    errors: list[str] = []
    for base in (VISION, BINANCE):
        try:
            resp = await http.get(
                f"{base}/api/v3/klines",
                params={"symbol": symbol, "interval": "1m", "limit": int(limit)},
            )
            resp.raise_for_status()
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                errors.append(f"{base}: empty")
                continue
            out: list[dict[str, Any]] = []
            for row in rows:
                out.append(
                    {
                        "open_time_ms": int(row[0]),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "close_time_ms": int(row[6]),
                    }
                )
            return out
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{base}: {type(exc).__name__}: {exc}")
    raise RuntimeError(f"1m klines {symbol} failed: {'; '.join(errors)}")


async def fetch_window_close(
    http: httpx.AsyncClient,
    symbol: str,
    minutes: int,
    start_unix: int,
    end_unix: int,
    now_ts: float,
) -> float | None:
    """Close of the last 1m bar that ends at/before window end. No lookahead past now."""
    if now_ts < end_unix:
        return None
    # Prefer the 1m bar whose open is end-60s (last minute of window).
    last_open_ms = (end_unix - 60) * 1000
    for base in (VISION, BINANCE):
        try:
            resp = await http.get(
                f"{base}/api/v3/klines",
                params={
                    "symbol": symbol,
                    "interval": "1m",
                    "startTime": last_open_ms,
                    "limit": 1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                row = data[0]
                if int(row[0]) == last_open_ms:
                    px = float(row[4])
                    return px if px > 0 else None
        except Exception:  # noqa: BLE001
            continue
    # Fallback: minutes-interval bar open at start
    try:
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
        if isinstance(data, list) and data and int(data[0][0]) == int(start_unix) * 1000:
            px = float(data[0][4])
            return px if px > 0 else None
    except Exception:  # noqa: BLE001
        pass
    return None


def entry_model(fair_side: float, catchup: float, slip: float) -> float:
    return clamp(0.50 + catchup * (fair_side - 0.50) + slip, 0.51, 0.92)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class OpenPos:
    coin: str
    symbol: str
    slug: str
    minutes: int
    window_start: int
    window_end: int
    direction: str  # UP | DOWN
    side: str  # up | down
    move_pct: float
    fair_up: float
    fair_side: float
    entry_p: float
    entry_source: str
    live_ask: float | None
    shares: float
    cost: float
    signal_ts: str
    fill_ts: str
    token_id: str | None = None

    def key(self) -> tuple[str, int]:
        return (self.coin, self.window_start)


@dataclass
class SpotLagParams:
    catchup: float = DEFAULT_CATCHUP
    slip: float = DEFAULT_SLIP
    min_edge: float = DEFAULT_MIN_EDGE
    threshold: float = DEFAULT_THRESHOLD
    max_concurrent: int = MAX_CONCURRENT
    risk_frac: float = RISK_FRAC
    fixed_clip: float = FIXED_CLIP
    max_elapsed_sec: float = MAX_ELAPSED_SEC
    min_remaining_sec: float = MIN_REMAINING_SEC
    fair_scale: float = FAIR_SCALE


@dataclass
class CycleNote:
    coin: str
    move_pct: float | None = None
    direction: str | None = None
    slug: str | None = None
    elapsed: float | None = None
    remaining: float | None = None
    fair_side: float | None = None
    entry_model: float | None = None
    live_ask: float | None = None
    fill_p: float | None = None
    edge: float | None = None
    action: str = "idle"
    detail: str = ""


class SpotLagPaper:
    """Stateful paper engine: detect → fill → hold → resolve."""

    def __init__(
        self,
        params: SpotLagParams | None = None,
        starting_balance: float = DEFAULT_BALANCE,
        ledger: SpotLagLedger | None = None,
    ):
        self.params = params or SpotLagParams()
        self.ledger = ledger or SpotLagLedger()
        self.sess = load_session(starting=starting_balance)
        # If caller passed a fresh balance and session was missing/new, already set.
        # If session exists, keep it; --balance only seeds missing session.
        self.open: dict[tuple[str, int], OpenPos] = {}
        self._replay_open()

    def _replay_open(self) -> None:
        """Rebuild open positions from ledger (spot_lag without matching resolve)."""
        opened: dict[str, dict[str, Any]] = {}
        resolved: set[str] = set()
        for rec in self.ledger.load():
            kind = rec.get("kind")
            slug = str(rec.get("slug") or "")
            if not slug:
                continue
            if kind == "spot_lag":
                opened[slug] = rec
            elif kind == "spot_lag_resolve":
                resolved.add(slug)
        for slug, rec in opened.items():
            if slug in resolved:
                continue
            coin = str(rec.get("coin") or "")
            w_start = int(rec.get("window_start") or 0)
            if not coin or not w_start:
                continue
            pos = OpenPos(
                coin=coin,
                symbol=str(rec.get("symbol") or COIN_SPOT.get(coin, "")),
                slug=slug,
                minutes=int(rec.get("minutes") or 5),
                window_start=w_start,
                window_end=int(rec.get("window_end") or (w_start + 300)),
                direction=str(rec.get("direction") or "UP"),
                side=str(rec.get("side") or "up"),
                move_pct=float(rec.get("move_pct") or 0.0),
                fair_up=float(rec.get("fair_up") or 0.5),
                fair_side=float(rec.get("fair_side") or 0.5),
                entry_p=float(rec.get("entry_p") or rec.get("fill_p") or 0.55),
                entry_source=str(rec.get("entry_source") or "catchup"),
                live_ask=rec.get("live_ask"),
                shares=float(rec.get("shares") or 0.0),
                cost=float(rec.get("cost") or 0.0),
                signal_ts=str(rec.get("signal_ts") or rec.get("ts") or ""),
                fill_ts=str(rec.get("ts") or ""),
                token_id=rec.get("token_id"),
            )
            self.open[pos.key()] = pos
        # Reconcile open_cost from open positions
        self.sess["open_cost"] = sum(p.cost for p in self.open.values())
        save_session(self.sess)

    @property
    def equity(self) -> float:
        return float(self.sess.get("cash") or 0.0) + float(self.sess.get("open_cost") or 0.0)

    @property
    def cash(self) -> float:
        return float(self.sess.get("cash") or 0.0)

    async def step(
        self,
        coins: list[str],
        windows: list[int],
        now_ts: float | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> tuple[list[dict[str, Any]], list[CycleNote]]:
        own = http is None
        client = http or _new_http()
        now_ts = time.time() if now_ts is None else now_ts
        events: list[dict[str, Any]] = []
        notes: list[CycleNote] = []
        try:
            events.extend(await self._resolve_due(client, now_ts))
            for coin in coins:
                for minutes in windows:
                    note, fill = await self._maybe_enter(client, coin, minutes, now_ts)
                    notes.append(note)
                    if fill is not None:
                        events.append(fill)
        finally:
            if own:
                await client.aclose()
        save_session(self.sess)
        return events, notes

    async def _resolve_due(
        self, http: httpx.AsyncClient, now_ts: float
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        due = [k for k, p in self.open.items() if p.window_end <= now_ts]
        for key in sorted(due, key=lambda k: self.open[k].window_end):
            pos = self.open.pop(key)
            symbol = pos.symbol or COIN_SPOT.get(pos.coin, "")
            open_px, oerr = await _safe(
                fetch_window_open(http, symbol, pos.minutes, pos.window_start, now_ts),
                "open",
            )
            close_px, cerr = await _safe(
                fetch_window_close(
                    http, symbol, pos.minutes, pos.window_start, pos.window_end, now_ts
                ),
                "close",
            )
            scratched = False
            if open_px is None or close_px is None:
                # Refund cost; no fee if we cannot resolve honestly
                self.sess["cash"] = self.cash + pos.cost
                self.sess["open_cost"] = max(0.0, float(self.sess.get("open_cost") or 0.0) - pos.cost)
                rec = {
                    "kind": "spot_lag_resolve",
                    "slug": pos.slug,
                    "coin": pos.coin,
                    "direction": pos.direction,
                    "resolved": "UNKNOWN",
                    "won": False,
                    "scratched": True,
                    "shares": pos.shares,
                    "entry_p": pos.entry_p,
                    "pnl": 0.0,
                    "fee": 0.0,
                    "pnl_after_fee": 0.0,
                    "equity": self.equity,
                    "error": "; ".join(x for x in (oerr, cerr) if x) or "missing open/close",
                    "live": False,
                    "note": "could not resolve; refunded cost; paper only",
                }
                self.ledger.append(rec)
                self.sess["resolves"] = int(self.sess.get("resolves") or 0) + 1
                out.append(rec)
                continue

            if close_px > open_px:
                resolved = "UP"
            elif close_px < open_px:
                resolved = "DOWN"
            else:
                resolved = "FLAT"

            # Fee already charged at fill time into cash; do not double-charge.
            fee = pos.shares * poly_taker_fee(pos.entry_p)
            if resolved == "FLAT":
                scratched = True
                won = False
                pnl = 0.0
                self.sess["cash"] = self.cash + pos.cost  # refund stake; fee stays
            else:
                won = resolved == pos.direction
                if won:
                    pnl = pos.shares * (1.0 - pos.entry_p)
                    self.sess["cash"] = self.cash + pos.shares * 1.0
                else:
                    pnl = pos.shares * (-pos.entry_p)
                    # cost already out of cash; payout 0; fee already charged

            self.sess["open_cost"] = max(0.0, float(self.sess.get("open_cost") or 0.0) - pos.cost)
            pnl_after = pnl - fee
            rec = {
                "kind": "spot_lag_resolve",
                "slug": pos.slug,
                "coin": pos.coin,
                "symbol": symbol,
                "minutes": pos.minutes,
                "window_start": pos.window_start,
                "window_end": pos.window_end,
                "direction": pos.direction,
                "side": pos.side,
                "open_px": open_px,
                "close_px": close_px,
                "resolved": resolved,
                "won": won and not scratched,
                "scratched": scratched,
                "shares": pos.shares,
                "entry_p": pos.entry_p,
                "entry_source": pos.entry_source,
                "cost": pos.cost,
                "pnl": pnl,
                "fee": fee,
                "pnl_after_fee": pnl_after,
                "equity": self.equity,
                "live": False,
                "note": "paper resolve from Binance window open/close; no live orders",
            }
            self.ledger.append(rec)
            self.sess["resolves"] = int(self.sess.get("resolves") or 0) + 1
            out.append(rec)
        return out

    async def _maybe_enter(
        self,
        http: httpx.AsyncClient,
        coin: str,
        minutes: int,
        now_ts: float,
    ) -> tuple[CycleNote, dict[str, Any] | None]:
        p = self.params
        note = CycleNote(coin=coin)
        symbol = COIN_SPOT.get(coin)
        if not symbol:
            note.action = "skip"
            note.detail = "unknown symbol"
            return note, None

        # 1) Detect |Δ| ≥ threshold over last closed 1m bar
        try:
            bars = await fetch_1m_klines(http, symbol, limit=4)
        except Exception as exc:  # noqa: BLE001
            note.action = "skip"
            note.detail = f"klines: {exc}"
            return note, None
        if len(bars) < 2:
            note.action = "skip"
            note.detail = "need ≥2 1m bars"
            return note, None

        # Use last two *closed* bars when possible (exclude forming bar if close_time in future)
        closed = [b for b in bars if b["close_time_ms"] / 1000.0 <= now_ts + 1.0]
        if len(closed) < 2:
            closed = bars
        prev, cur = closed[-2], closed[-1]
        if prev["close"] <= 0:
            note.action = "skip"
            note.detail = "bad prev close"
            return note, None
        move = (cur["close"] - prev["close"]) / prev["close"]
        note.move_pct = move
        if abs(move) < p.threshold:
            note.action = "no_signal"
            note.detail = f"|move|={abs(move)*100:.3f}% < {p.threshold*100:.2f}%"
            return note, None

        direction = "UP" if move > 0 else "DOWN"
        side = "up" if direction == "UP" else "down"
        note.direction = direction

        cur_ref, _nxt = current_and_next(coin, minutes, now_ts)
        note.slug = cur_ref.slug
        elapsed = now_ts - cur_ref.start_unix
        remaining = cur_ref.end_unix - now_ts
        note.elapsed = elapsed
        note.remaining = remaining

        # Also require signal bar to fall inside this window
        sig_bar_open = cur["open_time_ms"] / 1000.0
        if not (cur_ref.start_unix <= sig_bar_open < cur_ref.end_unix):
            note.action = "skip"
            note.detail = "signal bar outside current window"
            return note, None

        if not (0 < elapsed <= p.max_elapsed_sec):
            note.action = "skip"
            note.detail = f"elapsed={elapsed:.0f}s not in (0,{p.max_elapsed_sec:.0f}]"
            return note, None
        if remaining < p.min_remaining_sec:
            note.action = "skip"
            note.detail = f"remaining={remaining:.0f}s < {p.min_remaining_sec:.0f}s"
            return note, None

        key = (coin, cur_ref.start_unix)
        if key in self.open:
            note.action = "skip"
            note.detail = "already open this window"
            return note, None
        if any(pos.coin == coin and pos.window_start == cur_ref.start_unix for pos in self.open.values()):
            note.action = "skip"
            note.detail = "≤1 trade/coin/window"
            return note, None
        if len(self.open) >= p.max_concurrent:
            note.action = "skip"
            note.detail = f"max concurrent {p.max_concurrent}"
            return note, None

        # Window open + spot for fair
        open_px, oerr = await _safe(
            fetch_window_open(http, symbol, minutes, cur_ref.start_unix, now_ts),
            "open",
        )
        spot, serr = await _safe(fetch_spot(http, symbol), "spot")
        if open_px is None or spot is None or open_px <= 0 or spot <= 0:
            note.action = "skip"
            note.detail = f"need open+spot ({oerr or ''} {serr or ''})".strip()
            return note, None

        try:
            fair_up = crude_fair_up(spot, open_px, scale=p.fair_scale)
        except ValueError as exc:
            note.action = "skip"
            note.detail = str(exc)
            return note, None
        fair_side = fair_up if direction == "UP" else (1.0 - fair_up)
        note.fair_side = fair_side
        model = entry_model(fair_side, p.catchup, p.slip)
        note.entry_model = model

        # Live ask for favored side
        live_ask: float | None = None
        token_id: str | None = None
        event, eerr = await _safe(fetch_event(http, cur_ref.slug), "gamma")
        if event and isinstance(event, dict):
            markets = event.get("markets") or []
            if markets:
                from stablebot.poly.client import _token_map

                tokens = _token_map(markets[0])
                token_id = tokens.get(side)
                if token_id:
                    ask, aerr = await _safe(
                        fetch_clob_price(http, token_id, "sell"), "ask"
                    )
                    if ask is not None and 0.0 < ask < 1.0:
                        live_ask = float(ask)
                    elif aerr:
                        note.detail = aerr
        elif eerr:
            note.detail = eerr
        note.live_ask = live_ask

        if live_ask is not None:
            fill_p = max(live_ask, model)
            entry_source = "live_ask" if live_ask >= model else "catchup_floor"
        else:
            fill_p = model
            entry_source = "catchup"
        note.fill_p = fill_p
        edge = fair_side - fill_p
        note.edge = edge
        if edge < p.min_edge:
            note.action = "skip"
            note.detail = f"edge={edge:.4f} < min_edge={p.min_edge:.4f}"
            return note, None

        equity = self.equity
        clip = min(p.risk_frac * equity, p.fixed_clip)
        if clip < 1.0 or fill_p <= 0.0 or fill_p >= 1.0:
            note.action = "skip"
            note.detail = f"bad clip/fill clip={clip:.2f} fill_p={fill_p:.3f}"
            return note, None
        shares = clip / fill_p
        cost = shares * fill_p
        if cost > self.cash + 1e-9:
            note.action = "skip"
            note.detail = f"insufficient cash {self.cash:.2f} < {cost:.2f}"
            return note, None

        # Paper fill
        self.sess["cash"] = self.cash - cost
        self.sess["open_cost"] = float(self.sess.get("open_cost") or 0.0) + cost
        self.sess["fills"] = int(self.sess.get("fills") or 0) + 1

        pos = OpenPos(
            coin=coin,
            symbol=symbol,
            slug=cur_ref.slug,
            minutes=minutes,
            window_start=cur_ref.start_unix,
            window_end=cur_ref.end_unix,
            direction=direction,
            side=side,
            move_pct=move,
            fair_up=fair_up,
            fair_side=fair_side,
            entry_p=fill_p,
            entry_source=entry_source,
            live_ask=live_ask,
            shares=shares,
            cost=cost,
            signal_ts=_iso(datetime.fromtimestamp(cur["close_time_ms"] / 1000.0, tz=timezone.utc)),
            fill_ts=_iso(),
            token_id=token_id,
        )
        self.open[pos.key()] = pos

        # Charge fee notionally at fill time into equity? Spec: "charge 0.07*p*(1-p)
        # into paper equity when recording fills". We deduct fee from cash at fill
        # (conservative) — resolve also must not double-charge. So charge once at fill.
        fee = shares * poly_taker_fee(fill_p)
        self.sess["cash"] = self.cash - fee

        rec = {
            "kind": "spot_lag",
            "slug": pos.slug,
            "coin": coin,
            "symbol": symbol,
            "minutes": minutes,
            "which": "current",
            "window_start": pos.window_start,
            "window_end": pos.window_end,
            "direction": direction,
            "side": side,
            "move_pct": move,
            "spot": spot,
            "open_px": open_px,
            "fair_up": fair_up,
            "fair_side": fair_side,
            "entry_model": model,
            "live_ask": live_ask,
            "entry_p": fill_p,
            "fill_p": fill_p,
            "entry_source": entry_source,
            "edge": edge,
            "shares": shares,
            "cost": cost,
            "fee": fee,
            "clip": clip,
            "elapsed_sec": elapsed,
            "remaining_sec": remaining,
            "signal_ts": pos.signal_ts,
            "token_id": token_id,
            "equity": self.equity,
            "catchup": p.catchup,
            "slip": p.slip,
            "min_edge": p.min_edge,
            "threshold": p.threshold,
            "live": False,
            "note": "paper spot_lag fill; no live CLOB order",
        }
        self.ledger.append(rec)
        note.action = "FILL"
        note.detail = (
            f"{direction} @{fill_p:.3f} ({entry_source}) edge={edge:.4f} "
            f"shares={shares:.2f} fee={fee:.4f}"
        )
        return note, rec


# ---------------------------------------------------------------------------
# CLI: scan / run
# ---------------------------------------------------------------------------


def _print_cycle(
    notes: list[CycleNote],
    events: list[dict[str, Any]],
    engine: SpotLagPaper,
) -> None:
    console.rule("[bold]spot_lag paper (no live orders)")
    console.print(
        f"[dim]equity=${engine.equity:.2f}  cash=${engine.cash:.2f}  "
        f"open={len(engine.open)}/{engine.params.max_concurrent}  "
        f"fills={engine.sess.get('fills')}  resolves={engine.sess.get('resolves')}  "
        f"ledger={engine.ledger.path}[/dim]"
    )
    for n in notes:
        move = f"{n.move_pct*100:+.3f}%" if n.move_pct is not None else "—"
        elrem = (
            f"el={n.elapsed:.0f}s rem={n.remaining:.0f}s"
            if n.elapsed is not None and n.remaining is not None
            else ""
        )
        fair = f"fair={n.fair_side:.3f}" if n.fair_side is not None else ""
        model = f"model={n.entry_model:.3f}" if n.entry_model is not None else ""
        ask = f"ask={n.live_ask:.3f}" if n.live_ask is not None else "ask=—"
        fill = f"fill={n.fill_p:.3f}" if n.fill_p is not None else ""
        edge = f"edge={n.edge:.4f}" if n.edge is not None else ""
        slug = n.slug or "—"
        direction = n.direction or "—"
        style = "green" if n.action == "FILL" else "dim"
        console.print(
            f"[{style}]{n.coin:4}[/{style}] move={move:9} {direction:4} {slug}  "
            f"{elrem} {fair} {model} {ask} {fill} {edge}  "
            f"[bold]{n.action}[/bold] {n.detail}"
        )
    for e in events:
        kind = e.get("kind")
        if kind == "spot_lag":
            console.print(
                f"[green]FILL[/green] spot_lag {e.get('slug')} "
                f"{e.get('direction')} @{float(e.get('fill_p') or 0):.3f} "
                f"({e.get('entry_source')}) shares={float(e.get('shares') or 0):.2f} "
                f"edge={float(e.get('edge') or 0):+.4f} fee={float(e.get('fee') or 0):.4f}"
            )
        elif kind == "spot_lag_resolve":
            flag = "SCRATCH" if e.get("scratched") else ("WIN" if e.get("won") else "LOSS")
            console.print(
                f"[cyan]RESOLVE[/cyan] {flag} {e.get('slug')} "
                f"resolved={e.get('resolved')} pnl_after_fee="
                f"{float(e.get('pnl_after_fee') or 0):+.4f} "
                f"equity=${float(e.get('equity') or 0):.2f}"
            )
    if not events:
        console.print(
            f"{datetime.now(timezone.utc).isoformat()}  "
            f"coins={len(notes)} paper events=0  equity=${engine.equity:.2f}"
        )
    console.print(
        "[dim]Paper only. Fee 0.07·p·(1-p) charged at fill. "
        "Session: data/spot_lag_session.json (separate from poly_session.json). "
        "Live wallet path OUT OF SCOPE.[/dim]"
    )



def _build_engine(args: Any) -> SpotLagPaper:
    params = SpotLagParams(
        catchup=float(args.catchup),
        slip=float(args.slip),
        min_edge=float(args.min_edge),
        threshold=float(args.threshold),
    )
    return SpotLagPaper(params=params, starting_balance=float(args.balance))


async def cmd_spot_lag_scan(cfg: AppConfig, args: Any) -> None:
    if getattr(args, "live", False):
        raise SystemExit("spot-lag is paper-only; --live is refused")
    coins = parse_coins(args.coins, DEFAULT_COINS)
    windows = parse_minutes_list(args.windows, DEFAULT_WINDOWS)
    engine = _build_engine(args)
    console.print(
        f"spot-lag-scan paper  coins={','.join(coins)}  windows={windows}  "
        f"catchup={engine.params.catchup} slip={engine.params.slip} "
        f"min_edge={engine.params.min_edge} threshold={engine.params.threshold}"
    )
    events, notes = await engine.step(coins, windows)
    _print_cycle(notes, events, engine)


async def cmd_spot_lag_run(cfg: AppConfig, args: Any) -> None:
    if getattr(args, "live", False):
        raise SystemExit("spot-lag is paper-only; --live is refused")
    coins = parse_coins(args.coins, DEFAULT_COINS)
    windows = parse_minutes_list(str(args.windows), DEFAULT_WINDOWS)
    interval = max(5, int(args.interval))
    engine = _build_engine(args)
    console.print(
        f"spot-lag paper loop every {interval}s  coins={','.join(coins)}  "
        f"windows={windows}  catchup={engine.params.catchup} slip={engine.params.slip} "
        f"min_edge={engine.params.min_edge} threshold={engine.params.threshold}  "
        f"ledger={engine.ledger.path}  session={session_path()}  data={data_dir()}"
    )
    console.print("[yellow]No live Polymarket orders. Paper fills only. No wallet required.[/yellow]")
    while True:
        try:
            events, notes = await engine.step(coins, windows)
            _print_cycle(notes, events, engine)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]cycle error[/red] {type(exc).__name__}: {exc}")
        await asyncio.sleep(interval)
