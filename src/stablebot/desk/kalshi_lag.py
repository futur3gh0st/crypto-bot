"""Vol-aware directional sleeve on Kalshi 15-minute crypto binaries. Paper only.

This is the spot-lag thesis — a sharp move on the spot tape, then a prediction
market that has not fully repriced — run on a venue that is actually reachable.

It is a better fit than the Polymarket version in one important way: Kalshi
publishes an explicit strike on every market ("Target Price: $78,835.32"), so
the reference price is read straight off the contract instead of being inferred
from a window-open candle.

    fair_yes = Phi( log(spot / strike) / (sigma * sqrt(tau)) )

with sigma the per-symbol EWMA of 1m log returns and tau the minutes left until
close. Both sides are then compared against their own ask, fee included, and
whichever side shows the larger edge is the candidate.

Two honest caveats, both material:

* **Settlement basis.** Kalshi settles these on CF Benchmarks, not on Binance.
  The model reads Binance spot, so there is basis risk between the price this
  sleeve reasons about and the price that decides the contract. CF Benchmarks'
  index aggregates several venues including Binance, so they track closely, but
  "closely" is not "exactly" — and near a strike, small basis moves flip
  outcomes. Paper resolution here uses Binance and is therefore an approximation
  of settlement, not a replay of it.

* **Fees.** Kalshi's published schedule is round_up(0.07 * C * P * (1-P)) per
  side. Paper charges the unrounded curve, which slightly understates the cost
  of small clips.

No live orders. This sleeve has no order-placement path at all.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from stablebot.config import data_dir
from stablebot.desk.reference import ReferencePrice
from stablebot.desk.signal import (
    GateCounter,
    SignalCfg,
    VolTracker,
    distance_is_measurable,
    fair_with_reference_noise,
    vol_fair_up,
)
from stablebot.kalshi.client import KalshiClient, KalshiNotFound, KalshiRateLimit
from stablebot.kalshi.strategy import kalshi_taker_fee as curve_fee
from stablebot.poly.markets import COIN_SPOT
from stablebot.poly.spot_lag import fetch_1m_klines

SEED_BARS = 500
VISION = "https://data-api.binance.vision"
BINANCE = "https://api.binance.com"

# Kalshi 15m crypto series that have a Binance spot equivalent to model against.
COIN_SERIES: dict[str, str] = {
    "btc": "KXBTC15M",
    "eth": "KXETH15M",
    "sol": "KXSOL15M",
    "xrp": "KXXRP15M",
    "doge": "KXDOGE15M",
    "bnb": "KXBNB15M",
    # Kalshi lists 14 crypto 15M series. These five are every remaining one that
    # clears both bars a coin has to clear: a CF-style composite from 3+ of the
    # reference venues, and a Binance 1m series to seed sigma from. HYPE has the
    # reference but no Binance symbol (400), so it cannot be vol-seeded and stays
    # out. KXCRYPTOCOMP15M and KXCRYPTOLEAD15M are comparison and race markets,
    # not "price above strike", so fair_yes() does not describe them.
    "ada": "KXADA15M",
    "bch": "KXBCH15M",
    "near": "KXNEAR15M",
    "ton": "KXTON15M",
    "zec": "KXZEC15M",
}


def _iso(ts: datetime | None = None) -> str:
    return (ts or datetime.now(timezone.utc)).isoformat()


def session_path() -> Path:
    return data_dir() / "kalshi_lag_session.json"


def ledger_path() -> Path:
    return data_dir() / "kalshi_lag_ledger.jsonl"


def parse_close_ts(market: dict[str, Any]) -> float | None:
    raw = market.get("close_time")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_strike(market: dict[str, Any]) -> float | None:
    for key in ("floor_strike", "cap_strike"):
        v = market.get(key)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return None


# ---------------------------------------------------------------------------
# session + ledger
# ---------------------------------------------------------------------------


def load_session(path: Path | None = None, starting: float = 1000.0) -> dict[str, Any]:
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
            "live": False,
            "note": "kalshi_lag paper session; separate pot; no live orders",
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
    raw["live"] = False
    return raw


def save_session(sess: dict[str, Any], path: Path | None = None) -> None:
    path = path or session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    sess = dict(sess)
    sess["live"] = False
    sess["last_ts"] = _iso()
    sess["equity"] = float(sess.get("cash") or 0.0) + float(sess.get("open_cost") or 0.0)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sess, indent=2), encoding="utf-8")
    tmp.replace(path)


class KalshiLagLedger:
    def __init__(self, path: Path | None = None):
        self.path = path or ledger_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, rec: dict[str, Any]) -> None:
        rec = dict(rec)
        rec.setdefault("ts", _iso())
        rec["live"] = False
        rec.setdefault("venue", "kalshi")
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
# model
# ---------------------------------------------------------------------------


def fair_yes(spot: float, strike: float, sigma_1m: float, minutes_left: float) -> float:
    """P(settle >= strike) for a driftless walk with `minutes_left` to go."""
    return vol_fair_up(spot, strike, sigma_1m, minutes_left)


@dataclass
class LagPos:
    ticker: str
    coin: str
    symbol: str
    series: str
    strike: float
    close_ts: float
    side: str            # yes | no
    shares: float
    entry_p: float
    cost: float
    fee: float
    fair: float
    z: float
    sigma: float
    fill_ts: str

    @property
    def key(self) -> str:
        return self.ticker


@dataclass
class LagNote:
    coin: str
    ticker: str | None = None
    minutes_left: float | None = None
    strike: float | None = None
    spot: float | None = None
    sigma: float | None = None
    z: float | None = None
    fair: float | None = None
    yes_ask: float | None = None
    no_ask: float | None = None
    side: str | None = None
    edge: float | None = None
    ref_method: str | None = None
    ref_dispersion_bp: float | None = None
    distance_ratio: float | None = None
    fair_err: float | None = None
    depth_contracts: float | None = None
    action: str = "idle"
    detail: str = ""


@dataclass
class LagParams:
    signal: SignalCfg = field(default_factory=SignalCfg)
    max_concurrent: int = 4
    risk_frac: float = 0.05
    fixed_clip: float = 15.0
    min_remaining_sec: float = 90.0
    max_remaining_sec: float = 14 * 60.0
    depth_mult: float = 1.5      # resting contracts must cover this x our size
    # Our reference is never exact against a CF-settled strike. Even when the
    # venues agree perfectly, hold a floor on that uncertainty rather than
    # pretending a 1 bp distance is a real measurement.
    min_ref_sigma_bp: float = 2.0
    fallback_ref_sigma_bp: float = 8.0   # single-source reference: trust it less


class KalshiLagPaper:
    """Detect a move on the spot tape, buy the lagging side, hold to settlement."""

    def __init__(
        self,
        params: LagParams | None = None,
        starting_balance: float = 1000.0,
        ledger: KalshiLagLedger | None = None,
        reference: ReferencePrice | None = None,
    ):
        self.params = params or LagParams()
        self.ledger = ledger or KalshiLagLedger()
        # Kalshi settles on CF Benchmarks, so price against a CF-style composite
        # rather than a single Binance USDT book.
        self.reference = reference or ReferencePrice()
        self.sess = load_session(starting=starting_balance)
        self.vol = VolTracker(halflife=self.params.signal.vol_halflife)
        self.gates = GateCounter()
        self.open: dict[str, LagPos] = {}
        self._seeded: set[str] = set()
        self._last_bar_ms: dict[str, int] = {}
        self._replay_open()

    # ---- state ---------------------------------------------------------

    def _replay_open(self) -> None:
        opened: dict[str, dict[str, Any]] = {}
        resolved: set[str] = set()
        for rec in self.ledger.load():
            ticker = str(rec.get("ticker") or "")
            if not ticker:
                continue
            if rec.get("kind") == "kalshi_lag":
                opened[ticker] = rec
            elif rec.get("kind") == "kalshi_lag_resolve":
                resolved.add(ticker)
        for ticker, rec in opened.items():
            if ticker in resolved:
                continue
            try:
                pos = LagPos(
                    ticker=ticker,
                    coin=str(rec.get("coin") or ""),
                    symbol=str(rec.get("symbol") or ""),
                    series=str(rec.get("series") or ""),
                    strike=float(rec.get("strike") or 0.0),
                    close_ts=float(rec.get("close_ts") or 0.0),
                    side=str(rec.get("side") or "yes"),
                    shares=float(rec.get("shares") or 0.0),
                    entry_p=float(rec.get("entry_p") or 0.0),
                    cost=float(rec.get("cost") or 0.0),
                    fee=float(rec.get("fee") or 0.0),
                    fair=float(rec.get("fair") or 0.0),
                    z=float(rec.get("z") or 0.0),
                    sigma=float(rec.get("sigma") or 0.0),
                    fill_ts=str(rec.get("ts") or ""),
                )
            except (TypeError, ValueError):
                continue
            if pos.strike > 0 and pos.close_ts > 0:
                self.open[ticker] = pos
        self.sess["open_cost"] = sum(p.cost for p in self.open.values())
        save_session(self.sess)

    @property
    def equity(self) -> float:
        return float(self.sess.get("cash") or 0.0) + float(self.sess.get("open_cost") or 0.0)

    @property
    def cash(self) -> float:
        return float(self.sess.get("cash") or 0.0)

    # ---- volatility ----------------------------------------------------

    async def seed_vol(self, http: httpx.AsyncClient, coins: list[str]) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for coin in coins:
            symbol = COIN_SPOT.get(coin)
            if not symbol or symbol in self._seeded:
                continue
            try:
                bars = await fetch_1m_klines(http, symbol, limit=SEED_BARS)
            except Exception:  # noqa: BLE001
                out[coin] = None
                continue
            out[coin] = self.vol.seed_from_closes(symbol, [b["close"] for b in bars])
            if bars:
                self._last_bar_ms[symbol] = int(bars[-1]["close_time_ms"])
            self._seeded.add(symbol)
        return out

    # ---- cycle ---------------------------------------------------------

    async def step(
        self,
        client: KalshiClient,
        http: httpx.AsyncClient,
        coins: list[str],
        now_ts: float | None = None,
    ) -> tuple[list[dict[str, Any]], list[LagNote]]:
        now_ts = time.time() if now_ts is None else now_ts
        events: list[dict[str, Any]] = []
        notes: list[LagNote] = []
        events.extend(await self._resolve_due(http, now_ts))
        for coin in coins:
            note, fill = await self._maybe_enter(client, http, coin, now_ts)
            notes.append(note)
            if fill is not None:
                events.append(fill)
        save_session(self.sess)
        return events, notes

    async def _settle_price(
        self, http: httpx.AsyncClient, coin: str, close_ts: float
    ):
        """Composite close of the minute ending at close_ts, CF-constituent venues."""
        return await self.reference.close_at(http, coin, close_ts)

    async def _resolve_due(
        self, http: httpx.AsyncClient, now_ts: float
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        due = [t for t, p in self.open.items() if p.close_ts <= now_ts]
        for ticker in sorted(due, key=lambda t: self.open[t].close_ts):
            pos = self.open.pop(ticker)
            ref = await self._settle_price(http, pos.coin, pos.close_ts)
            settle = ref.price if ref.ok else None
            self.sess["open_cost"] = max(
                0.0, float(self.sess.get("open_cost") or 0.0) - pos.cost
            )
            if settle is None:
                # cannot resolve honestly: refund the stake, keep the fee paid
                self.sess["cash"] = self.cash + pos.cost
                rec = {
                    "kind": "kalshi_lag_resolve",
                    "ticker": ticker,
                    "coin": pos.coin,
                    "side": pos.side,
                    "resolved": "UNKNOWN",
                    "won": False,
                    "scratched": True,
                    "shares": pos.shares,
                    "entry_p": pos.entry_p,
                    "pnl": 0.0,
                    "pnl_after_fee": 0.0,
                    "equity": self.equity,
                    "error": f"no settlement price ({ref.note or ref.method})",
                    "note": "refunded stake; paper only",
                }
                self.ledger.append(rec)
                self.sess["resolves"] = int(self.sess.get("resolves") or 0) + 1
                out.append(rec)
                continue

            yes_wins = settle >= pos.strike
            won = yes_wins if pos.side == "yes" else not yes_wins
            if won:
                self.sess["cash"] = self.cash + pos.shares * 1.0
                pnl = pos.shares * (1.0 - pos.entry_p)
            else:
                pnl = -pos.shares * pos.entry_p
            rec = {
                "kind": "kalshi_lag_resolve",
                "ticker": ticker,
                "coin": pos.coin,
                "symbol": pos.symbol,
                "series": pos.series,
                "side": pos.side,
                "strike": pos.strike,
                "settle_px": settle,
                "settle_source": ref.method,
                "settle_sources": ref.sources,
                "settle_dispersion_bp": ref.dispersion_bp,
                "resolved": "YES" if yes_wins else "NO",
                "won": won,
                "scratched": False,
                "shares": pos.shares,
                "entry_p": pos.entry_p,
                "cost": pos.cost,
                "fee": pos.fee,
                "pnl": pnl,
                "pnl_after_fee": pnl - pos.fee,
                "equity": self.equity,
                "note": f"paper resolve; {ref.label()}",
            }
            self.ledger.append(rec)
            self.sess["resolves"] = int(self.sess.get("resolves") or 0) + 1
            out.append(rec)
        return out

    async def _maybe_enter(
        self,
        client: KalshiClient,
        http: httpx.AsyncClient,
        coin: str,
        now_ts: float,
    ) -> tuple[LagNote, dict[str, Any] | None]:
        p = self.params
        sig = p.signal
        note = LagNote(coin=coin)
        symbol = COIN_SPOT.get(coin)
        series = COIN_SERIES.get(coin)
        if not symbol or not series:
            note.action = "skip"
            note.detail = "no series/symbol mapping"
            self.gates.hit("no_vol", note.detail)
            return note, None

        # ---- spot tape + vol -----------------------------------------
        try:
            bars = await fetch_1m_klines(http, symbol, limit=6)
        except Exception as exc:  # noqa: BLE001
            note.action = "skip"
            note.detail = f"klines: {exc}"
            self.gates.hit("no_vol", str(exc))
            return note, None
        closed = [b for b in bars if b["close_time_ms"] / 1000.0 <= now_ts + 1.0]
        if len(closed) < 2:
            closed = bars
        if len(closed) < 2 or closed[-2]["close"] <= 0:
            note.action = "skip"
            note.detail = "need >=2 1m bars"
            self.gates.hit("no_vol", note.detail)
            return note, None
        prev, cur = closed[-2], closed[-1]

        if symbol not in self._seeded:
            await self.seed_vol(http, [coin])
        bar_ms = int(cur["close_time_ms"])
        if bar_ms > self._last_bar_ms.get(symbol, 0):
            self.vol.update(symbol, math.log(cur["close"] / prev["close"]))
            self._last_bar_ms[symbol] = bar_ms

        sigma = self.vol.sigma(symbol)
        if sigma is None or sigma <= 0:
            note.action = "skip"
            note.detail = f"vol warming ({self.vol.bars(symbol)} bars)"
            self.gates.hit("no_vol", "warming")
            return note, None
        note.sigma = sigma
        note.spot = cur["close"]

        ret = math.log(cur["close"] / prev["close"])
        z = ret / sigma
        note.z = z

        # ---- pick the market ------------------------------------------
        try:
            markets = await client.list_open_markets(series)
        except (KalshiNotFound, KalshiRateLimit) as exc:
            note.action = "skip"
            note.detail = f"markets: {exc}"
            self.gates.hit("no_quote", str(exc))
            return note, None
        except Exception as exc:  # noqa: BLE001
            note.action = "skip"
            note.detail = f"markets: {type(exc).__name__}: {exc}"
            self.gates.hit("no_quote", note.detail)
            return note, None

        best: tuple[dict[str, Any], float, float] | None = None
        for m in markets:
            close_ts = parse_close_ts(m)
            strike = parse_strike(m)
            if close_ts is None or strike is None:
                continue
            remaining = close_ts - now_ts
            if not (p.min_remaining_sec <= remaining <= p.max_remaining_sec):
                continue
            if best is None or remaining < best[2] - now_ts:
                best = (m, strike, close_ts)
        if best is None:
            note.action = "skip"
            note.detail = "no market inside the entry window"
            self.gates.hit("window_timing", note.detail)
            return note, None
        market, strike, close_ts = best
        ticker = str(market.get("ticker") or "")
        remaining_min = max((close_ts - now_ts) / 60.0, 1.0 / 60.0)
        note.ticker = ticker
        note.strike = strike
        note.minutes_left = remaining_min

        # ---- indicative fair, from the cheap Binance tick --------------
        # Shown for every coin every cycle so the book is readable while idle.
        try:
            note.fair = fair_yes(cur["close"], strike, sigma, remaining_min)
        except ValueError:
            pass

        if abs(z) < sig.z_entry:
            note.action = "no_signal"
            note.detail = f"z={z:+.2f} of {sig.z_entry:.1f} needed (sigma={sigma*100:.4f}%/min)"
            self.gates.hit("no_signal", note.detail)
            return note, None

        # ---- trading fair, from the CF-style composite -----------------
        # Only fetched once a signal fires: four venues per call, so this stays
        # off the idle path.
        ref = await self.reference.spot(http, coin)
        if not ref.ok:
            note.action = "skip"
            note.detail = f"no reference price ({ref.note or ref.method})"
            self.gates.hit("no_quote", note.detail)
            return note, None
        note.spot = ref.price
        note.ref_method = ref.method
        note.ref_dispersion_bp = ref.dispersion_bp

        # How much do we actually trust our own reference level?
        if ref.method == "composite":
            ref_bp = max(p.min_ref_sigma_bp, ref.dispersion_bp or 0.0)
        elif ref.method == "cf_benchmarks":
            ref_bp = p.min_ref_sigma_bp
        else:
            ref_bp = max(p.fallback_ref_sigma_bp, ref.dispersion_bp or 0.0)
        ref_sigma = ref_bp / 1e4

        # A strike distance smaller than the venues' own disagreement is not a
        # measurement. This is what turned a 2 bp BTC distance into a fabricated
        # 19-cent edge and the largest single loss of the session.
        measurable, ratio = distance_is_measurable(
            ref.price, strike, ref_sigma, sig.min_distance_ratio
        )
        note.distance_ratio = ratio
        if not measurable:
            note.action = "skip"
            note.detail = (
                f"strike is {ratio:.1f}x ref-noise away, need "
                f"{sig.min_distance_ratio:.0f}x (ref +/-{ref_bp:.1f}bp)"
            )
            self.gates.hit("reference_noise", note.detail)
            return note, None

        try:
            fair, fair_err = fair_with_reference_noise(
                ref.price, strike, sigma, remaining_min, ref_sigma
            )
        except ValueError as exc:
            note.action = "skip"
            note.detail = str(exc)
            self.gates.hit("no_vol", str(exc))
            return note, None
        note.fair = fair
        note.fair_err = fair_err

        if ticker in self.open:
            note.action = "skip"
            note.detail = "already open on this market"
            self.gates.hit("already_open")
            return note, None
        if len(self.open) >= p.max_concurrent:
            note.action = "skip"
            note.detail = (
                "entries gated off" if p.max_concurrent == 0
                else f"max concurrent {p.max_concurrent}"
            )
            self.gates.hit("risk" if p.max_concurrent == 0 else "max_concurrent", note.detail)
            return note, None

        # ---- book -----------------------------------------------------
        try:
            book = await client.orderbook(ticker)
        except Exception as exc:  # noqa: BLE001
            note.action = "skip"
            note.detail = f"orderbook: {type(exc).__name__}: {exc}"
            self.gates.hit("no_quote", note.detail)
            return note, None
        note.yes_ask = book.yes_ask
        note.no_ask = book.no_ask

        candidates: list[tuple[str, float, float | None, float, float]] = []
        if book.yes_ask is not None:
            e = fair - book.yes_ask - curve_fee(book.yes_ask)
            candidates.append(("yes", book.yes_ask, book.yes_ask_size, e, fair))
        if book.no_ask is not None:
            e = (1.0 - fair) - book.no_ask - curve_fee(book.no_ask)
            candidates.append(("no", book.no_ask, book.no_ask_size, e, 1.0 - fair))
        if not candidates:
            note.action = "skip"
            note.detail = "no ask on either side"
            self.gates.hit("no_quote", note.detail)
            return note, None

        # Taking whichever side shows more edge means that whenever the model is
        # less confident than the market, the cheap side always wins the
        # comparison — so a sleeve whose thesis is "follow the move" ends up
        # fading it. Three of the first four trades did exactly that and all
        # three lost. Only buy a side the model itself favours.
        if sig.require_model_side:
            favoured = [c for c in candidates if c[4] > 0.5]
            if not favoured:
                best = max(candidates, key=lambda c: c[3])
                note.side = best[0]
                note.action = "skip"
                note.detail = (
                    f"model favours neither side at this price "
                    f"(fair {fair:.3f}); refusing to fade"
                )
                self.gates.hit("wrong_side", note.detail)
                return note, None
            candidates = favoured

        side, ask, ask_size, edge, side_fair = max(candidates, key=lambda c: c[3])
        note.side = side
        note.edge = edge

        if not (sig.min_ask <= ask <= sig.max_ask):
            note.action = "skip"
            note.detail = f"ask {ask:.3f} outside [{sig.min_ask:.2f},{sig.max_ask:.2f}]"
            self.gates.hit("edge", note.detail)
            return note, None
        # The edge has to clear the fair value's own error bar, not just a
        # fixed threshold — an edge smaller than the uncertainty is not an edge.
        required = sig.min_edge + sig.edge_uncertainty_mult * fair_err
        if edge < required:
            note.action = "skip"
            note.detail = (
                f"edge={edge:+.4f} < {required:.4f} "
                f"({sig.min_edge:.3f} + {sig.edge_uncertainty_mult:g}x err {fair_err:.3f}; "
                f"fair={side_fair:.3f} {side} ask={ask:.3f})"
            )
            self.gates.hit("edge", note.detail)
            return note, None

        # ---- size ------------------------------------------------------
        clip = min(p.risk_frac * self.equity, p.fixed_clip)
        if clip < 1.0 or not (0.0 < ask < 1.0):
            note.action = "skip"
            note.detail = f"bad clip/ask clip={clip:.2f} ask={ask:.3f}"
            self.gates.hit("sizing", note.detail)
            return note, None
        shares = clip / ask
        cost = shares * ask
        if cost > self.cash + 1e-9:
            note.action = "skip"
            note.detail = f"insufficient cash {self.cash:.2f} < {cost:.2f}"
            self.gates.hit("sizing", note.detail)
            return note, None
        # Kalshi quotes bids only, in notional dollars, and an ask is lifted off
        # the *opposite* bid. So `ask_size` dollars sit at price (1 - ask), and
        # the contracts we could actually lift are ask_size / (1 - ask) — not
        # ask_size compared against our dollar cost, which mixes units.
        if ask_size is not None:
            opposite = max(1e-9, 1.0 - ask)
            contracts_available = ask_size / opposite
            if contracts_available < shares * p.depth_mult:
                note.action = "skip"
                note.detail = (
                    f"depth {contracts_available:,.0f} contracts < "
                    f"{p.depth_mult:g}x our {shares:,.0f}"
                )
                self.gates.hit("sizing", note.detail)
                return note, None
            note.depth_contracts = contracts_available

        # ---- paper fill -------------------------------------------------
        fee = shares * curve_fee(ask)
        self.sess["cash"] = self.cash - cost - fee
        self.sess["open_cost"] = float(self.sess.get("open_cost") or 0.0) + cost
        self.sess["fills"] = int(self.sess.get("fills") or 0) + 1

        pos = LagPos(
            ticker=ticker,
            coin=coin,
            symbol=symbol,
            series=series,
            strike=strike,
            close_ts=close_ts,
            side=side,
            shares=shares,
            entry_p=ask,
            cost=cost,
            fee=fee,
            fair=fair,
            z=z,
            sigma=sigma,
            fill_ts=_iso(),
        )
        self.open[ticker] = pos

        rec = {
            "kind": "kalshi_lag",
            "model": "vol_aware_v2",
            "ticker": ticker,
            "series": series,
            "coin": coin,
            "symbol": symbol,
            "strike": strike,
            "close_ts": close_ts,
            "side": side,
            "spot": ref.price,
            "spot_binance": cur["close"],
            "spot_source": ref.method,
            "spot_sources": ref.sources,
            "spot_dispersion_bp": ref.dispersion_bp,
            "z": z,
            "sigma": sigma,
            "fair": fair,
            "fair_err": fair_err,
            "side_fair": side_fair,
            "ref_dispersion_bp": ref.dispersion_bp,
            "ref_sigma_bp": ref_bp,
            "distance_ratio": ratio,
            "required_edge": required,
            "yes_ask": book.yes_ask,
            "no_ask": book.no_ask,
            "ask_size_dollars": ask_size,
            "depth_contracts": note.depth_contracts,
            "entry_p": ask,
            "edge": edge,
            "shares": shares,
            "cost": cost,
            "fee": fee,
            "clip": clip,
            "remaining_min": remaining_min,
            "equity": self.equity,
            "z_entry": sig.z_entry,
            "min_edge": sig.min_edge,
            "note": "paper kalshi_lag fill; no live order",
        }
        self.ledger.append(rec)
        self.gates.hit("fired", f"{coin} {side} edge {edge:+.4f}")
        note.action = "FILL"
        note.detail = (
            f"{side.upper()} @{ask:.3f} vs fair {fair if side == 'yes' else 1 - fair:.3f} "
            f"(z={z:+.2f}) edge={edge:+.4f} shares={shares:.1f}"
        )
        return note, rec
