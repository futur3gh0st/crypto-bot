"""Tests for the trading desk: signal model, risk governor, allocator, view."""

from __future__ import annotations

import json
import math
import time

import pytest

from stablebot.config import AppConfig
from stablebot.desk.allocator import MIN_SAMPLE, Allocator, AllocatorCfg
from stablebot.desk.health import SLEEVE_VENUES, VenueHealth, sleeve_blockers
from stablebot.desk.risk import RiskGovernor, clear_halt, halt_path, raise_halt
from stablebot.desk.signal import (
    GateCounter,
    SignalCfg,
    VolTracker,
    implied_sigma_from_scale,
    norm_cdf,
    vol_fair_up,
)
from stablebot.desk.state import BookRow, DeskState, PositionRow, SleeveStat, TapeEvent, utcnow


# ---------------------------------------------------------------------------
# signal model
# ---------------------------------------------------------------------------


def test_norm_cdf_known_points():
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(1.0) == pytest.approx(0.8413, abs=1e-4)
    assert norm_cdf(-1.96) == pytest.approx(0.025, abs=1e-3)


def test_vol_fair_at_the_money_is_a_coin_flip():
    assert vol_fair_up(100.0, 100.0, 0.0004, 4.0) == pytest.approx(0.5)


def test_vol_fair_rises_with_the_move_and_is_symmetric():
    up = vol_fair_up(100.5, 100.0, 0.0004, 4.0)
    down = vol_fair_up(100.0, 100.5, 0.0004, 4.0)
    assert up > 0.5 > down
    assert up + down == pytest.approx(1.0, abs=1e-9)


def test_same_move_is_worth_more_with_less_time_left():
    early = vol_fair_up(100.1, 100.0, 0.0004, 4.0)
    late = vol_fair_up(100.1, 100.0, 0.0004, 0.5)
    assert late > early


def test_same_move_is_worth_less_on_a_more_volatile_coin():
    calm = vol_fair_up(100.1, 100.0, 0.0004, 4.0)
    wild = vol_fair_up(100.1, 100.0, 0.0012, 4.0)
    assert calm > wild


def test_vol_fair_is_clipped_and_validates_input():
    assert vol_fair_up(200.0, 100.0, 0.0004, 4.0) <= 0.98
    assert vol_fair_up(50.0, 100.0, 0.0004, 4.0) >= 0.02
    with pytest.raises(ValueError):
        vol_fair_up(0.0, 100.0, 0.0004, 4.0)
    with pytest.raises(ValueError):
        vol_fair_up(100.0, 100.0, 0.0, 4.0)


def test_expiry_does_not_divide_by_zero():
    assert 0.0 < vol_fair_up(100.01, 100.0, 0.0004, 0.0) <= 0.98


def test_crude_scale_implies_an_absurdly_wide_window_sigma():
    # scale=25 assumes ~1.6% of movement per window; BTC 5m is nearer 0.09%.
    assert implied_sigma_from_scale(25.0) == pytest.approx(0.01596, abs=1e-4)


def test_vol_tracker_needs_a_sample_before_it_reports():
    vt = VolTracker(halflife=50)
    assert vt.sigma("X") is None
    vt.seed("X", [0.001] * 5)
    assert vt.sigma("X") is None          # still under MIN_BARS
    vt.seed("X", [0.001] * 40)
    assert vt.sigma("X") == pytest.approx(0.001, abs=2e-4)


def test_vol_tracker_seeds_from_closes_and_scores_z():
    vt = VolTracker(halflife=50)
    closes = [100.0 * math.exp(0.0005 * (-1) ** i) for i in range(60)]
    sigma = vt.seed_from_closes("Y", closes)
    assert sigma is not None and sigma > 0
    z = vt.zscore("Y", 3 * sigma)
    assert z == pytest.approx(3.0, abs=1e-6)


def test_vol_tracker_tracks_a_regime_change():
    vt = VolTracker(halflife=20)
    vt.seed("Z", [0.0002] * 100)
    calm = vt.sigma("Z")
    vt.seed("Z", [0.004] * 100)
    assert vt.sigma("Z") > calm * 5


# ---------------------------------------------------------------------------
# gate diagnostics
# ---------------------------------------------------------------------------


