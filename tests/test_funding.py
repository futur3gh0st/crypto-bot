from datetime import datetime, timedelta, timezone

from stablebot.market.funding import (
    cost_usd,
    funding_cash,
    harvest_side,
    open_close_cost_bps,
    round_trip_cost_bps,
    should_enter,
    should_exit,
    trailing_avg,
)
from stablebot.strategy.funding_harvest import FundingState


def test_accrual_short_perp_positive_funding():
    # $1000 notional, 1 bp, short perp (side +1) → +$0.10
    assert abs(funding_cash(1000.0, 0.0001, 1) - 0.10) < 1e-12
    # long perp on the same print pays
    assert abs(funding_cash(1000.0, 0.0001, -1) - (-0.10)) < 1e-12
    # negative funding: short perp pays
    assert abs(funding_cash(1000.0, -0.0002, 1) - (-0.20)) < 1e-12


def test_round_trip_fee_math():
    # 10 + 5 taker, 1 bp half-spread, 2 legs one-way = 17 bps; RT = 34 bps
    assert abs(open_close_cost_bps(10, 5, 1) - 17.0) < 1e-12
    assert abs(round_trip_cost_bps(10, 5, 1) - 34.0) < 1e-12
    assert abs(cost_usd(1000.0, 34.0) - 3.40) < 1e-12


def test_enter_requires_three_same_sign_and_min():
    assert should_enter([0.0003, 0.0003]) is False
    assert should_enter([0.0003, 0.0003, 0.0003]) is True
    assert should_enter([0.0003, -0.0003, 0.0003]) is False
    assert should_enter([0.0001, 0.0001, 0.0001]) is False  # 1 bp < 3 bp default
    assert should_enter([0.0001, 0.0001, 0.0001], min_funding=0.0001) is True
    assert harvest_side([0.0003, 0.0003, 0.0003]) == 1
    assert harvest_side([-0.0003, -0.0003, -0.0003]) == -1


def test_three_bp_threshold_sits_flat():
    """A 1 bp same-sign streak must not enter under the 3 bp gate."""
    st = FundingState(min_funding=0.0003, exit_funding=0.00005, trail=3)
    ts0 = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    kinds = []
    for i in range(8):
        evs = st.on_print(
            "BTC-USDT-SWAP",
            ts0 + timedelta(hours=8 * i),
            0.0001,
            can_enter=True,
            notional=1000.0,
            one_way_cost_bps=6.0,
        )
        kinds.extend(e["kind"] for e in evs)
    assert "funding_enter" not in kinds
    assert st.position is None


def test_exit_on_flip_or_small_avg():
    assert should_exit([0.0003, 0.0003, -0.0003]) is True
    assert should_exit([0.00004, 0.00004, 0.00004]) is True  # 0.4 bp < 0.5 bp
    assert should_exit([0.00006, 0.00006, 0.00006]) is False  # 0.6 bp holds
    assert should_exit([0.0003, 0.0003, 0.0003]) is False


def test_no_lookahead_collects_only_after_entry():
    """Third confirming print opens the book; that print is NOT collected."""
    st = FundingState(min_funding=0.0001, exit_funding=0.00003, trail=3)
    ts0 = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    collected = []
    entered_at = None
    for i, rate in enumerate([0.0001, 0.0001, 0.0001, 0.0001, 0.0001]):
        evs = st.on_print(
            "BTC-USDT-SWAP",
            ts0 + timedelta(hours=8 * i),
            rate,
            can_enter=True,
            notional=1000.0,
            one_way_cost_bps=17.0,
        )
        for e in evs:
            if e["kind"] == "funding_enter":
                entered_at = i
            if e["kind"] == "funding_accrual":
                collected.append(i)
    assert entered_at == 2
    assert collected == [3, 4]
    assert abs(st.position.collected - 0.20) < 1e-12


def test_exit_collects_the_exit_print_then_closes():
    st = FundingState(min_funding=0.0001, exit_funding=0.00003, trail=3)
    ts0 = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    # 3 positives → enter; then a flip window
    seq = [0.0001, 0.0001, 0.0001, 0.0001, -0.0001, -0.0001]
    exit_i = None
    last_accrual = None
    for i, rate in enumerate(seq):
        evs = st.on_print(
            "BTC-USDT-SWAP",
            ts0 + timedelta(hours=8 * i),
            rate,
            can_enter=True,
            notional=500.0,
            one_way_cost_bps=17.0,
        )
        for e in evs:
            if e["kind"] == "funding_accrual":
                last_accrual = i
            if e["kind"] == "funding_exit":
                exit_i = i
    # trailing 3 at i=4 is [0.0001, 0.0001, -0.0001] → flip → exit
    assert exit_i == 4
    assert last_accrual == 4
    assert st.position is None
    assert trailing_avg([0.0001, 0.0001, 0.0001]) == 0.0001


def test_anti_churn_24h():
    """After an exit, same symbol cannot re-enter until 24h has passed."""
    st = FundingState(min_funding=0.0003, exit_funding=0.00005, trail=3, cooldown_hours=24)
    ts0 = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    # 3 positives → enter at i=2; flip at i=3 → exit; then 3 more positives.
    # i=4 (t+32h, 8h after exit) and i=5 (16h) blocked; i=6 is exactly 24h → allowed.
    seq = [0.0003, 0.0003, 0.0003, -0.0003, 0.0003, 0.0003, 0.0003]
    enters, exits = [], []
    for i, rate in enumerate(seq):
        evs = st.on_print(
            "BTC-USDT-SWAP",
            ts0 + timedelta(hours=8 * i),
            rate,
            can_enter=True,
            notional=1000.0,
            one_way_cost_bps=6.0,
        )
        for e in evs:
            if e["kind"] == "funding_enter":
                enters.append(i)
            if e["kind"] == "funding_exit":
                exits.append(i)
    assert exits == [3]
    assert enters == [2, 6], enters
    assert st.position is not None


def test_idle_cash_pnl_formula():
    from stablebot.market.idle_yield import idle_cash_pnl

    # $1000 * 3.65% * 24h / (365*24) = $0.10
    assert abs(idle_cash_pnl(1000.0, 0.0365, 24.0) - 0.10) < 1e-12
    assert idle_cash_pnl(0.0, 0.03, 24.0) == 0.0
    assert idle_cash_pnl(1000.0, 0.0, 24.0) == 0.0
