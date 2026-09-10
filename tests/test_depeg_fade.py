from datetime import datetime, timedelta, timezone

from stablebot.backtest.book import run_book_backtest
from stablebot.backtest.history import Bar, HistoryBook
from stablebot.strategy.depeg_fade import (
    DepegPosition,
    acute_entry,
    consecutive_cheap,
    depeg_pnl,
    deviation_bps,
    exit_reason,
    near_peg,
    should_rearm,
)
from tests.helpers import make_cfg


def _hourly(book, start, n, px, *, venue="binance", base="TUSD", quote="USDT"):
    for h in range(n):
        ts = start + timedelta(hours=h)
        p = px(h) if callable(px) else px
        book.bars.append(Bar(venue, base, quote, ts, p, p, p, p))


def test_entry_needs_n_consecutive():
    assert consecutive_cheap([0.9960], n=2, entry_bps=35) is False
    assert consecutive_cheap([0.9960, 0.9964], n=2, entry_bps=35) is True
    assert consecutive_cheap([0.9960, 0.9990], n=2, entry_bps=35) is False
    assert consecutive_cheap([0.9970, 0.9970], n=2, entry_bps=35) is False  # -30 bps


def test_acute_requires_near_peg_then_cheap():
    near = [0.9990] * 24  # -10 bps, inside ±15
    cheap = [0.9960, 0.9960]  # -40 bps
    assert near_peg(0.9990, 15.0) is True
    assert near_peg(0.9960, 15.0) is False
    assert acute_entry(near + cheap, n=2, entry_bps=35, band_bps=15, lookback_hours=24) is True
    assert acute_entry([0.9960] * 26, n=2, entry_bps=35, band_bps=15, lookback_hours=24) is False
    assert acute_entry(near + cheap[:1], n=2, entry_bps=35, band_bps=15, lookback_hours=24) is False
    assert acute_entry(near[:10] + cheap, n=2, entry_bps=35, band_bps=15, lookback_hours=24) is False


def test_exit_repeg_stop_time():
    ts = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    assert exit_reason(0.9992, ts, ts + timedelta(hours=1)) == "repeg"
    assert exit_reason(0.9800, ts, ts + timedelta(hours=1)) == "stop"
    assert exit_reason(0.9960, ts, ts + timedelta(hours=72)) == "time"
    assert exit_reason(0.9960, ts, ts + timedelta(hours=10)) is None


def test_depeg_pnl_and_stop_loss():
    pos = DepegPosition(
        asset="TUSD",
        pair="TUSD/USDT",
        venue="binance",
        entry_ts=datetime(2026, 7, 1, tzinfo=timezone.utc),
        entry_px=0.9960,
        notional=100.0,
        units=100.0 / 0.9960,
        entry_fee=100.0 * 11 / 10_000.0,
    )
    # recover to 0.9990
    pnl, fee = depeg_pnl(pos, 0.9990, 11.0)
    assert pnl > 0
    # melt to 0.9800 (stop)
    pnl_s, _ = depeg_pnl(pos, 0.9800, 11.0)
    assert pnl_s < 0
    assert deviation_bps(0.9800) <= -150


def test_chronic_discount_does_not_enter():
    """Month-long TUSD at −40 bps is not an acute depeg — sit flat."""
    cfg = make_cfg()
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    book = HistoryBook()
    _hourly(book, start - timedelta(hours=24), 24, 0.9962)
    _hourly(book, start, 16, 0.9962)
    result = run_book_backtest(cfg, book, None, start, start + timedelta(hours=16), 1000.0, ["depeg"])
    enters = [t for t in result.trades if t.kind == "enter"]
    assert enters == []
    assert result.n_trades == 0
    assert result.total_pnl == 0.0


def test_no_restack_and_reset_required():
    cfg = make_cfg()
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    book = HistoryBook()
    _hourly(book, start - timedelta(hours=24), 24, 0.9990)
    _hourly(book, start, 16, 0.9960)
    result = run_book_backtest(cfg, book, None, start, start + timedelta(hours=16), 1000.0, ["depeg"])
    enters = [t for t in result.trades if t.kind == "enter"]
    assert len(enters) == 1, enters
    # still cheap after exit-by-time would require reset; 16h < 72h so still open then eod flatten
    # one enter, one eod exit
    exits = [t for t in result.trades if t.kind.startswith("exit")]
    assert len(exits) == 1


def test_no_reenter_without_reset_after_time_stop():
    """Stuck discount must not be re-bought every time-stop (fee bleed)."""
    cfg = make_cfg()
    cfg.depeg_fade.max_hold_hours = 3
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    book = HistoryBook()
    _hourly(book, start - timedelta(hours=24), 24, 0.9990)
    _hourly(book, start, 20, 0.9960)
    result = run_book_backtest(cfg, book, None, start, start + timedelta(hours=20), 1000.0, ["depeg"])
    enters = [t for t in result.trades if t.kind == "enter"]
    assert len(enters) == 1, [t.kind for t in result.trades]
    assert should_rearm(0.9960, 35.0) is False
    assert should_rearm(0.9970, 35.0) is True


def test_no_lookahead_fill_next_open():
    cfg = make_cfg()
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    book = HistoryBook()
    _hourly(book, start - timedelta(hours=24), 24, 0.9990)
    # hours 0-1 cheap on close; fill must be hour 2 OPEN (1.0), not the cheap close
    prices = [
        (0.9960, 0.9960),  # h0
        (0.9960, 0.9960),  # h1 signal
        (1.0000, 0.9990),  # h2 fill at open 1.0
        (0.9990, 0.9992),  # h3
    ]
    for h, (opn, cls) in enumerate(prices):
        ts = start + timedelta(hours=h)
        book.bars.append(Bar("binance", "TUSD", "USDT", ts, opn, max(opn, cls), min(opn, cls), cls))
    result = run_book_backtest(cfg, book, None, start, start + timedelta(hours=4), 1000.0, ["depeg"])
    enters = [t for t in result.trades if t.kind == "enter"]
    assert len(enters) == 1
    assert abs(enters[0].extra["entry_px"] - 1.0) < 1e-9
    assert enters[0].ts == start + timedelta(hours=2)


def test_stop_shows_a_loss():
    cfg = make_cfg()
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    book = HistoryBook()
    _hourly(book, start - timedelta(hours=24), 24, 0.9990)
    # 2 cheap bars, fill, then melt
    seq = [0.9960, 0.9960, 0.9960, 0.9800, 0.9800]
    for h, px in enumerate(seq):
        ts = start + timedelta(hours=h)
        book.bars.append(Bar("binance", "TUSD", "USDT", ts, px, px, px, px))
    result = run_book_backtest(cfg, book, None, start, start + timedelta(hours=5), 1000.0, ["depeg"])
    exits = [t for t in result.trades if t.kind.startswith("exit")]
    assert exits
    assert any(t.pnl < 0 for t in exits)
    assert result.depeg_losses >= 1