def test_gate_counter_names_the_binding_constraint():
    gc = GateCounter()
    for _ in range(30):
        gc.hit("no_signal", "z too small")
    for _ in range(5):
        gc.hit("edge")
    gc.hit("fired")
    assert gc.binding() == ("no_signal", 30)
    assert gc.total == 36
    assert gc.top(2)[0][0] == "no_signal"
    assert gc.last_detail["no_signal"] == "z too small"


def test_gate_counter_ignores_fired_when_picking_the_blocker():
    gc = GateCounter()
    for _ in range(50):
        gc.hit("fired")
    gc.hit("edge")
    assert gc.binding() == ("edge", 1)


def test_gate_counter_with_nothing_recorded():
    assert GateCounter().binding() is None


def test_signal_cfg_describes_itself():
    assert "sigma" in SignalCfg().describe()


# ---------------------------------------------------------------------------
# desk state
# ---------------------------------------------------------------------------


def _state(equity: float = 1000.0) -> DeskState:
    st = DeskState()
    st.starting_equity = 1000.0
    st.day_start_equity = 1000.0
    st.mark_equity(equity, equity, 0.0)
    return st


def test_state_pnl_math():
    st = _state(1050.0)
    assert st.session_pnl == pytest.approx(50.0)
    assert st.session_pnl_pct == pytest.approx(0.05)
    assert st.day_pnl == pytest.approx(50.0)


def test_day_pnl_is_zero_before_the_day_is_based():
    st = DeskState()
    st.starting_equity = 1000.0
    st.mark_equity(1000.0, 1000.0, 0.0)
    assert st.day_pnl == 0.0


def test_drawdown_is_measured_from_the_peak():
    st = _state(1000.0)
    st.mark_equity(1200.0, 1200.0, 0.0)
    st.mark_equity(1080.0, 1080.0, 0.0)
    assert st.peak_equity == pytest.approx(1200.0)
    assert st.drawdown == pytest.approx(-0.10)


def test_set_book_replaces_only_that_sleeve():
    st = _state()
    st.set_book("a", [BookRow("a", "BTC", "poly", "5m", 1.0, None, None, None, 0.05, "ARMED")])
    st.set_book("b", [BookRow("b", "ETH", "kalshi", "15m", 2.0, None, None, None, 0.01, "WATCH")])
    st.set_book("a", [BookRow("a", "SOL", "poly", "5m", 3.0, None, None, None, 0.02, "WATCH")])
    syms = {r.symbol for r in st.book}
    assert syms == {"SOL", "ETH"}


def test_book_sorts_best_edge_first():
    st = _state()
    st.set_book(
        "a",
        [
            BookRow("a", "LOW", "poly", "5m", 1.0, None, None, None, 0.01, "WATCH"),
            BookRow("a", "HIGH", "poly", "5m", 1.0, None, None, None, 0.09, "ARMED"),
            BookRow("a", "NONE", "poly", "5m", 1.0, None, None, None, None, "COLD"),
        ],
    )
    assert [r.symbol for r in st.book] == ["HIGH", "LOW", "NONE"]


def test_position_unrealized_needs_a_mark():
    p = PositionRow("a", "BTC", "UP", 10.0, 0.60, 6.0, None, mark=None)
    assert p.unrealized is None
    p.mark = 0.70
    assert p.unrealized == pytest.approx(1.0)


def test_sleeve_stat_records_wins_losses_and_scratches():
    s = SleeveStat("x", "X")
    s.record_result(1.0, 0.55)
    s.record_result(-0.6, 0.60)
    s.record_result(0.0, 0.50, scratched=True)
    assert (s.wins, s.losses, s.scratches) == (1, 1, 1)
    assert s.realized == pytest.approx(0.4)
    assert s.win_rate == pytest.approx(0.5)
    assert s.expectancy == pytest.approx(0.4 / 3)
    assert s.breakeven_hit_rate == pytest.approx(0.55)


def test_flash_expires():
    st = _state()
    st.flash("hi", seconds=0.0)
    assert st.flash_active() == ""


def test_tape_event_win_flag():
    ev = TapeEvent(utcnow(), "S", "resolve", "BTC", "UP", 1.0, 0.5, 0.25)
    assert ev.won is True
    assert TapeEvent(utcnow(), "S", "fill", "BTC", "UP", 1.0, 0.5, None).won is None


