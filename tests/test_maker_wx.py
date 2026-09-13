"""The maker forward test is only as honest as its fill rule, so the rule is
what gets pinned: at-level prints never fill the lower tiers on their own,
queue fills need the resting size ahead to be consumed first, a sweep fills
every tier, a requote sends us to the back, and the window closes 3h out."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "maker_wx_fill", Path(__file__).resolve().parents[1] / "scripts" / "bt" / "maker_wx_fill.py"
)
fill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fill)

CLOSE = 200_000.0
M = {("kalshi", "T"): {"venue": "kalshi", "id": "T", "city": "x", "date": "26SEP12",
                       "close_ts": CLOSE, "result": None}}


def book(ts, bid, bid_sz, ask, ask_sz):
    return {"ts": ts, "venue": "kalshi", "id": "T", "bid": bid, "bid_sz": bid_sz, "ask": ask, "ask_sz": ask_sz}


def trade(ts, price, size, aggressor):
    return {"ts": ts, "venue": "kalshi", "id": "T", "price": price, "size": size, "aggressor": aggressor}


def by_tier(fills):
    out = {t: 0.0 for t in fill.TIERS}
    for f in fills:
        out[f["tier"]] += f["size"]
    return out


def test_print_at_level_only_counts_for_atlevel_until_queue_ahead_is_consumed():
    books = [book(1000, 0.40, 30.0, 0.41, 30.0)]
    trades = [trade(1010, 0.40, 20.0, "sell")]           # 20 of the 30 ahead of us
    got = by_tier(fill.simulate(M, books, trades, quote_size=10))
    assert got == {"through": 0.0, "queue": 0.0, "atlevel": 10.0}


def test_queue_fills_on_overflow_past_size_ahead():
    books = [book(1000, 0.40, 30.0, 0.41, 30.0)]
    trades = [trade(1010, 0.40, 25.0, "sell"), trade(1020, 0.40, 8.0, "sell")]   # 33 > 30 ahead
    got = by_tier(fill.simulate(M, books, trades, quote_size=10))
    assert got["queue"] == pytest.approx(3.0)
    assert got["through"] == 0.0


def test_sweep_through_our_level_fills_every_tier_in_full():
    books = [book(1000, 0.40, 30.0, 0.41, 30.0)]
    trades = [trade(1010, 0.39, 1.0, "sell")]             # printed below our bid: level was cleared
    got = by_tier(fill.simulate(M, books, trades, quote_size=10))
    assert got == {"through": 10.0, "queue": 10.0, "atlevel": 10.0}


def test_ask_side_is_symmetric():
    books = [book(1000, 0.40, 30.0, 0.41, 5.0)]
    trades = [trade(1010, 0.41, 6.0, "buy"), trade(1020, 0.42, 1.0, "buy")]
    fills_ = fill.simulate(M, books, trades, quote_size=10)
    sells = [f for f in fills_ if f["side"] == "sell"]
    assert by_tier(sells)["queue"] == pytest.approx(10.0)   # 1 on overflow, 9 more on the sweep
    assert all(f["price"] == 0.41 for f in sells)


def test_wrong_side_prints_do_not_touch_us():
    books = [book(1000, 0.40, 30.0, 0.41, 30.0)]
    trades = [trade(1010, 0.40, 100.0, "buy")]            # buyer lifting at 0.40 is not hitting our bid
    assert fill.simulate(M, books, trades, quote_size=10) == []


def test_requote_on_touch_move_resets_queue_to_back():
    books = [book(1000, 0.40, 30.0, 0.41, 30.0), book(1100, 0.39, 50.0, 0.41, 30.0)]
    trades = [trade(1050, 0.40, 29.0, "sell"),            # nearly through the old queue
              trade(1150, 0.39, 20.0, "sell")]            # new queue of 50 ahead: no fill
    got = by_tier(fill.simulate(M, books, trades, quote_size=10))
    assert got["queue"] == 0.0


def test_no_quotes_inside_window_end_or_on_empty_side():
    books = [book(CLOSE - 2 * 3600, 0.40, 30.0, 0.41, 30.0), book(1000, 0.0, 0.0, 0.41, 30.0)]
    trades = [trade(CLOSE - 2 * 3600 + 10, 0.30, 5.0, "sell"), trade(1010, 0.10, 5.0, "sell")]
    assert fill.simulate(M, books, trades, quote_size=10) == []


def test_settlement_pnl_and_fee_direction():
    books = [book(1000, 0.40, 1.0, 0.41, 1.0), book(5000, 0.50, 1.0, 0.52, 1.0)]
    trades = [trade(1010, 0.39, 1.0, "sell"), trade(1020, 0.42, 1.0, "buy")]
    markets = {k: dict(v, result="yes") for k, v in M.items()}
    fills_ = fill.simulate(markets, books, trades, quote_size=10)
    fill.settle(fills_, markets, books)
    buy = next(f for f in fills_ if f["side"] == "buy" and f["tier"] == "queue")
    sell = next(f for f in fills_ if f["side"] == "sell" and f["tier"] == "queue")
    assert buy["pnl"] == pytest.approx((1.0 - 0.40 - fill.kalshi_maker_fee(0.40)) * 10)
    assert sell["pnl"] == pytest.approx((0.41 - 1.0 - fill.kalshi_maker_fee(0.41)) * 10)
    assert buy["pnl_rebate"] == buy["pnl"]                # rebate is Polymarket-only
    assert buy["markout_1h"] == pytest.approx(0.51 - 0.40)
    assert sell["markout_1h"] == pytest.approx(0.41 - 0.51)


def test_kalshi_maker_fee_is_quarter_of_taker_curve():
    assert fill.kalshi_maker_fee(0.5) == pytest.approx(0.25 * 0.07 * 0.25)
    assert fill.kalshi_maker_fee(0.0) == 0.0 and fill.kalshi_maker_fee(1.0) == 0.0
