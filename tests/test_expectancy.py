"""The expectancy tool exists to stop us fooling ourselves, so its own
small-sample guards are the part most worth pinning down."""

from __future__ import annotations

import importlib.util
import json
import random
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "expectancy", Path(__file__).resolve().parents[1] / "scripts" / "bt" / "expectancy.py"
)
expectancy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(expectancy)


def _ledger(tmp_path: Path, trades: list[tuple[float, float, bool, float]]) -> Path:
    """trades = [(model_prob, edge, won, pnl_after_fee)] -> a kalshi_lag ledger."""
    p = tmp_path / "led.jsonl"
    with p.open("w") as f:
        for i, (prob, edge, won, pnl) in enumerate(trades):
            f.write(json.dumps({
                "kind": "kalshi_lag", "ticker": f"T{i}", "fair": prob,
                "edge": edge, "shares": 1.0, "entry_p": 0.5,
            }) + "\n")
            f.write(json.dumps({
                "kind": "kalshi_lag_resolve", "ticker": f"T{i}",
                "won": won, "pnl_after_fee": pnl,
            }) + "\n")
    return p


def _flat_ledger(tmp_path: Path, trades: list[tuple[float, float, bool, float]]) -> Path:
    """The shape scripts/bt/replay_kalshilag.py writes: one settled row per
    trade, no "kind", pnl already net of fees."""
    p = tmp_path / "flat.jsonl"
    with p.open("w") as f:
        for i, (prob, edge, won, pnl) in enumerate(trades):
            f.write(json.dumps({
                "ticker": f"T{i}", "coin": "btc", "side": "yes", "ask": 0.5,
                "fair": prob, "edge": edge, "shares": 1.0,
                "won": won, "pnl": pnl,
            }) + "\n")
    return p


def test_a_flat_backtest_ledger_is_read(tmp_path, capsys):
    """replay_kalshilag output must go through the same statistics as the live
    ledger -- that is the whole point of reading it."""
    import sys as _s
    rng = random.Random(3)
    led = _flat_ledger(tmp_path, [(0.6, 1.0, True, rng.gauss(5.0, 1.0)) for _ in range(200)])
    _s.argv = ["expectancy", str(led)]
    expectancy.main()
    out = capsys.readouterr().out
    assert "flat (settled backtest replay)" in out
    assert "resolved      200" in out
    assert "DISTINGUISHABLE from zero" in out


def test_flat_and_paired_agree_on_identical_trades(tmp_path, capsys):
    """Same trades in both shapes must produce the same mean and the same
    verdict, or one of the two code paths is lying."""
    import sys as _s
    rng = random.Random(5)
    trades = [(0.6, 1.0, rng.random() < 0.6, rng.gauss(2.0, 4.0)) for _ in range(120)]

    _s.argv = ["expectancy", str(_ledger(tmp_path, trades))]
    expectancy.main()
    paired_out = capsys.readouterr().out

    _s.argv = ["expectancy", str(_flat_ledger(tmp_path, trades))]
    expectancy.main()
    flat_out = capsys.readouterr().out

    def mean_line(text: str) -> str:
        return next(l for l in text.splitlines() if "mean per trade" in l).strip()

    assert mean_line(paired_out) == mean_line(flat_out)


def test_three_lucky_wins_get_no_verdict(tmp_path, capsys):
    """The exact shape that fooled the first version: a few wins on high
    -probability bets, tiny sample std, falsely narrow interval."""
    import sys as _s
    led = _ledger(tmp_path, [(0.92, 5.0, True, 7.0)] * 3)
    _s.argv = ["expectancy", str(led)]
    expectancy.main()
    out = capsys.readouterr().out
    assert "NO VERDICT" in out
    assert "DISTINGUISHABLE" not in out.replace("NOT distinguishable", "")


def test_a_real_edge_over_many_trades_is_reported(tmp_path, capsys):
    import sys as _s
    rng = random.Random(7)
    trades = [(0.6, 1.0, True, rng.gauss(5.0, 1.0)) for _ in range(200)]
    led = _ledger(tmp_path, trades)
    _s.argv = ["expectancy", str(led)]
    expectancy.main()
    out = capsys.readouterr().out
    assert "DISTINGUISHABLE from zero" in out
    assert "NO VERDICT" not in out


def test_pure_noise_over_many_trades_is_not_distinguishable(tmp_path, capsys):
    import sys as _s
    rng = random.Random(11)
    trades = [(0.5, 1.0, rng.random() < 0.5, rng.gauss(0.0, 10.0)) for _ in range(200)]
    led = _ledger(tmp_path, trades)
    _s.argv = ["expectancy", str(led)]
    expectancy.main()
    out = capsys.readouterr().out
    assert "NOT distinguishable from zero" in out


def test_a_no_side_trade_is_scored_against_its_own_probability():
    """The live kalshi ledger stores fair = P(YES), not P(side taken). Reading
    it as P(side) scores a no-side trade against its own complement -- a 0.70
    row is really the model saying 0.30. Verified against the real ledger:
    edge reconciles as (1 - fair) - entry_p - fee only on the no side."""
    spec = expectancy.LEDGERS["kalshi_lag"]
    assert expectancy.side_prob({"fair": 0.7072, "side": "no"}, spec) == pytest.approx(0.2928)
    assert expectancy.side_prob({"fair": 0.7763, "side": "yes"}, spec) == pytest.approx(0.7763)

    # The replay already stores P(side), so it must NOT be flipped again.
    flat = expectancy.FLAT_LEDGERS["kalshilag_trades"]
    assert expectancy.side_prob({"fair": 0.30, "side": "no"}, flat) == pytest.approx(0.30)


def test_t_critical_is_wider_than_normal_at_small_n():
    """Using 1.96 at n=3 was understating the interval by more than 2x."""
    assert expectancy.t95(2) == pytest.approx(4.303, abs=1e-3)
    assert expectancy.t95(2) > expectancy.Z95 * 2
    assert expectancy.t95(500) == pytest.approx(expectancy.Z95, abs=0.03)


def test_repeated_keys_pair_in_order(tmp_path):
    """Same ticker can be traded twice; resolves must not all bind to entry #1."""
    rows = [
        {"kind": "kalshi_lag", "ticker": "SAME", "fair": 0.6, "edge": 1.0, "shares": 1.0},
        {"kind": "kalshi_lag", "ticker": "SAME", "fair": 0.7, "edge": 2.0, "shares": 1.0},
        {"kind": "kalshi_lag_resolve", "ticker": "SAME", "won": True, "pnl_after_fee": 1.0},
        {"kind": "kalshi_lag_resolve", "ticker": "SAME", "won": False, "pnl_after_fee": -1.0},
    ]
    paired = expectancy.pair(rows, "kalshi_lag", "kalshi_lag_resolve", "ticker")
    assert len(paired) == 2
    assert [p["entry"]["fair"] for p in paired] == [0.6, 0.7]