# ---------------------------------------------------------------------------
# risk governor
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_halt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("STABLEBOT_ROOT", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}")
    yield
    if halt_path().exists():
        clear_halt()


def _gov(daily_stop: float = 0.02, max_dd: float = 0.10) -> RiskGovernor:
    return RiskGovernor(AppConfig(), daily_stop_pct=daily_stop, max_drawdown_pct=max_dd)


def test_governor_runs_when_flat():
    st = _state()
    g = _gov()
    g.bind(st)
    assert g.evaluate(st) == "RUNNING"
    assert RiskGovernor.may_trade(st)


def test_governor_throttles_before_it_stops():
    st = _state()
    g = _gov(daily_stop=0.02)
    g.bind(st)
    st.mark_equity(985.0, 985.0, 0.0)   # -1.5%, past the halfway trigger
    assert g.evaluate(st) == "THROTTLED"
    assert 0.25 <= st.risk.throttle_mult < 1.0
    assert RiskGovernor.may_trade(st)


def test_governor_halts_on_the_daily_stop():
    st = _state()
    g = _gov(daily_stop=0.02)
    g.bind(st)
    st.mark_equity(975.0, 975.0, 0.0)
    assert g.evaluate(st) == "HALTED"
    assert st.risk.throttle_mult == 0.0
    assert not RiskGovernor.may_trade(st)
    assert "daily stop" in st.risk.reason


def test_governor_halts_on_drawdown_from_the_peak():
    st = _state()
    g = _gov(daily_stop=0.50, max_dd=0.10)
    g.bind(st)
    st.mark_equity(2000.0, 2000.0, 0.0)
    st.mark_equity(1700.0, 1700.0, 0.0)   # -15% from peak, day still up
    assert g.evaluate(st) == "HALTED"
    assert "drawdown" in st.risk.reason


def test_pause_blocks_trading_without_a_halt_file():
    st = _state()
    g = _gov()
    g.bind(st)
    st.paused = True
    assert g.evaluate(st) == "PAUSED"
    assert not RiskGovernor.may_trade(st)
    assert not halt_path().exists()


def test_halt_file_stops_everything_and_clears():
    st = _state()
    g = _gov()
    g.bind(st)
    raise_halt("test")
    assert g.evaluate(st) == "HALTED"
    assert clear_halt()
    assert g.evaluate(st) == "RUNNING"


def test_day_roll_rebases_the_daily_stop():
    st = _state()
    g = _gov()
    g.bind(st)
    st.mark_equity(975.0, 975.0, 0.0)
    assert g.evaluate(st) == "HALTED"
    st.risk.day_key = "1999-01-01"          # pretend a day boundary passed
    assert g.evaluate(st) == "RUNNING"
    assert st.day_start_equity == pytest.approx(975.0)


# ---------------------------------------------------------------------------
# allocator
# ---------------------------------------------------------------------------


def _stat(name: str, results: list[float], clip: float = 10.0) -> SleeveStat:
    s = SleeveStat(name, name.title())
    s.clip = clip
    for r in results:
        s.record_result(r, 0.55)
    return s


def test_unproven_sleeves_share_the_pot():
    st = _state()
    st.sleeves = {"a": _stat("a", []), "b": _stat("b", [])}
    Allocator(AllocatorCfg(pot=1000)).rebalance(st, force=True)
    assert st.sleeves["a"].allocation > 0
    assert st.sleeves["b"].allocation > 0
    total = sum(s.allocation for s in st.sleeves.values())
    assert total == pytest.approx(1000.0, rel=1e-6)


def test_a_losing_sleeve_gets_benched():
    st = _state()
    st.sleeves = {"bad": _stat("bad", [-1.0] * MIN_SAMPLE)}
    Allocator(AllocatorCfg(pot=1000)).rebalance(st, force=True)
    bad = st.sleeves["bad"]
    assert not bad.enabled
    assert bad.disabled_reason.startswith("auto:")
    assert bad.allocation == 0.0


def test_benching_is_off_when_autopilot_is_off():
    st = _state()
    st.autopilot = False
    st.sleeves = {"bad": _stat("bad", [-1.0] * MIN_SAMPLE)}
    Allocator(AllocatorCfg(pot=1000)).rebalance(st, force=True)
    assert st.sleeves["bad"].enabled


