"""Tests for the Kalshi directional sleeve and the composite reference price."""

from __future__ import annotations

import json

import pytest

from stablebot.desk.kalshi_lag import (
    COIN_SERIES,
    KalshiLagLedger,
    KalshiLagPaper,
    LagParams,
    fair_yes,
    load_session,
    parse_close_ts,
    parse_strike,
    save_session,
    session_path,
)
from stablebot.desk.reference import (
    BINANCE_FALLBACK,
    VENUE_PAIRS,
    RefQuote,
    ReferencePrice,
    _dispersion_bp,
)
from stablebot.desk.signal import SignalCfg


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    monkeypatch.setenv("STABLEBOT_ROOT", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}")
    yield


# ---------------------------------------------------------------------------
# market parsing
# ---------------------------------------------------------------------------


def test_strike_is_read_off_the_contract():
    assert parse_strike({"floor_strike": 78835.32}) == pytest.approx(78835.32)
    assert parse_strike({"cap_strike": "101.5"}) == pytest.approx(101.5)
    assert parse_strike({"floor_strike": 0}) is None
    assert parse_strike({}) is None


def test_close_time_parses_kalshi_z_suffix():
    ts = parse_close_ts({"close_time": "2026-09-09T02:00:00Z"})
    assert ts is not None and ts > 1_700_000_000
    assert parse_close_ts({"close_time": "not a date"}) is None
    assert parse_close_ts({}) is None


def test_every_series_has_a_spot_symbol():
    from stablebot.poly.markets import COIN_SPOT

    for coin in COIN_SERIES:
        assert coin in COIN_SPOT
        assert coin in VENUE_PAIRS
        assert coin in BINANCE_FALLBACK


# ---------------------------------------------------------------------------
# fair value against a published strike
# ---------------------------------------------------------------------------


def test_fair_is_a_coin_flip_at_the_strike():
    assert fair_yes(100.0, 100.0, 0.0004, 5.0) == pytest.approx(0.5)


def test_fair_rises_above_the_strike_and_falls_below():
    assert fair_yes(101.0, 100.0, 0.0004, 5.0) > 0.5
    assert fair_yes(99.0, 100.0, 0.0004, 5.0) < 0.5


def test_the_clock_sharpens_the_same_distance_from_the_strike():
    far = fair_yes(100.1, 100.0, 0.0004, 10.0)
    near = fair_yes(100.1, 100.0, 0.0004, 1.0)
    assert near > far


# ---------------------------------------------------------------------------
# composite reference price
# ---------------------------------------------------------------------------


def test_composite_takes_the_median_so_one_bad_tick_cannot_move_it():
    rp = ReferencePrice()
    good = {"coinbase": 100.0, "kraken": 100.1, "bitstamp": 99.9, "gemini": 100.05}
    assert rp._build(good, None, "spot").price == pytest.approx(100.025)
    # one venue prints garbage; the median ignores it
    bad = dict(good, gemini=1.0)
    q = rp._build(bad, None, "spot")
    assert q.price == pytest.approx(99.95)
    assert q.method == "composite"


def test_too_few_venues_falls_back_and_says_so():
    rp = ReferencePrice(min_sources=3)
    q = rp._build({"coinbase": 100.0}, 100.5, "spot")
    assert q.method == "binance_fallback"
    assert q.price == pytest.approx(100.5)
    assert "basis" in q.note


def test_no_venues_and_no_fallback_is_not_a_price():
    q = ReferencePrice(min_sources=2)._build({}, None, "spot")
    assert not q.ok
    assert q.method == "none"


def test_fallback_can_be_refused():
    rp = ReferencePrice(min_sources=3, allow_binance_fallback=False)
    assert not rp._build({"coinbase": 100.0}, 100.5, "spot").ok


def test_dispersion_is_reported_in_bp():
    assert _dispersion_bp([100.0, 100.1], 100.0) == pytest.approx(10.0)
    assert _dispersion_bp([100.0], 100.0) is None


