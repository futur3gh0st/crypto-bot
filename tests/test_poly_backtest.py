"""Unit tests: window filter, lock math, no-lookahead resolution. No network."""

from __future__ import annotations

from stablebot.poly.replay import (
    aligned_pairs,
    fade_pnl_per_share,
    filter_window_history,
    first_fade,
    first_lock,
    lock_pnl_per_share,
    poly_taker_fee,
    replay_window,
    winning_side,
    zero_fee,
)


START = 1_786_745_700
END = START + 900  # 15m


def test_window_filter_drops_premarket_and_post_close():
    points = [
        {"t": START - 100, "p": 0.50},  # pre-market
        {"t": START, "p": 0.49},
        {"t": START + 60, "p": 0.48},
        {"t": END, "p": 0.02},
        {"t": END + 9, "p": 0.005},  # after close
    ]
    kept = filter_window_history(points, START, END)
    assert [t for t, _ in kept] == [START, START + 60, END]
    assert all(START <= t <= END for t, _ in kept)


def test_window_filter_empty_and_inclusive_bounds():
    assert filter_window_history([], START, END) == []
    assert filter_window_history([(START - 1, 0.5)], START, END) == []
    assert filter_window_history([(START, 0.4)], START, END) == [(START, 0.4)]
    assert filter_window_history([(END, 0.9)], START, END) == [(END, 0.9)]


def test_lock_math_fee_zero_and_curve():
    assert abs(lock_pnl_per_share(0.49, 0.49, zero_fee) - 0.02) < 1e-12
    assert abs(lock_pnl_per_share(0.50, 0.51, zero_fee) + 0.01) < 1e-12
    # curve fee is positive and subtracts; never a rebate
    fee_up = poly_taker_fee(0.49)
    fee_dn = poly_taker_fee(0.49)
    assert fee_up > 0 and fee_dn > 0
    assert abs(fee_up - 0.07 * 0.49 * 0.51) < 1e-12
    edged = lock_pnl_per_share(0.49, 0.49, poly_taker_fee)
    assert abs(edged - (0.02 - fee_up - fee_dn)) < 1e-12
    assert edged < lock_pnl_per_share(0.49, 0.49, zero_fee)


def test_lock_gate_first_opportunity_no_lookahead_to_best():
    # later pair is a fatter lock; we must take the first
    pairs = aligned_pairs(
        [(START + 10, 0.49), (START + 80, 0.40)],
        [(START + 12, 0.49), (START + 81, 0.40)],
        align_tol_sec=15,
    )
    assert len(pairs) == 2
    hit = first_lock(pairs, min_lock=0.005)
    assert hit is not None
    assert hit.t == START + 12
    assert abs(hit.sum_p - 0.98) < 1e-12


def test_stale_carry_forward_is_not_a_lock():
    """A fresh 0.20 down + a 60s-stale 0.45 up must not pair (tol=15)."""
    pairs = aligned_pairs(
        [(START + 10, 0.45)],
        [(START + 12, 0.55), (START + 80, 0.20)],
        align_tol_sec=15,
    )
    assert all(abs(p.t_up - p.t_down) <= 15 for p in pairs)
    assert all(p.sum_p > 0.90 for p in pairs)
    assert first_lock(pairs, min_lock=0.005) is None


def test_fade_only_early_and_cheap_side():
    pairs = aligned_pairs(
        [(START + 20, 0.40), (START + 400, 0.30)],
        [(START + 21, 0.60), (START + 401, 0.70)],
        align_tol_sec=15,
    )
    hit = first_fade(pairs, START, 900, threshold=0.08, early_frac=1.0 / 3.0)
    assert hit is not None
    pr, side = hit
    assert side == "up"
    assert pr.t == START + 21
    # late cheap print is outside early window
    late_only = aligned_pairs(
        [(START + 400, 0.30)],
        [(START + 401, 0.70)],
        align_tol_sec=15,
    )
    assert first_fade(late_only, START, 900, 0.08, 1.0 / 3.0) is None


