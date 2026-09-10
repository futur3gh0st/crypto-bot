"""Live poly gates fail closed. Dry-run never posts. Paper still live=false."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from stablebot.config import PolyCfg
from stablebot.poly.client import parse_best_book
from stablebot.poly.markets import ScanRow
from stablebot.poly.paper import PolyLedger, PolyPaper
from stablebot.poly.live import (
    CONFIRM_TEXT,
    ClobLive,
    PolyLive,
    live_gates_ok,
    order_filled,
    parse_fill,
    poly_live_env_on,
    require_live_ready,
)


KNOWN_5M = 1786747800


def _row(**kwargs) -> ScanRow:
    base = dict(
        coin="btc",
        minutes=5,
        which="current",
        slug="btc-updown-5m-1786747800",
        start_unix=KNOWN_5M,
        end_unix=KNOWN_5M + 300,
        minutes_left=1.5,
        up_ask=0.40,
        down_ask=0.40,
        sum_asks=0.80,
        lock_edge=0.20,
        up_token_id="tok-up",
        down_token_id="tok-down",
        up_ask_size=100.0,
        down_ask_size=100.0,
    )
    base.update(kwargs)
    return ScanRow(**base)


class FakeBook:
    def __init__(
        self,
        ask: float | None,
        ask_size: float | None,
        bid: float | None = 0.39,
        bid_size: float | None = 50.0,
    ):
        self.ask = ask
        self.ask_size = ask_size
        self.bid = bid
        self.bid_size = bid_size


class FakeClob:
    def __init__(self) -> None:
        self.buys: list[tuple[str, float, float]] = []
        self.sells: list[tuple[str, float, float]] = []
        self.market_sells: list[tuple[str, float]] = []
        self.balance: float | None = 1000.0
        self.buy_results: list[dict] = []
        self.sell_results: list[dict] = []
        self.market_sell_results: list[dict] = []

    def collateral_balance(self) -> float | None:
        return self.balance

    def post_fok_buy(self, token_id: str, price: float, size: float) -> dict:
        self.buys.append((token_id, price, size))
        if self.buy_results:
            return self.buy_results.pop(0)
        return {"success": True, "status": "matched"}

    def post_fok_sell(self, token_id: str, price: float, size: float) -> dict:
        self.sells.append((token_id, price, size))
        if self.sell_results:
            return self.sell_results.pop(0)
        return {"success": True, "status": "matched"}

    def post_fok_market_sell(self, token_id: str, size: float) -> dict:
        self.market_sells.append((token_id, size))
        if self.market_sell_results:
            return self.market_sell_results.pop(0)
        return {"success": True, "status": "matched"}


def _arm_env(monkeypatch, tmp_path: Path, *, poly_live: str = "1", confirm: bool = True) -> None:
    monkeypatch.setattr("stablebot.poly.live.data_dir", lambda: tmp_path)
    monkeypatch.setenv("POLY_LIVE", poly_live)
    monkeypatch.setenv("POLY_PK", "0x" + "ab" * 32)
    monkeypatch.setenv("CLOB_API_KEY", "key")
    monkeypatch.setenv("CLOB_SECRET", "secret")
    monkeypatch.setenv("CLOB_PASS_PHRASE", "phrase")
    if confirm:
        (tmp_path / "poly_live_confirm.txt").write_text(CONFIRM_TEXT + "\n", encoding="utf-8")


def _engine(tmp_path: Path, clob: FakeClob, **cfg_kw) -> PolyLive:
    cfg = PolyCfg(
        min_lock=0.03,
        paper_shares=10.0,
        live_max_shares=20.0,
        live_daily_notional=200.0,
        live_balance_buffer=0.10,
        taker_fee_bps=0.0,
        apply_curve_fee=True,
        **cfg_kw,
    )
    books = {
        "tok-up": FakeBook(0.40, 100.0, bid=0.39),
        "tok-down": FakeBook(0.40, 100.0, bid=0.39),
    }

    def fetch(token_id: str) -> FakeBook:
        return books[token_id]

    return PolyLive(
        cfg,
        PolyLedger(tmp_path / "poly_ledger.jsonl"),
        clob,  # type: ignore[arg-type]
        dry_run=cfg_kw.pop("dry_run", False) if False else False,
        book_fetch=fetch,
    )


def test_scanrow_keeps_token_ids():
    row = _row()
    assert row.up_token_id == "tok-up"
    assert row.down_token_id == "tok-down"


def test_hydrate_persists_token_ids():
    import inspect

    from stablebot.poly.client import hydrate_row

    src = inspect.getsource(hydrate_row)
    assert "row.up_token_id" in src
    assert "row.down_token_id" in src


def test_parse_best_book_ignores_wings():
    book = parse_best_book(
        {
            "bids": [
                {"price": "0.01", "size": "999"},
                {"price": "0.48", "size": "12"},
            ],
            "asks": [
                {"price": "0.99", "size": "999"},
                {"price": "0.52", "size": "8"},
            ],
        }
    )
    assert book.bid == 0.48
    assert book.bid_size == 12.0
    assert book.ask == 0.52
    assert book.ask_size == 8.0


def test_cex_live_is_not_poly_live(monkeypatch):
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.delenv("POLY_LIVE", raising=False)
    assert poly_live_env_on() is False


def test_poly_live_must_be_exactly_one(monkeypatch):
    monkeypatch.setenv("POLY_LIVE", "true")
    assert poly_live_env_on() is False
    monkeypatch.setenv("POLY_LIVE", "1")
    assert poly_live_env_on() is True


def test_gates_fail_without_cli_live(monkeypatch, tmp_path):
    _arm_env(monkeypatch, tmp_path)
    ok, fails = live_gates_ok(cli_live=False, fade=False)
    assert ok is False
    assert any("CLI" in f for f in fails)


def test_gates_fail_without_poly_live(monkeypatch, tmp_path):
    _arm_env(monkeypatch, tmp_path, poly_live="0")
    ok, fails = live_gates_ok(cli_live=True, fade=False)
    assert ok is False
    assert any("POLY_LIVE" in f for f in fails)


def test_gates_fail_without_confirm(monkeypatch, tmp_path):
    _arm_env(monkeypatch, tmp_path, confirm=False)
    ok, fails = live_gates_ok(cli_live=True, fade=False)
    assert ok is False
    assert any("confirm" in f for f in fails)


def test_gates_fail_wrong_confirm_contents(monkeypatch, tmp_path):
    _arm_env(monkeypatch, tmp_path, confirm=False)
    (tmp_path / "poly_live_confirm.txt").write_text("yes\n", encoding="utf-8")
    ok, fails = live_gates_ok(cli_live=True, fade=False)
    assert ok is False
    assert any("confirm" in f for f in fails)


def test_gates_fail_when_halt_present(monkeypatch, tmp_path):
    _arm_env(monkeypatch, tmp_path)
    (tmp_path / "poly_halt").write_text("", encoding="utf-8")
    ok, fails = live_gates_ok(cli_live=True, fade=False)
    assert ok is False
    assert any("halt" in f for f in fails)


def test_gates_fail_with_fade(monkeypatch, tmp_path):
    _arm_env(monkeypatch, tmp_path)
    ok, fails = live_gates_ok(cli_live=True, fade=True)
    assert ok is False
    assert any("fade" in f for f in fails)


def test_gates_fail_missing_env(monkeypatch, tmp_path):
    monkeypatch.setattr("stablebot.poly.live.data_dir", lambda: tmp_path)
    (tmp_path / "poly_live_confirm.txt").write_text(CONFIRM_TEXT, encoding="utf-8")
    monkeypatch.setenv("POLY_LIVE", "1")
    monkeypatch.delenv("POLY_PK", raising=False)
    monkeypatch.delenv("PK", raising=False)
    monkeypatch.delenv("CLOB_API_KEY", raising=False)
    monkeypatch.delenv("CLOB_SECRET", raising=False)
    monkeypatch.delenv("CLOB_PASS_PHRASE", raising=False)
    ok, fails = live_gates_ok(cli_live=True, fade=False)
    assert ok is False
    blob = " ".join(fails)
    assert "POLY_PK" in blob
    assert "CLOB_API_KEY" in blob


def test_require_live_ready_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr("stablebot.poly.live.data_dir", lambda: tmp_path)
    monkeypatch.setenv("POLY_LIVE", "0")
    with pytest.raises(SystemExit) as ei:
        require_live_ready(cli_live=True, fade=False)
    assert "refusing to start" in str(ei.value)


def test_cmd_poly_run_fade_plus_live_hard_error():
    import asyncio

    from stablebot.config import AppConfig
    from stablebot.poly.scan import cmd_poly_run

    with pytest.raises(SystemExit) as ei:
        asyncio.run(cmd_poly_run(AppConfig(), interval=10, fade=True, live=True))
    assert "fade" in str(ei.value).lower()


def test_paper_path_source_does_not_import_live_client():
    import inspect

    from stablebot.poly.scan import cmd_poly_run

    src = inspect.getsource(cmd_poly_run)
    assert "from stablebot.poly.live import" in src
    assert "if live_mode:" in src
    assert "py_clob_client_v2" not in src


def test_paper_still_writes_live_false(tmp_path):
    cfg = PolyCfg(min_lock=0.005, paper_shares=10.0, taker_fee_bps=0.0)
    ledger = PolyLedger(tmp_path / "poly_ledger.jsonl")
    eng = PolyPaper(cfg, ledger, fade=False)
    row = _row(up_ask=0.49, down_ask=0.49, sum_asks=0.98, lock_edge=0.02)
    fills = [f for f in eng.step([row]) if not f.skipped]
    assert len(fills) == 1
    rec = json.loads((tmp_path / "poly_ledger.jsonl").read_text().strip())
    assert rec["live"] is False
    assert rec["kind"] == "pair_complete"


def test_dry_run_never_posts(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    cfg = PolyCfg(
        min_lock=0.03,
        paper_shares=10.0,
        live_max_shares=20.0,
        live_daily_notional=200.0,
        live_balance_buffer=0.10,
        taker_fee_bps=0.0,
    )
    books = {
        "tok-up": FakeBook(0.40, 100.0),
        "tok-down": FakeBook(0.40, 100.0),
    }
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        dry_run=True,
        book_fetch=lambda tid: books[tid],
    )
    fills = eng.step([_row()], datetime(2026, 8, 16, tzinfo=timezone.utc))
    assert fills
    assert fills[0].kind == "live_dry_run"
    assert fills[0].skipped is True
    assert "Up first" in fills[0].reason
    assert fills[0].extra.get("first_side") == "up"
    assert "Up then Down" in fills[0].extra.get("note", "")
    assert clob.buys == []
    assert clob.sells == []
    assert clob.market_sells == []
    assert not (tmp_path / "l.jsonl").exists() or "pair_complete" not in (
        tmp_path / "l.jsonl"
    ).read_text()


def test_one_leg_unwind_if_second_fok_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    clob.buy_results = [
        {"success": True, "status": "matched", "takingAmount": "10", "makingAmount": "4.0"},
        {"success": False, "errorMsg": "no match"},
    ]
    cfg = PolyCfg(
        min_lock=0.03,
        paper_shares=10.0,
        live_max_shares=20.0,
        live_daily_notional=200.0,
        live_balance_buffer=0.10,
        taker_fee_bps=0.0,
    )
    books = {
        "tok-up": FakeBook(0.40, 100.0, bid=0.39),
        "tok-down": FakeBook(0.40, 100.0),
    }
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        dry_run=False,
        book_fetch=lambda tid: books[tid],
    )
    fills = [f for f in eng.step([_row()]) if f.kind == "one_leg_unwind"]
    assert len(fills) == 1
    assert len(clob.buys) == 2
    assert clob.buys[0][0] == "tok-up"
    assert clob.buys[1][0] == "tok-down"
    assert clob.sells
    assert clob.sells[0][0] == "tok-up"
    rec = json.loads((tmp_path / "l.jsonl").read_text().strip())
    assert rec["kind"] == "one_leg_unwind"
    assert rec["live"] is True


def test_up_fail_does_not_send_down(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    clob.buy_results = [{"success": False, "errorMsg": "killed"}]
    cfg = PolyCfg(
        min_lock=0.03,
        paper_shares=10.0,
        live_max_shares=20.0,
        live_daily_notional=200.0,
        live_balance_buffer=0.10,
        taker_fee_bps=0.0,
    )
    books = {"tok-up": FakeBook(0.40, 100.0), "tok-down": FakeBook(0.40, 100.0)}
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = eng.step([_row()])
    assert any(f.kind == "pair_skip" for f in fills)
    assert len(clob.buys) == 1
    assert clob.buys[0][0] == "tok-up"
    assert clob.sells == []


def test_missing_balance_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    clob.balance = None
    cfg = PolyCfg(min_lock=0.03, paper_shares=10.0, live_max_shares=20.0)
    books = {"tok-up": FakeBook(0.40, 100.0), "tok-down": FakeBook(0.40, 100.0)}
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = eng.step([_row()])
    assert fills[0].skipped
    assert "balance" in fills[0].reason
    assert clob.buys == []


def test_ask_size_below_shares_skips(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    cfg = PolyCfg(min_lock=0.03, paper_shares=10.0, live_max_shares=20.0)
    books = {"tok-up": FakeBook(0.40, 3.0), "tok-down": FakeBook(0.40, 100.0)}
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = eng.step([_row()])
    assert fills[0].skipped
    assert "size" in fills[0].reason
    assert "2x" in fills[0].reason
    assert clob.buys == []


def test_shares_above_live_max_skips(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    cfg = PolyCfg(min_lock=0.03, paper_shares=50.0, live_max_shares=20.0)
    books = {"tok-up": FakeBook(0.40, 100.0), "tok-down": FakeBook(0.40, 100.0)}
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = eng.step([_row()])
    assert fills[0].skipped
    assert "live_max_shares" in fills[0].reason
    assert clob.buys == []


def test_daily_notional_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    ledger = PolyLedger(tmp_path / "l.jsonl")
    ledger.append(
        {
            "ts": "2026-08-16T01:00:00+00:00",
            "kind": "pair_complete",
            "slug": "other",
            "shares": 20.0,
            "ask_up": 0.5,
            "ask_down": 0.5,
            "live": True,
        }
    )
    clob = FakeClob()
    cfg = PolyCfg(
        min_lock=0.03,
        paper_shares=10.0,
        live_max_shares=20.0,
        live_daily_notional=20.0,
    )
    books = {"tok-up": FakeBook(0.40, 100.0), "tok-down": FakeBook(0.40, 100.0)}
    eng = PolyLive(cfg, ledger, clob, book_fetch=lambda tid: books[tid])  # type: ignore[arg-type]
    fills = eng.step([_row()], datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc))
    assert fills[0].skipped
    assert "daily notional" in fills[0].reason
    assert clob.buys == []


def test_halt_mid_loop_stops_sending(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: True)
    clob = FakeClob()
    cfg = PolyCfg(min_lock=0.03, paper_shares=10.0)
    books = {"tok-up": FakeBook(0.40, 100.0), "tok-down": FakeBook(0.40, 100.0)}
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = eng.step([_row()])
    assert fills[0].kind == "halt"
    assert clob.buys == []
    assert eng.halted is True


def test_missing_token_ids_skip(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    cfg = PolyCfg(min_lock=0.03, paper_shares=10.0)
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: FakeBook(0.40, 100.0),
    )
    fills = eng.step([_row(up_token_id=None, down_token_id=None)])
    assert fills[0].skipped
    assert "token" in fills[0].reason
    assert clob.buys == []


def test_both_legs_fill_ledgers_live_true(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    cfg = PolyCfg(
        min_lock=0.03,
        paper_shares=10.0,
        live_max_shares=20.0,
        live_daily_notional=200.0,
        live_balance_buffer=0.10,
        taker_fee_bps=0.0,
    )
    books = {"tok-up": FakeBook(0.40, 100.0), "tok-down": FakeBook(0.40, 100.0)}
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = [f for f in eng.step([_row()]) if not f.skipped]
    assert len(fills) == 1
    assert fills[0].kind == "pair_complete"
    rec = json.loads((tmp_path / "l.jsonl").read_text().strip())
    assert rec["live"] is True
    assert rec["kind"] == "pair_complete"
    assert len(clob.buys) == 2


def test_unwind_falls_back_to_market_if_bid_sell_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    clob.buy_results = [
        {"success": True, "status": "matched"},
        {"success": False, "errorMsg": "no down"},
    ]
    clob.sell_results = [{"success": False, "errorMsg": "no bid"}]
    clob.market_sell_results = [{"success": True, "status": "matched"}]
    cfg = PolyCfg(min_lock=0.03, paper_shares=10.0, live_max_shares=20.0)
    books = {"tok-up": FakeBook(0.40, 100.0, bid=0.39), "tok-down": FakeBook(0.40, 100.0)}
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = [f for f in eng.step([_row()]) if f.kind == "one_leg_unwind"]
    assert fills
    assert clob.sells
    assert clob.market_sells
    rec = json.loads((tmp_path / "l.jsonl").read_text().strip())
    assert rec["residual_risk"] is False


def test_order_filled_and_parse_fill():
    assert order_filled({"success": True, "status": "matched"})
    assert not order_filled({"success": False, "errorMsg": "x"})
    assert not order_filled({"success": True, "status": "killed"})
    px, sz = parse_fill(
        {"takingAmount": "10", "makingAmount": "4.0", "status": "matched"},
        0.40,
        10.0,
    )
    assert abs(px - 0.40) < 1e-9
    assert abs(sz - 10.0) < 1e-9


def test_clob_live_is_lazy_and_does_not_import_at_module_level():
    import stablebot.poly.live as live_mod

    src = Path(live_mod.__file__).read_text(encoding="utf-8")
    assert "from py_clob_client_v2 import" in src
    assert src.index("def _load_sdk") < src.index("from py_clob_client_v2 import")


def test_live_check_lists_missing_names_only(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("stablebot.poly.live.data_dir", lambda: tmp_path)
    monkeypatch.delenv("POLY_PK", raising=False)
    monkeypatch.delenv("PK", raising=False)
    monkeypatch.delenv("CLOB_API_KEY", raising=False)
    monkeypatch.delenv("CLOB_SECRET", raising=False)
    monkeypatch.delenv("CLOB_PASS_PHRASE", raising=False)
    monkeypatch.setenv("POLY_LIVE", "0")
    from stablebot.poly.live import cmd_poly_live_check

    cmd_poly_live_check()
    out = capsys.readouterr().out
    assert "POLY_PK" in out
    assert "CLOB_API_KEY" in out
    assert "I_ACCEPT_LIVE_POLY_ORDERS" in out
    assert "0x" + "ab" * 32 not in out
    assert "secret" not in out.lower() or "CLOB_SECRET" in out

def _live_cfg(**kw) -> PolyCfg:
    base = dict(
        min_lock=0.03,
        paper_shares=10.0,
        live_max_shares=20.0,
        live_daily_notional=200.0,
        live_balance_buffer=0.10,
        taker_fee_bps=0.0,
    )
    base.update(kw)
    return PolyCfg(**base)


def test_ask_size_1x_but_below_2x_skips(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    cfg = _live_cfg()
    books = {"tok-up": FakeBook(0.40, 15.0), "tok-down": FakeBook(0.40, 100.0)}
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = eng.step([_row()])
    assert fills[0].skipped
    assert "2x" in fills[0].reason
    assert clob.buys == []


def test_missing_bid_skips(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    cfg = _live_cfg()
    books = {"tok-up": FakeBook(0.40, 100.0, bid=None), "tok-down": FakeBook(0.40, 100.0)}
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = eng.step([_row()])
    assert fills[0].skipped
    assert "bid" in fills[0].reason
    assert clob.buys == []


def test_zero_bid_skips(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    cfg = _live_cfg()
    books = {"tok-up": FakeBook(0.40, 100.0, bid=0.0), "tok-down": FakeBook(0.40, 100.0)}
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = eng.step([_row()])
    assert fills[0].skipped
    assert "bid" in fills[0].reason
    assert clob.buys == []


def test_crossed_book_skips(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    cfg = _live_cfg()
    books = {
        "tok-up": FakeBook(0.40, 100.0, bid=0.41),
        "tok-down": FakeBook(0.40, 100.0, bid=0.39),
    }
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = eng.step([_row()])
    assert fills[0].skipped
    assert "crossed" in fills[0].reason
    assert clob.buys == []


def test_unwind_spread_wipes_lock_skips(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    cfg = _live_cfg()
    # ask-bid = 0.20 on Up; lock after curve is ~0.166, so unwind would wipe.
    books = {
        "tok-up": FakeBook(0.40, 100.0, bid=0.20),
        "tok-down": FakeBook(0.40, 100.0, bid=0.39),
    }
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = eng.step([_row()])
    assert fills[0].skipped
    assert "unwind spread would wipe lock" in fills[0].reason
    assert clob.buys == []


def test_zero_unwind_spread_does_not_skip(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    cfg = _live_cfg()
    books = {
        "tok-up": FakeBook(0.40, 100.0, bid=0.40),
        "tok-down": FakeBook(0.40, 100.0, bid=0.40),
    }
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = [f for f in eng.step([_row()]) if not f.skipped]
    assert len(fills) == 1
    assert fills[0].kind == "pair_complete"
    assert len(clob.buys) == 2


def test_thinner_ask_sent_first(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    cfg = _live_cfg()
    books = {
        "tok-up": FakeBook(0.40, 100.0),
        "tok-down": FakeBook(0.40, 25.0),
    }
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = [f for f in eng.step([_row()]) if not f.skipped]
    assert len(fills) == 1
    assert fills[0].kind == "pair_complete"
    assert clob.buys[0][0] == "tok-down"
    assert clob.buys[1][0] == "tok-up"
    rec = json.loads((tmp_path / "l.jsonl").read_text().strip())
    assert rec["first_side"] == "down"


def test_thinner_first_fail_does_not_send_second(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    clob.buy_results = [{"success": False, "errorMsg": "killed"}]
    cfg = _live_cfg()
    books = {
        "tok-up": FakeBook(0.40, 100.0),
        "tok-down": FakeBook(0.40, 25.0),
    }
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = eng.step([_row()])
    assert any(f.kind == "pair_skip" for f in fills)
    assert "Down FOK failed" in fills[0].reason
    assert len(clob.buys) == 1
    assert clob.buys[0][0] == "tok-down"
    assert clob.sells == []


def test_thinner_first_second_fail_unwinds_filled_side(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    clob.buy_results = [
        {"success": True, "status": "matched"},
        {"success": False, "errorMsg": "no up"},
    ]
    cfg = _live_cfg()
    books = {
        "tok-up": FakeBook(0.40, 100.0, bid=0.39),
        "tok-down": FakeBook(0.40, 25.0, bid=0.39),
    }
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        book_fetch=lambda tid: books[tid],
    )
    fills = [f for f in eng.step([_row()]) if f.kind == "one_leg_unwind"]
    assert len(fills) == 1
    assert clob.buys[0][0] == "tok-down"
    assert clob.buys[1][0] == "tok-up"
    assert clob.sells
    assert clob.sells[0][0] == "tok-down"
    rec = json.loads((tmp_path / "l.jsonl").read_text().strip())
    assert rec["kind"] == "one_leg_unwind"
    assert rec["first_side"] == "down"
    assert rec["live"] is True


def test_dry_run_thinner_down_says_down_first(tmp_path, monkeypatch):
    monkeypatch.setattr("stablebot.poly.live.halt_present", lambda: False)
    clob = FakeClob()
    cfg = _live_cfg()
    books = {
        "tok-up": FakeBook(0.40, 100.0),
        "tok-down": FakeBook(0.40, 25.0),
    }
    eng = PolyLive(
        cfg,
        PolyLedger(tmp_path / "l.jsonl"),
        clob,  # type: ignore[arg-type]
        dry_run=True,
        book_fetch=lambda tid: books[tid],
    )
    fills = eng.step([_row()], datetime(2026, 8, 16, tzinfo=timezone.utc))
    assert fills[0].kind == "live_dry_run"
    assert fills[0].skipped is True
    assert fills[0].extra.get("first_side") == "down"
    assert "Down then Up" in fills[0].extra.get("note", "")
    assert clob.buys == []
    assert clob.sells == []


def test_live_ask_size_mult_default_is_two():
    assert PolyCfg().live_ask_size_mult == 2.0