def test_a_benched_sleeve_re_probes_after_its_timeout():
    st = _state()
    st.sleeves = {"bad": _stat("bad", [-1.0] * MIN_SAMPLE)}
    a = Allocator(AllocatorCfg(pot=1000))
    a.rebalance(st, force=True)
    bad = st.sleeves["bad"]
    assert not bad.enabled
    bad.recent.clear()                 # its history aged out
    bad.probe_at = time.monotonic() - 1
    a.rebalance(st, force=True)
    assert bad.enabled


def test_the_winner_is_given_more_than_the_loser():
    st = _state()
    st.sleeves = {
        "good": _stat("good", [2.0] * MIN_SAMPLE),
        "meh": _stat("meh", [0.05] * MIN_SAMPLE),
    }
    Allocator(AllocatorCfg(pot=1000)).rebalance(st, force=True)
    assert st.sleeves["good"].allocation > st.sleeves["meh"].allocation


def test_no_sleeve_exceeds_its_cap():
    st = _state()
    st.sleeves = {
        "good": _stat("good", [5.0] * MIN_SAMPLE),
        "ok": _stat("ok", [0.01] * MIN_SAMPLE),
    }
    cfg = AllocatorCfg(pot=1000, max_sleeve_frac=0.6)
    Allocator(cfg).rebalance(st, force=True)
    for s in st.sleeves.values():
        assert s.alloc_frac <= 0.6 + 1e-9


def test_throttling_shrinks_every_allocation():
    st = _state()
    st.sleeves = {"a": _stat("a", [])}
    a = Allocator(AllocatorCfg(pot=1000))
    a.rebalance(st, force=True)
    full = st.sleeves["a"].allocation
    st.risk.throttle_mult = 0.5
    a.rebalance(st, force=True)
    assert st.sleeves["a"].allocation == pytest.approx(full * 0.5)


def test_min_edge_tightens_when_the_hit_rate_is_below_break_even():
    st = _state()
    s = SleeveStat("x", "X")
    s.params["min_edge"] = 0.04
    for _ in range(6):
        s.record_result(-0.6, 0.60)
    for _ in range(4):
        s.record_result(0.4, 0.60)      # 40% hit against a 0.60 break-even
    st.sleeves = {"x": s}
    changes = Allocator().tune(st, force=True)
    assert s.params["min_edge"] > 0.04
    assert changes


def test_min_edge_relaxes_when_the_hit_rate_is_comfortably_above():
    st = _state()
    s = SleeveStat("x", "X")
    s.params["min_edge"] = 0.10
    for _ in range(9):
        s.record_result(0.5, 0.50)
    s.record_result(-0.5, 0.50)          # 90% hit against a 0.50 break-even
    st.sleeves = {"x": s}
    Allocator().tune(st, force=True)
    assert s.params["min_edge"] < 0.10


def test_tuning_holds_inside_the_dead_band():
    st = _state()
    s = SleeveStat("x", "X")
    s.params["min_edge"] = 0.05
    for _ in range(6):
        s.record_result(0.4, 0.55)
    for _ in range(4):
        s.record_result(-0.5, 0.55)     # 60% vs 0.55 break-even: inside the band
    st.sleeves = {"x": s}
    Allocator().tune(st, force=True)
    assert s.params["min_edge"] == pytest.approx(0.05)


def test_tuning_waits_for_a_sample():
    st = _state()
    s = SleeveStat("x", "X")
    s.params["min_edge"] = 0.04
    s.record_result(-1.0, 0.6)
    st.sleeves = {"x": s}
    Allocator().tune(st, force=True)
    assert s.params["min_edge"] == pytest.approx(0.04)
    assert "sampling" in s.tuning


def test_an_idle_sleeve_relaxes_its_z_threshold():
    st = _state()
    s = SleeveStat("x", "X")
    s.params["z_entry"] = 2.5
    st.sleeves = {"x": s}
    gc = GateCounter()
    for _ in range(300):
        gc.hit("no_signal")
    st.gates["x"] = gc
    Allocator().tune(st, force=True)
    assert s.params["z_entry"] < 2.5


def test_an_idle_sleeve_blocked_on_edge_does_not_relax_z():
    st = _state()
    s = SleeveStat("x", "X")
    s.params["z_entry"] = 2.5
    st.sleeves = {"x": s}
    gc = GateCounter()
    for _ in range(300):
        gc.hit("edge")
    st.gates["x"] = gc
    Allocator().tune(st, force=True)
    assert s.params["z_entry"] == pytest.approx(2.5)


