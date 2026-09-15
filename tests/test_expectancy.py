"""scripts/bt/expectancy.py is a front for edgecheck: it knows this project's
ledger shapes and nothing else. These tests pin the shape detection and the
column mapping -- the statistics are edgecheck's to test."""

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


def _ledger(tmp_path: Path, trades: list[tuple[float, float, bool, float]],
            side: str = "yes") -> Path:
    """trades = [(model_prob, edge, won, pnl_after_fee)] -> a kalshi_lag ledger.
    Each trade gets its own 15-minute window so clusters == trades."""
    p = tmp_path / "led.jsonl"
    with p.open("w") as f:
        for i, (prob, edge, won, pnl) in enumerate(trades):
            close = 1_789_000_000 + 900 * i
            f.write(json.dumps({
                "kind": "kalshi_lag", "ticker": f"T{i}", "fair": prob, "side": side,
                "edge": edge, "shares": 1.0, "entry_p": 0.5, "ask_size": 50,
                "ts": close - 300, "close_ts": close, "fee": 0.02,
            }) + "\n")
            f.write(json.dumps({
                "kind": "kalshi_lag_resolve", "ticker": f"T{i}",
                "won": won, "pnl_after_fee": pnl, "fee": 0.02, "ts": close + 60,
            }) + "\n")
    return p


def _flat_ledger(tmp_path: Path, trades: list[tuple[float, float, bool, float]]) -> Path:
    """The shape scripts/bt/replay_kalshilag.py writes: one settled row per
    trade, no "kind", pnl already net of fees."""
    p = tmp_path / "flat.jsonl"
    with p.open("w") as f:
        for i, (prob, edge, won, pnl) in enumerate(trades):
            close = 1_789_000_000 + 900 * i
            f.write(json.dumps({
                "ticker": f"T{i}", "coin": "btc", "side": "yes", "ask": 0.5,
                "fair": prob, "edge": edge, "shares": 1.0, "fee": 0.02,
                "won": won, "pnl": pnl, "signal_ts": close - 300, "close_ts": close,
            }) + "\n")
    return p


def test_a_paired_live_ledger_is_detected_and_scored(tmp_path, capsys):
    rng = random.Random(1)
    led = _ledger(tmp_path, [(0.7, 0.2, True, rng.gauss(5.0, 1.0)) for _ in range(40)])
    rc = expectancy.main([str(led)])
    cap = capsys.readouterr()
    out = cap.out
    assert "shape kalshi_lag" in cap.err
    assert rc == 0                                   # a clear edge over 40 windows: GO
    assert "resolved trades       40" in out
    assert "clusters              40" in out
    assert "GO -- interval" in out


def test_a_flat_backtest_ledger_is_detected_and_scored(tmp_path, capsys):
    rng = random.Random(3)
    led = _flat_ledger(tmp_path, [(0.6, 1.0, True, rng.gauss(5.0, 1.0)) for _ in range(200)])
    rc = expectancy.main([str(led)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "resolved trades       200" in out
    assert "cluster-robust" in out


def test_three_lucky_wins_get_no_verdict(tmp_path, capsys):
    led = _ledger(tmp_path, [(0.9, 0.1, True, 7.0), (0.9, 0.1, True, 7.1), (0.9, 0.1, True, 6.9)])
    rc = expectancy.main([str(led)])
    assert rc == 3
    assert "NO_VERDICT" in capsys.readouterr().out


def test_live_kalshi_fair_is_p_yes_and_is_flipped_on_the_no_side(tmp_path, capsys):
    """The live ledger stores P(YES); the tool passes --predicted-is-yes so a
    no-side trade at fair 0.7 is scored as a 0.3 bet, not a 0.7 one."""
    trades = [(0.7, 0.1, False, -1.0)] * 40
    led = _ledger(tmp_path, trades, side="no")
    expectancy.main([str(led), "--json"])
    d = json.loads(capsys.readouterr().out)
    cal = d["calibration"]["buckets"]
    assert cal and all(b["predicted"] == pytest.approx(0.3) for b in cal)


def test_trades_in_the_same_window_are_one_cluster(tmp_path, capsys):
    """Every coin's 15-minute market settles on the same window; edgecheck
    must be told that via close_ts or it counts them as independent."""
    p = tmp_path / "led.jsonl"
    with p.open("w") as f:
        for i in range(60):
            close = 1_789_000_000 + 900 * (i // 3)     # three coins per window
            f.write(json.dumps({"kind": "kalshi_lag", "ticker": f"T{i}", "fair": 0.7,
                                "side": "yes", "edge": 0.1, "shares": 1.0, "entry_p": 0.5,
                                "ts": close - 300, "close_ts": close, "fee": 0.02}) + "\n")
            f.write(json.dumps({"kind": "kalshi_lag_resolve", "ticker": f"T{i}", "won": True,
                                "pnl_after_fee": 1.0 + (i % 3) * 0.1, "ts": close + 60}) + "\n")
    expectancy.main([str(p), "--json"])
    d = json.loads(capsys.readouterr().out)
    assert d["expectancy"]["n"] == 60
    assert d["expectancy"]["clusters"] == 20


def test_extra_flags_pass_through_to_edgecheck(tmp_path, capsys):
    rng = random.Random(5)
    led = _ledger(tmp_path, [(0.7, 0.2, True, rng.gauss(5.0, 1.0)) for _ in range(40)])
    expectancy.main([str(led), "--bankroll", "100", "--deploy-size", "10", "--json"])
    d = json.loads(capsys.readouterr().out)
    assert d["risk"]["bankroll"] == 100.0
    assert d["capacity"]["deploy_size"] == 10.0


def test_a_missing_or_unrecognised_ledger_is_a_usage_error(tmp_path, capsys):
    assert expectancy.main([str(tmp_path / "nope.jsonl")]) == 2
    p = tmp_path / "weird.jsonl"
    p.write_text(json.dumps({"kind": "something_else"}) + "\n")
    with pytest.raises(SystemExit, match="unrecognised ledger"):
        expectancy.main([str(p)])
