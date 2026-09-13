"""The wire contract: a remote screen must equal the local one."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from rich.console import Console

from stablebot.desk import render
from stablebot.desk.health import VenueHealth
from stablebot.desk.server import (
    MAX_REQUEST_BYTES,
    ExposureRefused,
    TOKEN_ENV,
    parse_request,
    serve_state,
)
from stablebot.desk.signal import GateCounter
from stablebot.desk.state import (
    BookRow,
    DeskState,
    PositionRow,
    SleeveStat,
    TapeEvent,
    utcnow,
)
from stablebot.desk.wire import state_from_dict, state_to_dict


def _populated() -> DeskState:
    st = DeskState()
    st.starting_equity, st.equity, st.cash = 3000.0, 2871.85, 2871.85
    st.peak_equity, st.day_start_equity = 3000.0, 2864.02
    st.risk.mode = "RUNNING"
    st.risk.day_start_equity, st.risk.daily_stop_pct = 2864.02, 0.04
    s = SleeveStat("kalshi_lag", "Kalshi Lag")
    s.enabled, s.status, s.allocation, s.clip = True, "scanning", 1500.0, 15.0
    s.trades, s.wins, s.losses = 6, 2, 4
    s.stale, s.stale_for, s.last_cycle_ts = True, 612.0, utcnow()
    s.params = {"z_entry": 2.5}
    st.sleeves = {"kalshi_lag": s}
    st.book = [BookRow(sleeve="kalshi_lag", symbol="BTC", venue="kalshi", window="15m",
                       expires_min=12.6, bid=None, ask=None, fair=0.469, edge=None,
                       state="COLD", detail="z=-0.48")]
    st.positions = [PositionRow(sleeve="kalshi_lag", symbol="BTC yes", side="yes", qty=10.0,
                                entry=0.86, cost=15.0, expires_in=300.0, mark=None)]
    st.push_tape(TapeEvent(ts=utcnow(), sleeve="Kalshi Lag", kind="fill", symbol="BTC",
                           side="YES", qty=10.0, price=0.86, pnl=None, detail="edge +0.05"))
    gc = GateCounter()
    for _ in range(887):
        gc.hit("no_signal", "z too small")
    st.gates = {"kalshi_lag": gc}
    st.venues = {"kalshi": VenueHealth(venue="kalshi", label="Kalshi", ok=True,
                                       status="ok", detail="200", resolved="3.151.224.18",
                                       latency_ms=241.0)}
    st.note("info", "desk up")
    return st


def _draw(state: DeskState) -> str:
    c = Console(width=180, record=True, file=open(os.devnull, "w"))
    c.print(render.build(state, 44))
    return c.export_text()


def test_a_round_trip_draws_the_same_screen():
    st = _populated()
    back = state_from_dict(json.loads(json.dumps(state_to_dict(st))))
    assert _draw(back) == _draw(st)


def test_the_snapshot_is_json_serialisable():
    json.dumps(state_to_dict(_populated()))


def test_uptime_is_rebased_onto_the_viewers_clock():
    """A raw monotonic stamp from another machine renders decades of uptime."""
    st = _populated()
    st.started_mono -= 3600.0                      # daemon has been up an hour
    back = state_from_dict(state_to_dict(st))
    assert back.uptime == pytest.approx(3600.0, abs=5.0)


def test_gate_counters_survive_the_wire():
    back = state_from_dict(state_to_dict(_populated()))
    assert back.gates["kalshi_lag"].total == 887
    assert back.gates["kalshi_lag"].binding() == ("no_signal", 887)


def test_an_unknown_field_from_a_newer_daemon_is_ignored():
    """Viewer and daemon versions drift; draw the screen anyway."""
    d = state_to_dict(_populated())
    d["sleeves"]["kalshi_lag"]["some_future_field"] = 123
    d["totally_new_top_level"] = {"x": 1}
    assert state_from_dict(d).sleeves["kalshi_lag"].trades == 6


def test_an_empty_state_round_trips():
    assert isinstance(state_from_dict(state_to_dict(DeskState())), DeskState)


# ---- the endpoint --------------------------------------------------------


def test_parse_request_reads_method_path_and_headers():
    m, p, h = parse_request(b"GET /state?x=1 HTTP/1.1\r\nHost: a\r\nAuthorization: Bearer t\r\n\r\n")
    assert (m, p) == ("GET", "/state?x=1")
    assert h["authorization"] == "Bearer t"


def test_parse_request_rejects_junk():
    with pytest.raises(ValueError):
        parse_request(b"garbage\r\n\r\n")


async def _get(port: int, path: str = "/state", token: str | None = None) -> tuple[str, str]:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    req = f"GET {path} HTTP/1.1\r\nHost: x\r\n"
    if token:
        req += f"Authorization: Bearer {token}\r\n"
    w.write((req + "\r\n").encode())
    await w.drain()
    data = await asyncio.wait_for(r.read(-1), timeout=5)
    w.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return head.split(b"\r\n")[0].decode(), body.decode()


def test_the_endpoint_serves_state_and_404s_everything_else():
    async def main():
        srv = await serve_state(lambda: {"hello": "desk"}, "127.0.0.1", 8791)
        try:
            assert await _get(8791, "/state") == ("HTTP/1.1 200 OK", '{"hello": "desk"}')
            assert (await _get(8791, "/health"))[0] == "HTTP/1.1 200 OK"
            assert (await _get(8791, "/nope"))[0].startswith("HTTP/1.1 404")
        finally:
            srv.close()
            await srv.wait_closed()

    asyncio.run(main())


def test_binding_off_loopback_without_a_token_is_refused():
    """The snapshot carries positions and P&L; do not publish it by accident."""
    async def main():
        with pytest.raises(ExposureRefused):
            await serve_state(lambda: {}, "0.0.0.0", 8792)

    os.environ.pop(TOKEN_ENV, None)
    asyncio.run(main())


def test_a_token_gates_the_state_feed(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "s3cret")

    async def main():
        srv = await serve_state(lambda: {"ok": 1}, "127.0.0.1", 8793)
        try:
            assert (await _get(8793))[0].startswith("HTTP/1.1 401")
            assert (await _get(8793, token="s3cret"))[0].startswith("HTTP/1.1 200")
        finally:
            srv.close()
            await srv.wait_closed()

    asyncio.run(main())


def test_health_is_reachable_without_a_token(monkeypatch):
    """The container HEALTHCHECK cannot send a token. Gating /health marked the
    desk permanently unhealthy and restart-looped it. /health leaks no book data."""
    monkeypatch.setenv(TOKEN_ENV, "s3cret")

    async def main():
        srv = await serve_state(lambda: {"equity": 1}, "127.0.0.1", 8794)
        try:
            status, body = await _get(8794, "/health")
            assert status == "HTTP/1.1 200 OK"
            assert body == '{"ok":true}'
            # ...while the feed that does carry positions stays shut.
            assert (await _get(8794, "/state"))[0].startswith("HTTP/1.1 401")
        finally:
            srv.close()
            await srv.wait_closed()

    asyncio.run(main())


def test_a_wrong_token_of_equal_length_is_rejected(monkeypatch):
    """Guards the compare_digest path: same length, so a naive == would still
    reject, but this pins the behaviour if anyone rewrites the comparison."""
    monkeypatch.setenv(TOKEN_ENV, "s3cret")

    async def main():
        srv = await serve_state(lambda: {"ok": 1}, "127.0.0.1", 8795)
        try:
            assert (await _get(8795, token="s3crXt"))[0].startswith("HTTP/1.1 401")
            assert (await _get(8795, token="s3cret"))[0].startswith("HTTP/1.1 200")
        finally:
            srv.close()
            await srv.wait_closed()

    asyncio.run(main())


def test_an_oversized_request_is_refused_not_buffered(monkeypatch):
    """MAX_REQUEST_BYTES was defined but never wired: a 60KB header returned 200.

    The server may answer 400 or drop the connection while we are still sending
    the oversized head. Both are refusals; the contract is that it is never
    served, so assert on that rather than on one particular refusal shape.
    """
    monkeypatch.delenv(TOKEN_ENV, raising=False)

    async def main():
        srv = await serve_state(lambda: {"ok": 1}, "127.0.0.1", 8796)
        try:
            first = ""
            try:
                r, w = await asyncio.open_connection("127.0.0.1", 8796)
                w.write(b"GET /state HTTP/1.1\r\nX-Pad: " + b"A" * (MAX_REQUEST_BYTES * 4) + b"\r\n\r\n")
                await w.drain()
                data = await asyncio.wait_for(r.read(-1), timeout=5)
                first = data.split(b"\r\n")[0].decode()
                w.close()
            except (ConnectionResetError, BrokenPipeError):
                first = "<connection dropped>"
            assert "200 OK" not in first, f"oversized request was served: {first!r}"

            # A normal request on the same server still works.
            assert (await _get(8796, "/state"))[0] == "HTTP/1.1 200 OK"
        finally:
            srv.close()
            await srv.wait_closed()

    asyncio.run(main())