def test_a_losing_sleeve_does_not_relax_its_z_threshold():
    """The pot is down, so loosening the gate buys more of a losing signal."""
    st = _state()
    s = SleeveStat("x", "X")
    s.params["z_entry"] = 2.5
    s.realized = -135.98
    st.sleeves = {"x": s}
    gc = GateCounter()
    for _ in range(300):
        gc.hit("no_signal")
    st.gates["x"] = gc
    Allocator().tune(st, force=True)
    assert s.params["z_entry"] == pytest.approx(2.5)


def test_a_sleeve_that_traded_before_the_restart_is_not_idle():
    """trades is replayed from the ledger, so prior fills still count."""
    st = _state()
    s = SleeveStat("x", "X")
    s.params["z_entry"] = 2.5
    s.trades = 4
    st.sleeves = {"x": s}
    gc = GateCounter()
    for _ in range(300):
        gc.hit("no_signal")
    st.gates["x"] = gc
    Allocator().tune(st, force=True)
    assert s.params["z_entry"] == pytest.approx(2.5)


def test_seed_stat_from_ledger_rebuilds_the_scoreboard(tmp_path):
    from stablebot.desk.sleeves import seed_stat_from_ledger

    led = tmp_path / "kalshi_lag_ledger.jsonl"
    led.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"kind": "kalshi_lag", "ticker": "A"},
                {"kind": "kalshi_lag", "ticker": "B"},
                {
                    "kind": "kalshi_lag_resolve",
                    "ticker": "A",
                    "pnl_after_fee": -52.7,
                    "entry_p": 0.23,
                },
                {
                    "kind": "kalshi_lag_resolve",
                    "ticker": "B",
                    "pnl_after_fee": 18.41,
                    "entry_p": 0.72,
                },
                # a duplicate resolve must not be counted twice
                {
                    "kind": "kalshi_lag_resolve",
                    "ticker": "B",
                    "pnl_after_fee": 18.41,
                    "entry_p": 0.72,
                },
            ]
        ),
        encoding="utf-8",
    )
    s = SleeveStat("kalshi_lag", "Kalshi Lag")
    seen = seed_stat_from_ledger(s, led, "kalshi_lag", "ticker")
    assert s.trades == 2
    assert s.wins == 1
    assert s.losses == 1
    assert s.realized == pytest.approx(-34.29)
    assert seen == {"A", "B"}


def test_seed_stat_from_ledger_tolerates_a_missing_file(tmp_path):
    from stablebot.desk.sleeves import seed_stat_from_ledger

    s = SleeveStat("kalshi_lag", "Kalshi Lag")
    assert seed_stat_from_ledger(s, tmp_path / "nope.jsonl", "kalshi_lag", "ticker") == set()
    assert s.trades == 0


def test_rebalance_respects_its_own_clock():
    st = _state()
    st.sleeves = {"a": _stat("a", [])}
    a = Allocator(AllocatorCfg(pot=1000, rebalance_every=999))
    assert a.rebalance(st, force=True)
    assert not a.rebalance(st)


# ---------------------------------------------------------------------------
# venue health
# ---------------------------------------------------------------------------


def test_sleeve_blockers_lists_only_the_venues_that_matter():
    health = {
        "binance": VenueHealth("binance", "Binance", True, "ok"),
        "poly": VenueHealth("poly", "Poly", False, "blocked", "resolves elsewhere"),
        "poly_clob": VenueHealth("poly_clob", "Poly CLOB", False, "blocked"),
        "kalshi": VenueHealth("kalshi", "Kalshi", True, "ok"),
    }
    assert [b.venue for b in sleeve_blockers("kalshi_lock", health)] == []
    assert {b.venue for b in sleeve_blockers("poly_lock", health)} == {"poly", "poly_clob"}
    assert health["poly"].blocked


def test_every_sleeve_declares_its_venues():
    for name in ("spot_lag", "poly_lock", "kalshi_lock"):
        assert SLEEVE_VENUES[name]


# ---------------------------------------------------------------------------
# the view renders
# ---------------------------------------------------------------------------


