"""Paper pair-complete and optional directional fade. JSONL ledger. No live orders."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stablebot.config import PolyCfg, data_dir
from stablebot.poly.markets import ScanRow
from stablebot.poly.replay import poly_taker_fee
from stablebot.poly.strategy import can_pair_complete, fade_side, lock_edge


def _iso(ts: datetime | None = None) -> str:
    return (ts or datetime.now(timezone.utc)).isoformat()


class PolyLedger:
    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "poly_ledger.jsonl")
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
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


@dataclass
class Inventory:
    up: float = 0.0
    down: float = 0.0
    up_cost: float = 0.0  # average ask paid
    down_cost: float = 0.0

    def add(self, side: str, shares: float, px: float) -> None:
        if shares <= 0:
            return
        if side == "up":
            total = self.up + shares
            self.up_cost = (self.up * self.up_cost + shares * px) / total
            self.up = total
        else:
            total = self.down + shares
            self.down_cost = (self.down * self.down_cost + shares * px) / total
            self.down = total

    def qty(self, side: str) -> float:
        return self.up if side == "up" else self.down


@dataclass
class PaperFill:
    kind: str
    slug: str
    shares: float
    pnl: float
    reason: str
    skipped: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class PolyPaper:
    """Paper fills only. Pair-complete is the locked sleeve; fade is directional."""

    def __init__(
        self,
        cfg: PolyCfg,
        ledger: PolyLedger | None = None,
        fade: bool = False,
    ):
        self.cfg = cfg
        self.ledger = ledger or PolyLedger()
        self.fade = fade
        self.completed: set[str] = set()
        self.inv: dict[str, Inventory] = {}
        self._replay()

    def _replay(self) -> None:
        for rec in self.ledger.load():
            slug = str(rec.get("slug") or "")
            kind = rec.get("kind")
            if not slug:
                continue
            inv = self.inv.setdefault(slug, Inventory())
            if kind == "pair_complete":
                self.completed.add(slug)
                shares = float(rec.get("shares") or 0)
                inv.add("up", shares, float(rec.get("ask_up") or 0))
                inv.add("down", shares, float(rec.get("ask_down") or 0))
            elif kind == "fade":
                side = str(rec.get("side") or "")
                shares = float(rec.get("shares") or 0)
                px = float(rec.get("ask") or 0)
                if side in {"up", "down"}:
                    inv.add(side, shares, px)
            elif kind == "one_leg_unwind":
                self.completed.add(slug)
            elif kind == "complete_hedge":
                self.completed.add(slug)
                side = str(rec.get("side") or "")
                shares = float(rec.get("shares") or 0)
                px = float(rec.get("ask") or 0)
                if side in {"up", "down"}:
                    inv.add(side, shares, px)

    def step(self, rows: list[ScanRow], now: datetime | None = None) -> list[PaperFill]:
        fills: list[PaperFill] = []
        for row in rows:
            fills.extend(self._maybe_lock(row, now))
            if self.fade:
                fills.extend(self._maybe_complete(row, now))
                fills.extend(self._maybe_fade(row, now))
        return fills

    def _maybe_lock(self, row: ScanRow, now: datetime | None) -> list[PaperFill]:
        if row.slug in self.completed:
            return [
                PaperFill("pair_complete", row.slug, 0.0, 0.0, "already pair-complete", True)
            ]
        if not can_pair_complete(row, self.cfg.min_lock, self.cfg.taker_fee_bps):
            return []
        shares = self.cfg.paper_shares
        edge = row.lock_edge
        if edge is None:
            edge = lock_edge(row.up_ask, row.down_ask, self.cfg.taker_fee_bps) or 0.0
        fee = self.cfg.taker_fee_bps / 10_000.0
        curve = 0.0
        if getattr(self.cfg, "apply_curve_fee", False) and row.up_ask and row.down_ask:
            curve = poly_taker_fee(float(row.up_ask)) + poly_taker_fee(float(row.down_ask))
        pnl = shares * (edge - curve)
        if getattr(self.cfg, "apply_curve_fee", False) and pnl <= 0:
            return [
                PaperFill(
                    "pair_complete",
                    row.slug,
                    0.0,
                    pnl,
                    f"lock {edge:.4f} dies after curve {curve:.4f}",
                    True,
                )
            ]
        rec = {
            "ts": _iso(now),
            "kind": "pair_complete",
            "slug": row.slug,
            "coin": row.coin,
            "minutes": row.minutes,
            "which": row.which,
            "ask_up": row.up_ask,
            "ask_down": row.down_ask,
            "sum_asks": row.sum_asks,
            "fee": fee,
            "curve_fee": curve,
            "lock_edge": edge,
            "shares": shares,
            "pnl": pnl,
            "live": False,
            "note": "paper pair-complete; no live order",
        }
        self.ledger.append(rec)
        self.completed.add(row.slug)
        inv = self.inv.setdefault(row.slug, Inventory())
        inv.add("up", shares, float(row.up_ask or 0))
        inv.add("down", shares, float(row.down_ask or 0))
        return [
            PaperFill(
                "pair_complete",
                row.slug,
                shares,
                pnl,
                f"lock {edge:.4f} / share",
                False,
                rec,
            )
        ]

    def _maybe_complete(self, row: ScanRow, now: datetime | None) -> list[PaperFill]:
        """If one side is already held, buy the other when it completes a lock."""
        if row.slug in self.completed:
            return []
        inv = self.inv.get(row.slug)
        if inv is None:
            return []
        fee = self.cfg.taker_fee_bps / 10_000.0
        # hold Up, Down now cheap enough vs our Up cost
        if inv.up > 0 and inv.down == 0 and row.down_ask is not None:
            edge = 1.0 - inv.up_cost - row.down_ask - fee
            if edge > self.cfg.min_lock:
                shares = min(inv.up, self.cfg.paper_shares)
                pnl = shares * edge
                rec = {
                    "ts": _iso(now),
                    "kind": "complete_hedge",
                    "slug": row.slug,
                    "side": "down",
                    "ask": row.down_ask,
                    "held_side": "up",
                    "held_cost": inv.up_cost,
                    "lock_edge": edge,
                    "shares": shares,
                    "pnl": pnl,
                    "live": False,
                    "note": "directional inventory completed; paper only",
                }
                self.ledger.append(rec)
                self.completed.add(row.slug)
                inv.add("down", shares, row.down_ask)
                return [PaperFill("complete_hedge", row.slug, shares, pnl, "complete Down", False, rec)]
        if inv.down > 0 and inv.up == 0 and row.up_ask is not None:
            edge = 1.0 - inv.down_cost - row.up_ask - fee
            if edge > self.cfg.min_lock:
                shares = min(inv.down, self.cfg.paper_shares)
                pnl = shares * edge
                rec = {
                    "ts": _iso(now),
                    "kind": "complete_hedge",
                    "slug": row.slug,
                    "side": "up",
                    "ask": row.up_ask,
                    "held_side": "down",
                    "held_cost": inv.down_cost,
                    "lock_edge": edge,
                    "shares": shares,
                    "pnl": pnl,
                    "live": False,
                    "note": "directional inventory completed; paper only",
                }
                self.ledger.append(rec)
                self.completed.add(row.slug)
                inv.add("up", shares, row.up_ask)
                return [PaperFill("complete_hedge", row.slug, shares, pnl, "complete Up", False, rec)]
        return []

    def _maybe_fade(self, row: ScanRow, now: datetime | None) -> list[PaperFill]:
        if row.slug in self.completed:
            return []
        side = fade_side(row, self.cfg.fade_threshold)
        if side is None:
            return []
        inv = self.inv.setdefault(row.slug, Inventory())
        if inv.qty(side) + self.cfg.paper_shares > self.cfg.max_inventory:
            return [
                PaperFill(
                    "fade",
                    row.slug,
                    0.0,
                    0.0,
                    f"max inventory {self.cfg.max_inventory}",
                    True,
                )
            ]
        ask = row.up_ask if side == "up" else row.down_ask
        if ask is None:
            return []
        shares = self.cfg.paper_shares
        rec = {
            "ts": _iso(now),
            "kind": "fade",
            "slug": row.slug,
            "side": side,
            "ask": ask,
            "fair_up": row.fair_up,
            "shares": shares,
            "pnl": 0.0,
            "live": False,
            "note": "DIRECTIONAL paper fade — not a lock; completing the other side later is the goal",
        }
        self.ledger.append(rec)
        inv.add(side, shares, ask)
        return [
            PaperFill(
                "fade",
                row.slug,
                shares,
                0.0,
                f"directional {side} @ {ask:.3f}",
                False,
                rec,
            )
        ]
