"""Serialise DeskState so a remote terminal can render a desk it is not running.

The daemon owns the sleeves, the clocks and the money. A viewer owns a screen.
Everything crossing the wire is therefore a snapshot, and anything measured
against a *local* clock is converted to a duration before it is sent —
`time.monotonic()` on the daemon means nothing on the viewer's machine, and a
raw monotonic stamp would render an uptime of several decades.

Round-tripping is the contract: render.build(state_from_dict(state_to_dict(s)))
must produce the same screen as render.build(s).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import fields, is_dataclass
from datetime import datetime
from typing import Any

from stablebot.desk.signal import GateCounter
from stablebot.desk.state import (
    MAX_CURVE,
    MAX_LOG,
    MAX_TAPE,
    BookRow,
    DeskState,
    PositionRow,
    RiskState,
    SleeveStat,
    TapeEvent,
)

WIRE_VERSION = 1


def _dump(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, (deque, list, tuple)):
        return [_dump(v) for v in value]
    if isinstance(value, dict):
        return {k: _dump(v) for k, v in value.items()}
    return value


def _row(obj: Any) -> dict[str, Any]:
    return {f.name: _dump(getattr(obj, f.name)) for f in fields(obj)}


def _load(value: Any) -> Any:
    if isinstance(value, dict):
        if "__dt__" in value:
            return datetime.fromisoformat(value["__dt__"])
        return {k: _load(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_load(v) for v in value]
    return value


def _build(cls: Any, raw: dict[str, Any]) -> Any:
    """Construct a dataclass from a dict, ignoring fields it no longer has.

    A viewer and a daemon can be on different versions; drop what we do not
    recognise rather than refusing to draw the screen.
    """
    known = {f.name for f in fields(cls)}
    obj = cls(**{k: _load(v) for k, v in raw.items() if k in known and k not in _SKIP})
    return obj


# Fields that are meaningless off the machine that produced them.
_SKIP = {"last_cycle_mono", "started_mono"}


def state_to_dict(st: DeskState) -> dict[str, Any]:
    return {
        "wire": WIRE_VERSION,
        "mode": st.mode,
        "uptime": st.uptime,
        "started_at": _dump(st.started_at),
        "starting_equity": st.starting_equity,
        "equity": st.equity,
        "cash": st.cash,
        "open_cost": st.open_cost,
        "day_start_equity": st.day_start_equity,
        "peak_equity": st.peak_equity,
        "paused": st.paused,
        "autopilot": st.autopilot,
        "focus": st.focus,
        "notice": st.notice,
        "notice_left": max(0.0, st.notice_until - time.monotonic()),
        "sleeves": {k: _row(v) for k, v in st.sleeves.items()},
        "book": [_row(r) for r in st.book],
        "positions": [_row(r) for r in st.positions],
        "tape": [_row(r) for r in st.tape],
        "log": [[_dump(ts), lvl, msg] for ts, lvl, msg in st.log],
        "curve": [[a, b] for a, b in st.curve],
        "risk": _row(st.risk),
        "gates": {
            k: {"counts": dict(v.counts), "last_detail": dict(v.last_detail)}
            for k, v in st.gates.items()
            if isinstance(v, GateCounter)
        },
        "venues": {
            k: _row(v) for k, v in st.venues.items() if is_dataclass(v)
        },
    }


def state_from_dict(d: dict[str, Any]) -> DeskState:
    now = time.monotonic()
    st = DeskState()
    st.mode = d.get("mode", "PAPER")
    # Rebase the daemon's uptime onto this machine's clock so `st.uptime` reads
    # as the daemon's, not as the age of this process.
    st.started_mono = now - float(d.get("uptime") or 0.0)
    if d.get("started_at"):
        st.started_at = _load(d["started_at"])
    for key in (
        "starting_equity", "equity", "cash", "open_cost",
        "day_start_equity", "peak_equity",
    ):
        setattr(st, key, float(d.get(key) or 0.0))
    st.paused = bool(d.get("paused"))
    st.autopilot = bool(d.get("autopilot"))
    st.focus = d.get("focus", "all")
    st.notice = d.get("notice", "")
    st.notice_until = now + float(d.get("notice_left") or 0.0)

    st.sleeves = {k: _build(SleeveStat, v) for k, v in (d.get("sleeves") or {}).items()}
    st.book = [_build(BookRow, r) for r in (d.get("book") or [])]
    st.positions = [_build(PositionRow, r) for r in (d.get("positions") or [])]
    st.tape = deque(
        (_build(TapeEvent, r) for r in (d.get("tape") or [])), maxlen=MAX_TAPE
    )
    st.log = deque(
        ((_load(ts), lvl, msg) for ts, lvl, msg in (d.get("log") or [])), maxlen=MAX_LOG
    )
    st.curve = deque(((float(a), float(b)) for a, b in (d.get("curve") or [])), maxlen=MAX_CURVE)
    if d.get("risk"):
        st.risk = _build(RiskState, d["risk"])
    for name, g in (d.get("gates") or {}).items():
        gc = GateCounter()
        gc.counts = dict(g.get("counts") or {})
        gc.last_detail = dict(g.get("last_detail") or {})
        st.gates[name] = gc
    from stablebot.desk.health import VenueHealth

    st.venues = {k: _build(VenueHealth, v) for k, v in (d.get("venues") or {}).items()}
    return st