def test_desk_renders_with_a_populated_state():
    from rich.console import Console

    from stablebot.desk import render

    st = _state(1042.0)
    st.sleeves = {"a": _stat("a", [0.5, -0.2, 0.3])}
    st.gates["a"] = GateCounter()
    st.gates["a"].hit("no_signal", "z too small")
    st.venues = {"poly": VenueHealth("poly", "Poly", False, "blocked", "resolves elsewhere")}
    st.set_book("a", [BookRow("a", "BTC", "poly", "5m", 1.2, 0.5, 0.55, 0.7, 0.09, "ARMED", "hot")])
    st.set_positions("a", [PositionRow("a", "BTC UP", "UP", 20.0, 0.55, 11.0, 90.0)])
    st.push_tape(TapeEvent(utcnow(), "A", "fill", "BTC", "UP", 20.0, 0.55, None, "edge"))
    st.note("info", "hello")

    console = Console(width=160, height=44, record=True, force_terminal=True)
    console.print(render.build(st, 44))
    out = console.export_text()
    assert "STABLEBOT DESK" in out
    assert "BTC" in out
    assert "BLOCKED" in out


def test_help_text_is_honest_about_paper():
    from stablebot.desk import render

    assert "paper" in render.HELP.lower()


# ---------------------------------------------------------------------------
# pot accounting — sleeves sharing a pot must not double the bankroll
# ---------------------------------------------------------------------------


class _FakeSleeve:
    def __init__(self, name, pot_id, start, pnl, open_cost=0.0):
        self.name = name
        self.label = name
        self.pot_id = pot_id
        self._start = start
        self._pnl = pnl
        self._open = open_cost

    pot_start = property(lambda self: self._start)
    pot_pnl = property(lambda self: self._pnl)
    open_cost = property(lambda self: self._open)
    starting_equity = property(lambda self: self._start)
    equity = property(lambda self: self._start + self._pnl)

    def stat(self, state):
        return state.sleeves.setdefault(self.name, SleeveStat(self.name, self.label))


def _app(sleeves):
    from stablebot.desk.app import DeskApp

    app = DeskApp.__new__(DeskApp)
    app.sleeves = sleeves
    app.state = DeskState()
    return app


def test_two_sleeves_on_one_pot_count_the_stake_once():
    app = _app([
        _FakeSleeve("poly", "poly_shared", 1000.0, 5.0),
        _FakeSleeve("kalshi", "poly_shared", 1000.0, 3.0),
    ])
    app._refresh_equity()
    assert app.state.starting_equity == pytest.approx(1000.0)
    assert app.state.equity == pytest.approx(1008.0)


def test_separate_pots_add_up():
    app = _app([
        _FakeSleeve("spot", "spot_lag", 1000.0, -20.0),
        _FakeSleeve("poly", "poly_shared", 1000.0, 5.0),
        _FakeSleeve("kalshi", "poly_shared", 1000.0, 3.0),
    ])
    app._refresh_equity()
    assert app.state.starting_equity == pytest.approx(2000.0)
    assert app.state.equity == pytest.approx(1988.0)


def test_a_lone_sleeve_on_a_shared_pot_still_has_a_bankroll():
    # Kalshi alone must not report a zero pot, or the allocator can never size it.
    app = _app([_FakeSleeve("kalshi", "poly_shared", 1000.0, 0.0)])
    app._refresh_equity()
    assert app.state.equity == pytest.approx(1000.0)


def test_cash_is_equity_less_what_is_committed():
    app = _app([_FakeSleeve("spot", "spot_lag", 1000.0, 0.0, open_cost=250.0)])
    app._refresh_equity()
    assert app.state.open_cost == pytest.approx(250.0)
    assert app.state.cash == pytest.approx(750.0)


def test_the_daily_stop_survives_a_restart():
    # A stop that re-bases every time the process starts is not a stop.
    from stablebot.desk.risk import load_day_anchor

    st = _state()
    g = _gov()
    g.bind(st)
    assert load_day_anchor() is not None

    st.mark_equity(975.0, 975.0, 0.0)
    assert g.evaluate(st) == "HALTED"

    # a fresh desk, started after the loss, must stay halted
    st2 = DeskState()
    st2.starting_equity = 975.0
    st2.mark_equity(975.0, 975.0, 0.0)
    g2 = _gov()
    g2.bind(st2)
    assert st2.day_start_equity == pytest.approx(1000.0)
    assert g2.evaluate(st2) == "HALTED"