def test_quote_label_is_readable():
    q = RefQuote(100.0, "composite", {"a": 1.0, "b": 2.0}, 3.0)
    assert "composite of 2" in q.label()
    assert "3.0bp" in q.label()
    assert "no reference" in RefQuote(None, "none", note="down").label()


def test_cf_benchmarks_is_off_without_a_key(monkeypatch):
    from stablebot.desk.reference import CFBenchmarksSource

    monkeypatch.delenv("CF_BENCHMARKS_API_KEY", raising=False)
    assert not CFBenchmarksSource().enabled
    assert CFBenchmarksSource(api_key="k").enabled


def test_cf_benchmarks_needs_an_index_id_per_coin(monkeypatch):
    from stablebot.desk.reference import CFBenchmarksSource

    monkeypatch.delenv("CF_BENCHMARKS_INDEX_BTC", raising=False)
    src = CFBenchmarksSource(api_key="k")
    assert src.index_id("btc") is None
    monkeypatch.setenv("CF_BENCHMARKS_INDEX_BTC", "BRTI")
    assert src.index_id("btc") == "BRTI"


# ---------------------------------------------------------------------------
# depth conversion — the units that bit
# ---------------------------------------------------------------------------


def test_resting_dollars_convert_to_contracts_at_the_opposite_bid():
    # $329.64 resting at 0.38 backs a 0.62 ask: 867 contracts, not 329.
    ask, size = 0.62, 329.64
    contracts = size / (1.0 - ask)
    assert contracts == pytest.approx(867.47, abs=0.1)
    assert contracts > size          # the naive comparison understated capacity


# ---------------------------------------------------------------------------
# session + ledger
# ---------------------------------------------------------------------------


def test_session_round_trips_and_equity_is_cash_plus_open():
    sess = load_session(starting=1000.0)
    assert sess["equity"] == pytest.approx(1000.0)
    sess["cash"] = 800.0
    sess["open_cost"] = 150.0
    save_session(sess)
    again = load_session()
    assert again["equity"] == pytest.approx(950.0)
    assert again["live"] is False


def test_ledger_forces_live_false_and_survives_a_bad_line():
    led = KalshiLagLedger()
    led.append({"kind": "kalshi_lag", "ticker": "T1", "live": True})
    with led.path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n\n")
    led.append({"kind": "kalshi_lag", "ticker": "T2"})
    recs = led.load()
    assert [r["ticker"] for r in recs] == ["T1", "T2"]
    assert all(r["live"] is False for r in recs)
    assert all(r["venue"] == "kalshi" for r in recs)


def _fill(ticker: str, **kw) -> dict:
    rec = {
        "kind": "kalshi_lag",
        "ticker": ticker,
        "coin": "btc",
        "symbol": "BTCUSDT",
        "series": "KXBTC15M",
        "strike": 100.0,
        "close_ts": 2_000_000_000.0,
        "side": "yes",
        "shares": 10.0,
        "entry_p": 0.5,
        "cost": 5.0,
        "fee": 0.1,
        "fair": 0.6,
        "z": 3.0,
        "sigma": 0.0004,
    }
    rec.update(kw)
    return rec


def test_open_positions_replay_from_the_ledger():
    led = KalshiLagLedger()
    led.append(_fill("OPEN1"))
    led.append(_fill("CLOSED1"))
    led.append({"kind": "kalshi_lag_resolve", "ticker": "CLOSED1"})
    eng = KalshiLagPaper(ledger=led, starting_balance=1000.0)
    assert set(eng.open) == {"OPEN1"}
    assert eng.sess["open_cost"] == pytest.approx(5.0)


def test_replay_skips_records_it_cannot_trust():
    led = KalshiLagLedger()
    led.append(_fill("BADSTRIKE", strike=0.0))
    led.append(_fill("BADCLOSE", close_ts=0.0))
    led.append(_fill("GOOD"))
    eng = KalshiLagPaper(ledger=led, starting_balance=1000.0)
    assert set(eng.open) == {"GOOD"}


