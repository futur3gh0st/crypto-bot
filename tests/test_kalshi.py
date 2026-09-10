"""Kalshi paper pair-complete: lock math, mocked HTTP, live=false. No network."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from stablebot.cli import build_parser
from stablebot.config import KalshiCfg
from stablebot.kalshi.client import (
    KalshiClient,
    KalshiNotFound,
    KalshiRateLimit,
    ScanRow,
    parse_orderbook,
    scan_series,
)
from stablebot.kalshi.paper import KalshiLedger, KalshiPaper
from stablebot.kalshi.session import recompute_shared_session
from stablebot.kalshi.strategy import can_pair_complete, kalshi_taker_fee, lock_edge, pair_curve_fee


def _row(**kwargs) -> ScanRow:
    base = dict(
        ticker="KXBTC15M-26AUG151900-00",
        series="KXBTC15M",
        yes_ask=0.45,
        no_ask=0.45,
        yes_ask_size=50.0,
        no_ask_size=50.0,
        sum_asks=0.90,
    )
    base.update(kwargs)
    if "lock_edge" not in kwargs and base.get("yes_ask") and base.get("no_ask"):
        apply_fee = kwargs.get("apply_curve_fee", True)
        base["curve_fee"] = pair_curve_fee(base["yes_ask"], base["no_ask"]) if apply_fee else 0.0
        base["lock_edge"] = lock_edge(base["yes_ask"], base["no_ask"], apply_fee)
    return ScanRow(**{k: v for k, v in base.items() if k != "apply_curve_fee"})


def test_parse_orderbook_asks_from_opposite_bids():
    # ascending bids; best = last
    data = {
        "orderbook_fp": {
            "yes_dollars": [["0.4000", "10.00"], ["0.5300", "25.00"]],
            "no_dollars": [["0.3800", "8.00"], ["0.4700", "40.00"]],
        }
    }
    book = parse_orderbook(data)
    assert book.yes_bid == pytest.approx(0.53)
    assert book.no_bid == pytest.approx(0.47)
    assert book.yes_ask == pytest.approx(1.0 - 0.47)
    assert book.no_ask == pytest.approx(1.0 - 0.53)
    assert book.yes_ask_size == pytest.approx(40.0)  # lift the no bid
    assert book.no_ask_size == pytest.approx(25.0)


def test_lock_accepted_when_pair_cheap_after_fee():
    # 0.45+0.45=0.90; curve ~ 2*0.07*0.45*0.55 = 0.03465; edge ~ 0.065 > 0.03
    edge = lock_edge(0.45, 0.45, apply_curve_fee=True)
    assert edge is not None
    assert edge > 0.03
    row = _row(yes_ask=0.45, no_ask=0.45, lock_edge=edge, curve_fee=pair_curve_fee(0.45, 0.45))
    assert can_pair_complete(row, min_lock=0.03, apply_curve_fee=True)


def test_lock_skipped_when_sum_asks_near_1_01():
    edge = lock_edge(0.50, 0.51, apply_curve_fee=True)
    assert edge is not None
    assert edge < 0
    row = _row(yes_ask=0.50, no_ask=0.51, sum_asks=1.01, lock_edge=edge)
    assert not can_pair_complete(row, min_lock=0.03, apply_curve_fee=True)


def test_fee_applied_before_accepting_lock():
    # 0.48+0.48=0.96 raw edge 0.04 > 0.03, but curve ~ 0.035 → post-fee < 0.03
    raw = lock_edge(0.48, 0.48, apply_curve_fee=False)
    after = lock_edge(0.48, 0.48, apply_curve_fee=True)
    assert raw is not None and raw > 0.03
    assert after is not None and after <= 0.03
    row = _row(yes_ask=0.48, no_ask=0.48, lock_edge=after)
    assert not can_pair_complete(row, min_lock=0.03, apply_curve_fee=True)


def test_fee_never_invents_rebate():
    assert kalshi_taker_fee(0.50) > 0
    assert lock_edge(0.45, 0.45, True) < lock_edge(0.45, 0.45, False)


def test_paper_lock_live_false_and_no_double_fill(tmp_path: Path):
    cfg = KalshiCfg(min_lock=0.03, paper_shares=20.0, apply_curve_fee=True)
    ledger = KalshiLedger(tmp_path / "kalshi_ledger.jsonl")
    eng = KalshiPaper(cfg, ledger, update_session=False)
    row = _row()
    fills = eng.step([row], datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc))
    done = [f for f in fills if not f.skipped]
    assert len(done) == 1
    assert done[0].kind == "pair_complete"
    assert done[0].shares == 20.0
    assert done[0].pnl > 0
    rec = done[0].extra
    assert rec["live"] is False
    assert rec["venue"] == "kalshi"
    assert rec["note"] == "paper pair-complete; no live order"
    assert rec["ticker"] == row.ticker
    assert rec["series"] == "KXBTC15M"
    assert "ask_yes" in rec and "ask_no" in rec
    fills2 = eng.step([row])
    assert all(f.skipped for f in fills2)
    lines = (tmp_path / "kalshi_ledger.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    stored = json.loads(lines[0])
    assert stored["live"] is False
    assert stored["kind"] == "pair_complete"


def test_paper_no_lock_when_sum_asks_over_one(tmp_path: Path):
    cfg = KalshiCfg(min_lock=0.03, apply_curve_fee=True)
    eng = KalshiPaper(cfg, KalshiLedger(tmp_path / "l.jsonl"), update_session=False)
    row = _row(yes_ask=0.50, no_ask=0.51, sum_asks=1.01, lock_edge=lock_edge(0.50, 0.51, True))
    fills = [f for f in eng.step([row]) if not f.skipped]
    assert fills == []
    assert not (tmp_path / "l.jsonl").exists() or (tmp_path / "l.jsonl").read_text() == ""


def test_cli_has_no_live_or_fade():
    parser = build_parser()
    ks = None
    kr = None
    for action in parser._subparsers._group_actions:
        for name, sub in action.choices.items():
            if name == "kalshi-scan":
                ks = sub
            if name == "kalshi-run":
                kr = sub
    assert ks is not None and kr is not None
    for sub in (ks, kr):
        flags = {opt for action in sub._actions for opt in (action.option_strings or [])}
        assert "--live" not in flags
        assert "--fade" not in flags


def test_shared_session_combines_both_ledgers(tmp_path: Path):
    poly = tmp_path / "poly_ledger.jsonl"
    kalshi = tmp_path / "kalshi_ledger.jsonl"
    session = tmp_path / "poly_session.json"
    session.write_text(
        json.dumps(
            {
                "starting_equity": 1000,
                "equity": 1000.0,
                "fills": 0,
                "last_ts": "2026-08-15T22:32:37.915859+00:00",
                "reset": "2026-08-16 paper reset to $1000; prior session archived",
            }
        ),
        encoding="utf-8",
    )
    poly.write_text(
        json.dumps(
            {
                "ts": "2026-08-16T00:00:00+00:00",
                "kind": "pair_complete",
                "pnl": 2.0,
                "live": False,
            }
        )
        + "\n"
        + json.dumps({"ts": "2026-08-16T00:01:00+00:00", "kind": "fade", "pnl": 99.0})
        + "\n",
        encoding="utf-8",
    )
    kalshi.write_text(
        json.dumps(
            {
                "ts": "2026-08-16T00:02:00+00:00",
                "kind": "pair_complete",
                "venue": "kalshi",
                "pnl": 1.5,
                "live": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = recompute_shared_session(session, poly, kalshi)
    assert out["starting_equity"] == 1000
    assert abs(out["equity"] - 1003.5) < 1e-12
    assert out["fills"] == 2
    assert out["last_ts"] == "2026-08-16T00:02:00+00:00"
    assert out["reset"] == "2026-08-16 paper reset to $1000; prior session archived"


def _transport(handler):
    return httpx.MockTransport(handler)


def test_429_is_retried_then_succeeds():
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(429, json={"error": {"code": "too_many_requests"}})
        return httpx.Response(200, json={"markets": []})

    async def _run():
        async with httpx.AsyncClient(transport=_transport(handler)) as http:
            client = KalshiClient(http, throttle_ms=0, max_retries=4, backoff_start=0.0)
            return await client.list_open_markets("KXBTC15M")

    markets = asyncio.run(_run())
    assert markets == []
    assert hits["n"] == 2


def test_429_exhausted_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": "too_many_requests"}})

    async def _run():
        async with httpx.AsyncClient(transport=_transport(handler)) as http:
            client = KalshiClient(http, throttle_ms=0, max_retries=3, backoff_start=0.0)
            await client.get("/markets", params={"series_ticker": "KXBTC15M"})

    with pytest.raises(KalshiRateLimit):
        asyncio.run(_run())


def test_404_drops_series_and_does_not_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    async def _run():
        async with httpx.AsyncClient(transport=_transport(handler)) as http:
            client = KalshiClient(http, throttle_ms=0, backoff_start=0.0)
            rows = await scan_series(
                ["KXNOPE15M"],
                apply_curve_fee=True,
                client=client,
                throttle_ms=0,
            )
            more = await client.list_open_markets("KXNOPE15M")
            return rows, client.dropped_series, more

    rows, dropped, more = asyncio.run(_run())
    assert rows == []
    assert "KXNOPE15M" in dropped
    assert more == []


def test_scan_hydrates_lock_from_mocked_book():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/markets") and not path.endswith("orderbook"):
            return httpx.Response(
                200,
                json={
                    "markets": [
                        {
                            "ticker": "KXBTC15M-26AUG151900-00",
                            "title": "BTC 15m",
                            "close_time": "2026-08-15T19:15:00Z",
                        }
                    ]
                },
            )
        if path.endswith("/orderbook"):
            return httpx.Response(
                200,
                json={
                    "orderbook_fp": {
                        "yes_dollars": [["0.5500", "30.00"]],
                        "no_dollars": [["0.5500", "30.00"]],
                    }
                },
            )
        return httpx.Response(404, json={})

    async def _run():
        async with httpx.AsyncClient(transport=_transport(handler)) as http:
            client = KalshiClient(http, throttle_ms=0, backoff_start=0.0)
            return await scan_series(["KXBTC15M"], apply_curve_fee=True, client=client)

    rows = asyncio.run(_run())
    assert len(rows) == 1
    r = rows[0]
    assert r.yes_ask == pytest.approx(0.45)
    assert r.no_ask == pytest.approx(0.45)
    assert r.lock_edge is not None and r.lock_edge > 0.03
    assert r.error is None


def test_kalshi_package_has_no_trading_or_auth_imports():
    root = Path(__file__).resolve().parents[1] / "src" / "stablebot" / "kalshi"
    banned = ("py_clob", "py-clob", "KALSHI-ACCESS", "rsa", "private_key", "api_key")
    for path in root.glob("*.py"):
        src = path.read_text(encoding="utf-8").lower()
        for token in banned:
            assert token.lower() not in src, f"{path.name} mentions {token}"
        assert "httpx.post" not in src
        assert ".post(" not in src
