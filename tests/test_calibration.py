"""Calibration: is the fair-value model sharper than the price it paid?"""

from __future__ import annotations

import json

import pytest

from stablebot.desk.calibration import (
    Outcome,
    bucketise,
    build_report,
    brier,
    load_outcomes,
)


def _ledger(tmp_path, rows):
    p = tmp_path / "kalshi_lag_ledger.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _fill(ticker, side, fair, entry):
    return {
        "kind": "kalshi_lag", "ticker": ticker, "coin": "btc",
        "side": side, "fair": fair, "entry_p": entry,
    }


def _resolve(ticker, side, entry, won, pnl=1.0, scratched=False):
    return {
        "kind": "kalshi_lag_resolve", "ticker": ticker, "coin": "btc",
        "side": side, "entry_p": entry, "won": won,
        "pnl_after_fee": pnl, "scratched": scratched, "ts": ticker,
    }


def test_a_yes_position_takes_fair_directly(tmp_path):
    p = _ledger(tmp_path, [_fill("A", "yes", 0.80, 0.72), _resolve("A", "yes", 0.72, True)])
    (o,) = load_outcomes(p)
    assert o.p_model == pytest.approx(0.80)
    assert o.p_market == pytest.approx(0.72)
    assert o.won is True


def test_a_no_position_takes_the_complement_of_fair(tmp_path):
    """fair is P(YES), so a NO bet's claim is 1 - fair."""
    p = _ledger(tmp_path, [_fill("A", "no", 0.707, 0.23), _resolve("A", "no", 0.23, False)])
    (o,) = load_outcomes(p)
    assert o.p_model == pytest.approx(0.293)


def test_scratched_and_unmatched_resolves_are_skipped(tmp_path):
    p = _ledger(tmp_path, [
        _fill("A", "yes", 0.80, 0.72),
        _resolve("A", "yes", 0.72, True, scratched=True),
        _resolve("ORPHAN", "yes", 0.5, True),          # no matching fill
    ])
    assert load_outcomes(p) == []


def test_a_duplicate_resolve_counts_once(tmp_path):
    p = _ledger(tmp_path, [
        _fill("A", "yes", 0.80, 0.72),
        _resolve("A", "yes", 0.72, True),
        _resolve("A", "yes", 0.72, True),
    ])
    assert len(load_outcomes(p)) == 1


def test_missing_ledger_is_empty_not_an_error(tmp_path):
    assert load_outcomes(tmp_path / "nope.jsonl") == []


def test_brier_rewards_the_confident_and_correct():
    assert brier([(1.0, 1.0)]) == pytest.approx(0.0)
    assert brier([(0.0, 1.0)]) == pytest.approx(1.0)
    assert brier([(0.5, 1.0), (0.5, 0.0)]) == pytest.approx(0.25)


def test_skill_is_negative_when_the_price_forecast_better():
    """Model claims 90% and loses; the 50c price was closer to the truth."""
    outs = [Outcome("A", "btc", "yes", 0.90, 0.50, False, -1.0, "t")]
    r = build_report(outs)
    assert r.brier_model > r.brier_market
    assert r.skill_vs_market < 0


def test_skill_is_positive_when_the_model_forecast_better():
    outs = [Outcome("A", "btc", "yes", 0.90, 0.50, True, 1.0, "t")]
    r = build_report(outs)
    assert r.skill_vs_market > 0


def test_buckets_report_claimed_against_realized():
    outs = [
        Outcome(f"L{i}", "btc", "yes", 0.30, 0.20, False, -1.0, f"t{i}") for i in range(3)
    ] + [Outcome("W", "btc", "yes", 0.90, 0.80, True, 1.0, "tw")]
    bs = bucketise(outs, n_buckets=5)
    low = next(b for b in bs if b.lo <= 0.30 < b.hi)
    assert low.n == 3
    assert low.realized == pytest.approx(0.0)
    assert low.gap == pytest.approx(0.30)      # claimed 30%, delivered nothing


def test_a_small_sample_is_flagged_as_not_meaningful():
    outs = [Outcome("A", "btc", "yes", 0.9, 0.8, True, 1.0, "t")]
    assert build_report(outs).meaningful is False


def test_an_empty_report_does_not_divide_by_zero():
    r = build_report([])
    assert r.n == 0
    assert r.skill_vs_market == 0.0