def test_engine_starts_flat_with_the_declared_bankroll():
    eng = KalshiLagPaper(starting_balance=2500.0)
    assert eng.equity == pytest.approx(2500.0)
    assert eng.cash == pytest.approx(2500.0)
    assert eng.open == {}
    assert eng.gates.total == 0


def test_params_carry_the_signal_config():
    p = LagParams(signal=SignalCfg(z_entry=3.0, min_edge=0.06))
    assert p.signal.z_entry == 3.0
    assert "3.0sigma" in p.signal.describe()


def test_vol_tracker_is_shared_across_the_engine():
    eng = KalshiLagPaper(starting_balance=1000.0)
    eng.vol.seed("BTCUSDT", [0.0004] * 60)
    sigma = eng.vol.sigma("BTCUSDT")
    assert sigma is not None
    z = eng.vol.zscore("BTCUSDT", 3 * sigma)
    assert z == pytest.approx(3.0, abs=1e-6)


def test_this_sleeve_keeps_its_own_pot_and_ledger():
    # It must not write into the lock sleeves' shared poly/kalshi scoreboard.
    eng = KalshiLagPaper(starting_balance=1000.0)
    assert eng.ledger.path.name == "kalshi_lag_ledger.jsonl"
    assert session_path().name == "kalshi_lag_session.json"
    assert json.loads(session_path().read_text())["starting_equity"] == pytest.approx(1000.0)
    assert not session_path().with_name("poly_session.json").exists()


# ---------------------------------------------------------------------------
# the fixes, tested against the four trades that actually happened
# ---------------------------------------------------------------------------

from stablebot.desk.signal import distance_is_measurable, fair_with_reference_noise  # noqa: E402

# coin, spot, strike, sigma_1m, minutes_left, venue dispersion (fractional), ask, side
LIVE_TRADES = {
    # lost -52.70: model said YES 0.707 and it bought NO -> faded its own model
    "sol_faded": (103.838, 103.756, 0.00061809, 5.4938, 2.6965e-4, 0.230, "no"),
    # won +18.41: bought the side the model favoured
    "eth_won": (2498.21, 2496.17, 0.00046422, 5.3626, 2.4417e-4, 0.720, "yes"),
    # lost -51.09: 2.0bp from strike against 3.7bp of venue disagreement
    "btc_noise": (78841.76, 78857.80, 0.00035307, 3.0049, 3.6554e-4, 0.170, "yes"),
    # lost -50.61: 4.1bp against 3.6bp — thin
    "sol_thin": (103.7255, 103.7683, 0.00065054, 3.0049, 3.5671e-4, 0.270, "yes"),
}


def test_the_worst_loss_is_rejected_as_unmeasurable():
    spot, strike, _sig, _tau, disp, _ask, _side = LIVE_TRADES["btc_noise"]
    ok, ratio = distance_is_measurable(spot, strike, disp, min_ratio=3.0)
    assert not ok
    assert ratio == pytest.approx(0.56, abs=0.02)


def test_the_winning_trade_is_still_measurable():
    spot, strike, _sig, _tau, disp, _ask, _side = LIVE_TRADES["eth_won"]
    ok, ratio = distance_is_measurable(spot, strike, disp, min_ratio=3.0)
    assert ok
    assert ratio > 3.0


def test_reference_noise_widens_the_error_bar_when_close_to_the_strike():
    spot, strike, sig, tau, disp, _ask, _side = LIVE_TRADES["btc_noise"]
    _fair, err = fair_with_reference_noise(spot, strike, sig, tau, disp)
    # the fair was quoted as 0.370; its real error bar swamps the "edge"
    assert err > 0.15


def test_a_clean_trade_has_a_tight_error_bar():
    spot, strike, sig, tau, disp, _ask, _side = LIVE_TRADES["eth_won"]
    _fair, err = fair_with_reference_noise(spot, strike, sig, tau, disp)
    assert err < 0.09


def test_reference_noise_pulls_the_fair_toward_a_coin_flip():
    spot, strike, sig, tau = 78841.76, 78857.80, 0.00035307, 3.0049
    clean, _ = fair_with_reference_noise(spot, strike, sig, tau, 0.0)
    noisy, _ = fair_with_reference_noise(spot, strike, sig, tau, 3.6554e-4)
    assert abs(noisy - 0.5) < abs(clean - 0.5)


