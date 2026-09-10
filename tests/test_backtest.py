from datetime import datetime, timedelta, timezone
from pathlib import Path

from stablebot.backtest.engine import run_backtest
from stablebot.backtest.history import default_fixture_path, load_fixture
from tests.helpers import make_cfg


def test_fixture_exists():
    p = default_fixture_path()
    assert p.exists(), p


def test_walk_forward_no_lookahead_and_day_table():
    cfg = make_cfg()
    book = load_fixture(default_fixture_path())
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=36)
    result = run_backtest(cfg, book, start, end, balance=1000.0)
    assert result.days, "expected daily rows"
    assert result.starting_balance == 1000.0
    # Hour 10 signal should produce a fill at hour 11
    assert result.n_trades >= 1
    t0 = result.trades[0]
    assert t0.fill_ts - t0.signal_ts == timedelta(hours=1)
    assert t0.pnl != 0
    # Depeg around hour 20
    assert any(abs(a.deviation_bps) >= 30 for a in result.depegs)
    # Day breakdown fields present
    d0 = result.days[0]
    assert d0.starting_equity == 1000.0
    assert d0.ending_equity > 0
    assert 0 <= result.win_rate <= 1
    payload = result.to_dict()
    assert "total_return" in payload
    assert payload["x_note"]


def test_small_and_large_balances():
    cfg = make_cfg()
    book = load_fixture(default_fixture_path())
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=36)
    small = run_backtest(cfg, book, start, end, balance=100.0)
    large = run_backtest(cfg, book, start, end, balance=5000.0)
    assert small.starting_balance == 100.0
    assert large.starting_balance == 5000.0
    # Larger book uses larger notionals so |pnl| should be bigger when trades exist
    if small.n_trades and large.n_trades:
        assert abs(large.total_pnl) >= abs(small.total_pnl) - 1e-9


def test_persistent_cross_pair_not_harvested():
    """A stuck TUSD discount must not print money every hour."""
    from datetime import datetime, timedelta, timezone
    from stablebot.backtest.engine import run_backtest
    from stablebot.backtest.history import Bar, HistoryBook

    cfg = make_cfg()
    assert cfg.strategy.trade_cross_pair is False
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    book = HistoryBook()
    for h in range(12):
        ts = start + timedelta(hours=h)
        book.bars.append(Bar("binance", "TUSD", "USDT", ts, 0.996, 0.996, 0.996, 0.996))
        book.bars.append(Bar("binance", "USDC", "USDT", ts, 1.001, 1.001, 1.001, 1.001))
    result = run_backtest(cfg, book, start, start + timedelta(hours=12), 1000.0)
    assert result.n_trades == 0
    assert result.total_pnl == 0.0