def test_the_loss_budget_tracks_the_anchored_day():
    st = _state()
    g = _gov(daily_stop=0.02)
    g.bind(st)
    g.evaluate(st)
    assert st.risk.budget_total == pytest.approx(20.0)
    st.mark_equity(992.0, 992.0, 0.0)
    g.evaluate(st)
    assert st.risk.budget_remaining == pytest.approx(12.0)


# ---------------------------------------------------------------------------
# watchdog — an enabled sleeve that stops cycling must not look merely quiet
# ---------------------------------------------------------------------------


def _watch_app(interval=20.0):
    s = _FakeSleeve("kl", "pot", 1000.0, 0.0)
    s.interval = interval
    app = _app([s])
    return app, s


def test_a_sleeve_cycling_normally_is_not_stale():
    app, s = _watch_app()
    stat = s.stat(app.state)
    stat.enabled = True
    stat.last_cycle_mono = time.monotonic()
    app._check_watchdog()
    assert stat.stale is False


def test_a_sleeve_that_stops_cycling_is_flagged_and_logged():
    app, s = _watch_app()
    stat = s.stat(app.state)
    stat.enabled = True
    stat.last_cycle_mono = time.monotonic() - 600.0
    app._check_watchdog()
    assert stat.stale is True
    assert stat.stale_for > 500.0
    assert any("stalled" in e[2] for e in app.state.log)


def test_a_sleeve_that_never_cycled_is_flagged_against_desk_start():
    """The live failure: setup ran, the scan loop never turned."""
    app, s = _watch_app()
    stat = s.stat(app.state)
    stat.enabled = True
    stat.last_cycle_mono = 0.0
    app.state.started_mono = time.monotonic() - 600.0
    app._check_watchdog()
    assert stat.stale is True
    assert any("never completed one" in e[2] for e in app.state.log)


def test_a_benched_sleeve_is_not_called_stalled():
    """It is off on purpose and already says why; a second alarm is noise."""
    app, s = _watch_app()
    stat = s.stat(app.state)
    stat.enabled = False
    stat.disabled_reason = "venue unreachable: Polymarket gamma"
    stat.last_cycle_mono = 0.0
    app.state.started_mono = time.monotonic() - 600.0
    app._check_watchdog()
    assert stat.stale is False
    assert not any("stalled" in e[2] for e in app.state.log)


def test_recovery_is_announced_once():
    app, s = _watch_app()
    stat = s.stat(app.state)
    stat.enabled = True
    stat.last_cycle_mono = time.monotonic() - 600.0
    app._check_watchdog()
    stat.last_cycle_mono = time.monotonic()
    app._check_watchdog()
    assert stat.stale is False
    assert any("cycling again" in e[2] for e in app.state.log)


def test_a_slow_sleeve_is_not_accused_during_one_scan():
    """Floor keeps a 20s sleeve from being called stalled after 40s."""
    app, s = _watch_app(interval=20.0)
    stat = s.stat(app.state)
    stat.enabled = True
    stat.last_cycle_mono = time.monotonic() - 40.0
    app._check_watchdog()
    assert stat.stale is False


# ---------------------------------------------------------------------------
# health probes must name the same hosts the clients trade against
# ---------------------------------------------------------------------------


def test_every_probe_targets_the_host_its_client_uses():
    """A probe pointed at a CDN edge answers from near the caller, so it can
    report a healthy venue while the endpoint orders go to is unreachable.
    Kalshi drifted this way once: probe on api.elections.kalshi.com (CloudFront,
    AWS GLOBAL), orders on external-api.kalshi.com (us-east-2).
    """
    from stablebot.desk.health import PROBES
    from stablebot.desk.reference import VISION
    from stablebot.kalshi.client import HOST as KALSHI_HOST
    from stablebot.poly.client import CLOB, GAMMA

    expected = {
        "kalshi": KALSHI_HOST,
        "poly": GAMMA,
        "poly_clob": CLOB,
        "binance": VISION,
    }
    for venue, host in expected.items():
        assert venue in PROBES, f"no probe for {venue}"
        assert PROBES[venue][1].startswith(host), (
            f"{venue} probes {PROBES[venue][1]} but its client uses {host}"
        )