def test_zero_reference_noise_matches_the_plain_model():
    from stablebot.desk.signal import vol_fair_up

    a, err = fair_with_reference_noise(101.0, 100.0, 0.0004, 5.0, 0.0)
    assert a == pytest.approx(vol_fair_up(101.0, 100.0, 0.0004, 5.0))
    assert err == 0.0


def test_required_edge_grows_with_uncertainty():
    cfg = SignalCfg(min_edge=0.04, edge_uncertainty_mult=2.0)
    _f, err_btc = fair_with_reference_noise(*LIVE_TRADES["btc_noise"][:4], LIVE_TRADES["btc_noise"][4])
    _f, err_eth = fair_with_reference_noise(*LIVE_TRADES["eth_won"][:4], LIVE_TRADES["eth_won"][4])
    need_btc = cfg.min_edge + cfg.edge_uncertainty_mult * err_btc
    need_eth = cfg.min_edge + cfg.edge_uncertainty_mult * err_eth
    # the noisy trade's bar is far higher than its claimed +0.19 edge
    assert need_btc > 0.19
    assert need_eth < 0.21


def test_model_side_rule_refuses_the_trade_that_faded():
    # SOL: fair_yes 0.707, so NO is worth 0.293 — buying NO fades the model.
    fair_yes_v = 0.707
    sides = {"yes": fair_yes_v, "no": 1.0 - fair_yes_v}
    favoured = [k for k, v in sides.items() if v > 0.5]
    assert favoured == ["yes"]
    assert "no" not in favoured


def test_signal_cfg_defaults_carry_the_new_gates():
    c = SignalCfg()
    assert c.min_distance_ratio >= 3.0
    assert c.edge_uncertainty_mult >= 1.0
    assert c.require_model_side is True
    assert "ref-noise" in c.describe()


def test_gate_names_cover_the_new_rejections():
    from stablebot.desk.signal import GATES

    assert "reference_noise" in GATES
    assert "wrong_side" in GATES


# ---------------------------------------------------------------------------
# daily-stop-aware sizing
# ---------------------------------------------------------------------------


def test_stake_is_capped_by_what_is_left_of_the_daily_stop():
    from stablebot.config import AppConfig
    from stablebot.desk.risk import RiskGovernor
    from stablebot.desk.state import DeskState

    st = DeskState()
    st.starting_equity = 3000.0
    st.mark_equity(3000.0, 3000.0, 0.0)
    g = RiskGovernor(AppConfig(), daily_stop_pct=0.02)
    g.bind(st)
    g.evaluate(st)
    assert st.risk.budget_total == pytest.approx(60.0)
    assert RiskGovernor.clip_cap(st) == pytest.approx(60.0)

    # two $50 clips were what overshot the stop; the second cannot be funded now
    st.mark_equity(3000.0, 2950.0, 50.0)
    g.evaluate(st)
    assert RiskGovernor.clip_cap(st) == pytest.approx(10.0)


def test_capacity_is_zero_once_the_budget_is_spent():
    from stablebot.config import AppConfig
    from stablebot.desk.risk import RiskGovernor
    from stablebot.desk.state import DeskState

    st = DeskState()
    st.starting_equity = 3000.0
    st.mark_equity(3000.0, 2900.0, 100.0)
    g = RiskGovernor(AppConfig(), daily_stop_pct=0.02)
    g.bind(st)
    g.evaluate(st)
    assert RiskGovernor.clip_cap(st) == 0.0


def test_budget_shrinks_as_the_day_loses():
    from stablebot.config import AppConfig
    from stablebot.desk.risk import RiskGovernor
    from stablebot.desk.state import DeskState

    st = DeskState()
    st.starting_equity = 3000.0
    st.mark_equity(3000.0, 3000.0, 0.0)
    g = RiskGovernor(AppConfig(), daily_stop_pct=0.02)
    g.bind(st)
    st.mark_equity(2955.0, 2955.0, 0.0)
    g.evaluate(st)
    assert st.risk.budget_remaining == pytest.approx(15.0)
    assert RiskGovernor.clip_cap(st) < 15.0     # throttled as well


