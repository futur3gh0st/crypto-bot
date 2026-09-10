"""Polymarket Up/Down sleeve: slug math, lock_edge, no lookahead. No network."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from stablebot.config import PolyCfg
from stablebot.poly.fair import crude_fair_up, dislocation_bps
from stablebot.poly.markets import (
    ScanRow,
    current_and_next,
    slug_for,
    window_start_unix,
)
from stablebot.poly.paper import PolyLedger, PolyPaper
from stablebot.poly.strategy import can_pair_complete, fade_side, lock_edge


# Live example from 2026-08-14: 15m window 22:45-23:00 UTC
# slug btc-updown-15m-1786747500 → Aug 14, 6:45PM-7:00PM ET
KNOWN_15M = 1786747500
KNOWN_5M = 1786747800  # 22:50 UTC
NOW_IN_BOTH = 1786748012  # 22:53:32 UTC — inside 5m 22:50 and 15m 22:45


def _row(**kwargs) -> ScanRow:
    base = dict(
        coin="btc",
        minutes=5,
        which="current",
        slug="btc-updown-5m-1786747800",
        start_unix=KNOWN_5M,
        end_unix=KNOWN_5M + 300,
        minutes_left=1.5,
    )
    base.update(kwargs)
    return ScanRow(**base)


def test_slug_floor_matches_live_example():
    assert window_start_unix(NOW_IN_BOTH, 15) == KNOWN_15M
    assert window_start_unix(NOW_IN_BOTH, 5) == KNOWN_5M
    assert slug_for("btc", 15, KNOWN_15M) == "btc-updown-15m-1786747500"
    assert slug_for("BTC", 5, KNOWN_5M) == "btc-updown-5m-1786747800"
    assert slug_for("eth", 5, KNOWN_5M) == "eth-updown-5m-1786747800"


def test_current_window_never_in_the_future():
    cur, nxt = current_and_next("btc", 5, NOW_IN_BOTH)
    assert cur.start_unix <= NOW_IN_BOTH < cur.end_unix
    assert nxt.start_unix == cur.end_unix
    assert cur.which == "current" and nxt.which == "next"
    # next start is after now — that is the *next* window, not current
    assert nxt.start_unix > NOW_IN_BOTH


def test_window_start_at_boundary_is_current_not_next():
    # exactly on the open is the new current window
    assert window_start_unix(KNOWN_5M, 5) == KNOWN_5M
    assert window_start_unix(KNOWN_5M + 299, 5) == KNOWN_5M
    assert window_start_unix(KNOWN_5M + 300, 5) == KNOWN_5M + 300


def test_lock_edge_positive_and_negative():
    assert abs(lock_edge(0.49, 0.49, 0.0) - 0.02) < 1e-12
    assert abs(lock_edge(0.50, 0.51, 0.0) - (-0.01)) < 1e-12
    # 50 bps flat pair haircut
    assert abs(lock_edge(0.49, 0.49, 50.0) - 0.015) < 1e-12
    # the 2026-08-14 live book: 0.50+0.51 = 1.01 → no lock
    assert abs(lock_edge(0.50, 0.51, 0.0) + 0.01) < 1e-12
    assert lock_edge(None, 0.49) is None
    assert lock_edge(0.0, 0.49) is None


def test_lock_edge_does_not_invent_rebate():
    # fee only subtracts; never adds
    assert lock_edge(0.49, 0.49, 10.0) < lock_edge(0.49, 0.49, 0.0)


def test_can_pair_complete_requires_min_lock_and_both_asks():
    row = _row(up_ask=0.49, down_ask=0.49, lock_edge=0.02)
    assert can_pair_complete(row, min_lock=0.005, taker_fee_bps=0.0)
    row2 = _row(up_ask=0.50, down_ask=0.50, lock_edge=0.0)
    assert not can_pair_complete(row2, min_lock=0.005, taker_fee_bps=0.0)
    row3 = _row(up_ask=0.49, down_ask=None, lock_edge=None)
    assert not can_pair_complete(row3, min_lock=0.005, taker_fee_bps=0.0)
    # explicit zero size blocks
    row4 = _row(up_ask=0.49, down_ask=0.49, lock_edge=0.02, up_ask_size=0.0)
    assert not can_pair_complete(row4, min_lock=0.005, taker_fee_bps=0.0)


def test_crude_fair_at_open_is_half():
    assert abs(crude_fair_up(100.0, 100.0) - 0.50) < 1e-12
    # +2% clips to 0.98 with default scale 25
    assert crude_fair_up(102.0, 100.0) == 0.98
    # -2% clips to 0.02
    assert crude_fair_up(98.0, 100.0) == 0.02
    # +40 bps → 0.50 + 25*0.004 = 0.60
    assert abs(crude_fair_up(100.4, 100.0) - 0.60) < 1e-12


def test_fair_uses_only_open_not_future_close():
    """Fair is a function of (spot_now, open_then). A later close must not leak in."""
    open_px = 100.0
    spot_now = 100.2
    future_close = 110.0  # must be unused
    fair = crude_fair_up(spot_now, open_px)
    leaked = crude_fair_up(future_close, open_px)
    assert fair != leaked
    assert abs(fair - 0.55) < 1e-12


def test_dislocation_bps_sign():
    assert abs(dislocation_bps(0.60, 0.50) - 1000.0) < 1e-9
    assert abs(dislocation_bps(0.40, 0.50) - (-1000.0)) < 1e-9


def test_fade_requires_eight_cents_and_open_fair():
    row = _row(up_ask=0.40, down_ask=0.60, fair_up=0.50)
    assert fade_side(row, 0.08) == "up"
    row2 = _row(up_ask=0.45, down_ask=0.55, fair_up=0.50)
    assert fade_side(row2, 0.08) is None  # only 5c
    row3 = _row(up_ask=0.40, down_ask=0.60, fair_up=None)
    assert fade_side(row3, 0.08) is None  # no open print


def test_paper_lock_and_no_double_fill(tmp_path: Path):
    cfg = PolyCfg(min_lock=0.005, paper_shares=10.0, taker_fee_bps=0.0)
    ledger = PolyLedger(tmp_path / "poly_ledger.jsonl")
    eng = PolyPaper(cfg, ledger, fade=False)
    row = _row(up_ask=0.49, down_ask=0.49, sum_asks=0.98, lock_edge=0.02)
    fills = eng.step([row], datetime(2026, 8, 14, 22, 53, tzinfo=timezone.utc))
    done = [f for f in fills if not f.skipped]
    assert len(done) == 1
    assert done[0].kind == "pair_complete"
    assert abs(done[0].pnl - 0.20) < 1e-12  # 10 shares * 0.02
    # second pass: already complete
    fills2 = eng.step([row])
    assert all(f.skipped or f.kind != "pair_complete" or "already" in f.reason for f in fills2)
    lines = (tmp_path / "poly_ledger.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert "pair_complete" in lines[0]
    import json
    rec = json.loads(lines[0])
    assert rec["live"] is False


def test_paper_no_lock_when_sum_asks_over_one(tmp_path: Path):
    cfg = PolyCfg(min_lock=0.005)
    eng = PolyPaper(cfg, PolyLedger(tmp_path / "l.jsonl"), fade=False)
    row = _row(up_ask=0.50, down_ask=0.51, sum_asks=1.01, lock_edge=-0.01)
    fills = [f for f in eng.step([row]) if not f.skipped]
    assert fills == []


def test_fade_off_by_default(tmp_path: Path):
    cfg = PolyCfg(fade_threshold=0.08, paper_shares=10.0, max_inventory=50.0)
    eng = PolyPaper(cfg, PolyLedger(tmp_path / "l.jsonl"), fade=False)
    row = _row(up_ask=0.40, down_ask=0.62, fair_up=0.50, lock_edge=-0.02, sum_asks=1.02)
    fills = [f for f in eng.step([row]) if not f.skipped]
    assert fills == []


def test_fade_on_is_labeled_directional(tmp_path: Path):
    cfg = PolyCfg(fade_threshold=0.08, paper_shares=10.0, max_inventory=50.0)
    eng = PolyPaper(cfg, PolyLedger(tmp_path / "l.jsonl"), fade=True)
    row = _row(up_ask=0.40, down_ask=0.62, fair_up=0.50, lock_edge=-0.02, sum_asks=1.02)
    fills = [f for f in eng.step([row]) if not f.skipped]
    assert len(fills) == 1
    assert fills[0].kind == "fade"
    assert "directional" in fills[0].reason
    rec = fills[0].extra
    assert "DIRECTIONAL" in rec["note"]


def test_fade_respects_max_inventory(tmp_path: Path):
    cfg = PolyCfg(fade_threshold=0.08, paper_shares=10.0, max_inventory=10.0)
    eng = PolyPaper(cfg, PolyLedger(tmp_path / "l.jsonl"), fade=True)
    row = _row(up_ask=0.40, down_ask=0.62, fair_up=0.50, lock_edge=-0.02, sum_asks=1.02)
    first = [f for f in eng.step([row]) if not f.skipped]
    assert len(first) == 1
    second = eng.step([row])
    assert any(f.skipped and "max inventory" in f.reason for f in second)


def test_complete_hedge_uses_held_cost_not_future_ask(tmp_path: Path):
    """No lookahead: completion uses the ask we already paid + the *current* other ask."""
    cfg = PolyCfg(min_lock=0.005, paper_shares=10.0, fade_threshold=0.08)
    eng = PolyPaper(cfg, PolyLedger(tmp_path / "l.jsonl"), fade=True)
    t0 = datetime(2026, 8, 14, 22, 50, tzinfo=timezone.utc)
    cheap_up = _row(up_ask=0.40, down_ask=0.62, fair_up=0.50, lock_edge=-0.02, sum_asks=1.02)
    eng.step([cheap_up], t0)
    # later Down cheapens enough vs *held* 0.40, not vs a future Up print
    t1 = datetime(2026, 8, 14, 22, 52, tzinfo=timezone.utc)
    later = _row(up_ask=0.99, down_ask=0.55, fair_up=0.50, lock_edge=-0.54, sum_asks=1.54)
    fills = [f for f in eng.step([later], t1) if not f.skipped]
    assert any(f.kind == "complete_hedge" for f in fills)
    hedge = next(f for f in fills if f.kind == "complete_hedge")
    # 1 - 0.40 - 0.55 = 0.05; must NOT use the later 0.99 Up ask as the held cost
    assert abs(hedge.extra["held_cost"] - 0.40) < 1e-12
    assert abs(hedge.extra["lock_edge"] - 0.05) < 1e-12


def test_no_lookahead_next_window_has_no_open_fair():
    cur, nxt = current_and_next("btc", 5, NOW_IN_BOTH)
    assert nxt.start_unix > NOW_IN_BOTH
    # hydrate is not called; the rule is encoded: next window start is after now
    # so fetch_window_open would refuse. Mirror that gate here.
    from stablebot.poly.client import fetch_window_open
    import inspect
    src = inspect.getsource(fetch_window_open)
    assert "if start_unix > now_ts" in src
    assert "return None" in src