def test_fade_skips_when_both_sides_cheap():
    pairs = aligned_pairs(
        [(START + 10, 0.40)],
        [(START + 11, 0.40)],
        align_tol_sec=15,
    )
    assert first_fade(pairs, START, 900, 0.08, 1.0 / 3.0) is None


def test_resolution_from_outcome_prices_only():
    assert winning_side(["Up", "Down"], ["0", "1"]) == "down"
    assert winning_side(["Up", "Down"], ["1", "0"]) == "up"
    assert winning_side('["Up","Down"]', '["0","1"]') == "down"
    # in-progress / not 0-1
    assert winning_side(["Up", "Down"], ["0.4", "0.6"]) is None
    assert winning_side(["Up", "Down"], ["0", "0"]) is None
    assert winning_side(["Yes", "No"], ["1", "0"]) is None


def test_no_lookahead_resolution_ignores_last_mid():
    """Last tape print must not override outcomePrices."""
    last_mid_says_up = 0.995
    winner = winning_side(["Up", "Down"], ["0", "1"])
    assert winner == "down"
    assert last_mid_says_up > 0.9  # would have lied
    wr = replay_window(
        slug="btc-updown-15m-1786745700",
        coin="btc",
        minutes=15,
        start_unix=START,
        end_unix=END,
        hist_up=[(START + 10, 0.49), (END, last_mid_says_up)],
        hist_down=[(START + 11, 0.49), (END - 1, 0.005)],
        outcomes=["Up", "Down"],
        outcome_prices=["0", "1"],
        shares=20.0,
        allow_fade=True,
    )
    assert wr.winner == "down"
    assert wr.resolved
    # lock at first 0.49+0.49
    assert wr.lock is not None
    assert abs(wr.lock.pnl - 20.0 * 0.02) < 1e-12
    # lock is primary → no fade
    assert wr.fade is None


def test_unresolved_window_takes_no_trade():
    wr = replay_window(
        slug="x",
        coin="btc",
        minutes=15,
        start_unix=START,
        end_unix=END,
        hist_up=[(START + 10, 0.40)],
        hist_down=[(START + 11, 0.40)],
        outcomes=["Up", "Down"],
        outcome_prices=["0.5", "0.5"],
        shares=20.0,
    )
    assert wr.lock is None and wr.fade is None
    assert wr.skip_reason == "unresolved"


def test_fade_settles_at_resolution_not_last_price():
    wr = replay_window(
        slug="x",
        coin="eth",
        minutes=15,
        start_unix=START,
        end_unix=END,
        hist_up=[(START + 10, 0.40), (END, 0.99)],
        hist_down=[(START + 11, 0.60), (END - 2, 0.01)],
        outcomes=["Up", "Down"],
        outcome_prices=["0", "1"],  # Down won — fade Up loses
        shares=20.0,
        min_lock=0.005,
    )
    assert wr.lock is None
    assert wr.fade is not None
    assert wr.fade.side == "up"
    # paid 0.40, won 0 → pnl = 20 * (0 - 0.40)
    assert abs(wr.fade.pnl - 20.0 * (0.0 - 0.40)) < 1e-12


def test_fade_win_pnl():
    wr = replay_window(
        slug="x",
        coin="sol",
        minutes=15,
        start_unix=START,
        end_unix=END,
        hist_up=[(START + 10, 0.40)],
        hist_down=[(START + 11, 0.60)],
        outcomes=["Up", "Down"],
        outcome_prices=["1", "0"],
        shares=20.0,
    )
    assert wr.fade is not None
    assert abs(wr.fade.pnl - 20.0 * (1.0 - 0.40)) < 1e-12


def test_curve_fee_never_invents_rebate():
    assert poly_taker_fee(0.5) > 0
    assert poly_taker_fee(0.0) == 0.0
    assert poly_taker_fee(1.0) == 0.0
    assert fade_pnl_per_share(0.40, True, poly_taker_fee) < fade_pnl_per_share(
        0.40, True, zero_fee
    )