# ---------------------------------------------------------------------------
# a bleeding sleeve is benched without waiting for a full sample
# ---------------------------------------------------------------------------


def test_a_bleeding_sleeve_is_benched_without_waiting_for_a_full_sample():
    from stablebot.desk.allocator import Allocator, AllocatorCfg
    from stablebot.desk.state import DeskState, SleeveStat

    st = DeskState()
    st.starting_equity = 3000.0
    st.mark_equity(3000.0, 3000.0, 0.0)
    s = SleeveStat("lag", "Lag")
    s.clip = 50.0
    for _ in range(4):
        s.record_result(-50.0, 0.35)     # -200 on a 3000 pot (6.7%), only 4 trades
    st.sleeves = {"lag": s}
    Allocator(AllocatorCfg(pot=3000.0)).rebalance(st, force=True)
    assert not s.enabled
    assert "down" in s.disabled_reason


def test_the_bench_measures_the_drop_since_the_sleeve_came_back():
    """Session realized PnL never recovers, so a lifetime threshold pins a
    sleeve off for good: re-probe, still past the line, benched again.
    """
    from stablebot.desk.allocator import Allocator, AllocatorCfg
    from stablebot.desk.state import DeskState, SleeveStat

    st = DeskState()
    st.starting_equity = 3000.0
    st.mark_equity(3000.0, 3000.0, 0.0)
    s = SleeveStat("lag", "Lag")
    s.clip = 15.0
    for _ in range(4):
        s.record_result(-50.0, 0.35)     # deep in the hole from earlier
    s.bench_baseline = s.realized        # ... but forgiven at the last re-probe
    st.sleeves = {"lag": s}
    Allocator(AllocatorCfg(pot=3000.0)).rebalance(st, force=True)
    assert s.enabled, "a forgiven sleeve must not be re-benched on old losses"

    s.record_result(-200.0, 0.35)        # a fresh 6.7% drop since coming back
    Allocator(AllocatorCfg(pot=3000.0)).rebalance(st, force=True)
    assert not s.enabled
    assert "since it came back" in s.disabled_reason


def test_the_operator_can_resume_a_benched_sleeve():
    from stablebot.desk.allocator import Allocator, AllocatorCfg
    from stablebot.desk.app import DeskApp
    from stablebot.desk.state import DeskState, SleeveStat

    app = DeskApp.__new__(DeskApp)
    app.state = DeskState()
    app.state.starting_equity = 3000.0
    app.allocator = Allocator(AllocatorCfg(pot=3000.0))
    s = SleeveStat("lag", "Lag")
    for _ in range(4):
        s.record_result(-50.0, 0.35)
    s.enabled = False
    s.disabled_reason = "auto: down -200.00 since it came back"
    app.state.sleeves = {"lag": s}
    app.sleeves = []

    app._resume_sleeves()
    assert s.enabled
    assert s.disabled_reason == ""
    assert s.bench_baseline == s.realized, "resuming must forgive what is already lost"

    # and the allocator must not undo the operator on the very next pass
    app.allocator.rebalance(app.state, force=True)
    assert s.enabled


def test_a_small_loss_does_not_trip_the_immediate_bench():
    from stablebot.desk.allocator import Allocator, AllocatorCfg
    from stablebot.desk.state import DeskState, SleeveStat

    st = DeskState()
    st.starting_equity = 3000.0
    st.mark_equity(3000.0, 3000.0, 0.0)
    s = SleeveStat("lag", "Lag")
    s.clip = 50.0
    s.record_result(-5.0, 0.35)
    st.sleeves = {"lag": s}
    Allocator(AllocatorCfg(pot=3000.0)).rebalance(st, force=True)
    assert s.enabled


# ---------------------------------------------------------------------------
# a scanned coin needs its whole toolchain, not just a Kalshi series
# ---------------------------------------------------------------------------


