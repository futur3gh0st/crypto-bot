"""Kalshi paper pair-complete. JSONL ledger. live=false always. No fade. No orders."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stablebot.config import KalshiCfg, data_dir
from stablebot.kalshi.client import ScanRow
from stablebot.kalshi.session import recompute_shared_session
from stablebot.kalshi.strategy import can_pair_complete, lock_edge, pair_curve_fee


def _iso(ts: datetime | None = None) -> str:
    return (ts or datetime.now(timezone.utc)).isoformat()


class KalshiLedger:
    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "kalshi_ledger.jsonl")
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
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


@dataclass
class PaperFill:
    kind: str
    ticker: str
    shares: float
    pnl: float
    reason: str
    skipped: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class KalshiPaper:
    """Paper fills only. Pair-complete both YES and NO. Never posts an order."""

    def __init__(
        self,
        cfg: KalshiCfg,
        ledger: KalshiLedger | None = None,
        update_session: bool = True,
    ):
        self.cfg = cfg
        self.ledger = ledger or KalshiLedger()
        self.update_session = update_session
        self.completed: set[str] = set()
        self._replay()

    def _replay(self) -> None:
        for rec in self.ledger.load():
            ticker = str(rec.get("ticker") or "")
            if rec.get("kind") == "pair_complete" and ticker:
                self.completed.add(ticker)

    def step(self, rows: list[ScanRow], now: datetime | None = None) -> list[PaperFill]:
        fills: list[PaperFill] = []
        for row in rows:
            fills.extend(self._maybe_lock(row, now))
        if self.update_session and any(not f.skipped for f in fills):
            recompute_shared_session()
        return fills

    def _maybe_lock(self, row: ScanRow, now: datetime | None) -> list[PaperFill]:
        if not row.ticker:
            return []
        if row.ticker in self.completed:
            return [
                PaperFill("pair_complete", row.ticker, 0.0, 0.0, "already pair-complete", True)
            ]
        if not can_pair_complete(row, self.cfg.min_lock, self.cfg.apply_curve_fee):
            return []
        shares = self.cfg.paper_shares
        if row.yes_ask is None or row.no_ask is None:
            return []
        curve = 0.0
        if self.cfg.apply_curve_fee:
            curve = pair_curve_fee(float(row.yes_ask), float(row.no_ask))
        edge = row.lock_edge
        if edge is None:
            edge = lock_edge(row.yes_ask, row.no_ask, self.cfg.apply_curve_fee) or 0.0
        # fee already inside edge when apply_curve_fee
        pnl = shares * edge
        if pnl <= 0:
            return [
                PaperFill(
                    "pair_complete",
                    row.ticker,
                    0.0,
                    pnl,
                    f"lock {edge:.4f} dies after curve {curve:.4f}",
                    True,
                )
            ]
        rec = {
            "ts": _iso(now),
            "kind": "pair_complete",
            "venue": "kalshi",
            "ticker": row.ticker,
            "series": row.series,
            "ask_yes": row.yes_ask,
            "ask_no": row.no_ask,
            "sum_asks": row.sum_asks if row.sum_asks is not None else (row.yes_ask + row.no_ask),
            "curve_fee": curve,
            "lock_edge": edge,
            "shares": shares,
            "pnl": pnl,
            "live": False,
            "note": "paper pair-complete; no live order",
        }
        self.ledger.append(rec)
        self.completed.add(row.ticker)
        return [
            PaperFill(
                "pair_complete",
                row.ticker,
                shares,
                pnl,
                f"lock {edge:.4f} / contract",
                False,
                rec,
            )
        ]
