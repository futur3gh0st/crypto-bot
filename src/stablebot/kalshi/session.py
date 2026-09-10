"""Shared $1000 paper pool: data/poly_session.json covers poly + kalshi.

equity = starting_equity
       + sum(pnl of pair_complete/complete_hedge in poly_ledger.jsonl)
       + sum(pnl of pair_complete in kalshi_ledger.jsonl)
Kalshi paper does not invent a second $1000. live=false always.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stablebot.config import data_dir

POLY_KINDS = frozenset({"pair_complete", "complete_hedge"})
KALSHI_KINDS = frozenset({"pair_complete"})


def session_path() -> Path:
    return data_dir() / "poly_session.json"


def poly_ledger_path() -> Path:
    return data_dir() / "poly_ledger.jsonl"


def kalshi_ledger_path() -> Path:
    return data_dir() / "kalshi_ledger.jsonl"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
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


def _load_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _qualifying(recs: list[dict[str, Any]], kinds: frozenset[str]) -> list[dict[str, Any]]:
    return [r for r in recs if r.get("kind") in kinds]


def ledger_pnl(which: str = "poly", path: Path | None = None) -> float:
    """Realised PnL booked in ONE ledger, counting the same kinds the shared
    scoreboard counts.

    The two lock sleeves share a pot, and the desk de-duplicates pot_start by
    pot_id but *sums* pot_pnl across sleeves. Each therefore has to seed from
    its own ledger; seeding both from the combined figure would book the pot
    twice.
    """
    if which == "poly":
        recs = _qualifying(load_jsonl(path or poly_ledger_path()), POLY_KINDS)
    else:
        recs = _qualifying(load_jsonl(path or kalshi_ledger_path()), KALSHI_KINDS)
    return sum(float(r.get("pnl") or 0.0) for r in recs)


def recompute_shared_session(
    path: Path | None = None,
    poly_ledger: Path | None = None,
    kalshi_ledger: Path | None = None,
) -> dict[str, Any]:
    """Rewrite the shared scoreboard from both ledgers. Preserves ``reset``."""
    path = path or session_path()
    existing = _load_existing(path)
    starting = float(existing.get("starting_equity") or 1000)
    reset = existing.get("reset")

    poly_recs = _qualifying(load_jsonl(poly_ledger or poly_ledger_path()), POLY_KINDS)
    kalshi_recs = _qualifying(load_jsonl(kalshi_ledger or kalshi_ledger_path()), KALSHI_KINDS)
    recs = [*poly_recs, *kalshi_recs]

    pnl = sum(float(r.get("pnl") or 0.0) for r in recs)
    fills = len(recs)
    last_ts = existing.get("last_ts")
    for r in recs:
        ts = r.get("ts")
        if isinstance(ts, str) and (last_ts is None or ts > str(last_ts)):
            last_ts = ts

    out: dict[str, Any] = {
        "starting_equity": starting if starting == int(starting) else starting,
        "equity": starting + pnl,
        "fills": fills,
        "last_ts": last_ts,
    }
    # keep starting_equity as 1000 int when it is 1000
    if abs(starting - 1000) < 1e-12:
        out["starting_equity"] = 1000
    if reset is not None:
        out["reset"] = reset

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return out