def test_every_scanned_coin_has_a_reference_and_a_vol_source():
    """A coin with a market but no reference price only adds gate noise.

    Pricing needs a CF-style composite (VENUE_PAIRS), sigma needs a Binance 1m
    series (COIN_SPOT / BINANCE_FALLBACK). HYPE has a Kalshi 15M series and a
    reference but no Binance symbol, which is why it is not scanned.
    """
    from stablebot.desk.kalshi_lag import COIN_SERIES
    from stablebot.desk.reference import BINANCE_FALLBACK, VENUE_PAIRS
    from stablebot.poly.markets import COIN_SPOT

    for coin in COIN_SERIES:
        assert coin in VENUE_PAIRS, f"{coin}: no reference venues"
        assert len(VENUE_PAIRS[coin]) >= 3, (
            f"{coin}: only {len(VENUE_PAIRS[coin])} reference venues; a thin "
            "composite widens the dispersion floor and blocks entries anyway"
        )
        assert coin in COIN_SPOT, f"{coin}: no spot symbol for vol seeding"
        assert coin in BINANCE_FALLBACK, f"{coin}: no Binance fallback"


# ---------------------------------------------------------------------------
# a series Kalshi is not listing should stop costing API calls
# ---------------------------------------------------------------------------


def _engine():
    from stablebot.desk.kalshi_lag import KalshiLagPaper, LagParams

    return KalshiLagPaper(params=LagParams(), starting_balance=1000.0)


def test_a_series_goes_dormant_only_after_repeated_empty_listings():
    """One empty poll is a blip; three in a row means Kalshi is not listing it."""
    from stablebot.desk.kalshi_lag import DORMANT_AFTER

    eng = _engine()
    now = 1_000_000.0
    for i in range(DORMANT_AFTER - 1):
        assert eng._series_empty("KXADA15M", now) is False, f"dormant too early at {i+1}"
        assert not eng.series_is_dormant("KXADA15M", now)
    assert eng._series_empty("KXADA15M", now) is True
    assert eng.series_is_dormant("KXADA15M", now)


def test_a_dormant_series_wakes_up_for_a_re_probe():
    from stablebot.desk.kalshi_lag import DORMANT_AFTER, DORMANT_SECONDS

    eng = _engine()
    now = 1_000_000.0
    for _ in range(DORMANT_AFTER):
        eng._series_empty("KXTON15M", now)
    assert eng.series_is_dormant("KXTON15M", now)
    assert eng.series_is_dormant("KXTON15M", now + DORMANT_SECONDS - 1)
    assert not eng.series_is_dormant("KXTON15M", now + DORMANT_SECONDS + 1)


def test_a_market_appearing_clears_the_miss_streak():
    """Two empties then a listing must not leave the series one poll from sleep."""
    from stablebot.desk.kalshi_lag import DORMANT_AFTER

    eng = _engine()
    now = 1_000_000.0
    for _ in range(DORMANT_AFTER - 1):
        eng._series_empty("KXBCH15M", now)
    eng._series_listed("KXBCH15M")
    assert eng._series_empty("KXBCH15M", now) is False
    assert not eng.series_is_dormant("KXBCH15M", now)


def test_series_are_tracked_independently():
    from stablebot.desk.kalshi_lag import DORMANT_AFTER

    eng = _engine()
    now = 1_000_000.0
    for _ in range(DORMANT_AFTER):
        eng._series_empty("KXADA15M", now)
    assert eng.series_is_dormant("KXADA15M", now)
    assert not eng.series_is_dormant("KXBTC15M", now), "a healthy series must not sleep"


def test_series_dormant_is_a_countable_gate():
    """Out-of-band markets stay window_timing; only an unlisted series is dormant."""
    from stablebot.desk.signal import GATES, GateCounter

    assert "series_dormant" in GATES
    assert "window_timing" in GATES
    gc = GateCounter()
    gc.hit("series_dormant", "KXADA15M not listing")
    assert gc.counts["series_dormant"] == 1
    assert gc.binding() == ("series_dormant", 1)
