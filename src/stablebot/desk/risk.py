"""Risk governor: the thing that lets the desk run unattended without you watching.

Three independent brakes, checked every cycle:
  1. daily stop      — equity down more than daily_stop_pct vs UTC-day open
  2. drawdown halt   — equity down more than max_drawdown_pct vs session peak
  3. halt file       — data/desk_halt exists (drop a file, everything stops)

Between "fine" and "stopped" there is a THROTTLED band where sizing is cut
rather than trading stopped outright, so a normal losing streak shrinks risk
instead of ending the session.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from stablebot.config import AppConfig, data_dir
from stablebot.desk.state import DeskState

HALT_FILE = "desk_halt"
DAY_FILE = "desk_day.json"


def halt_path() -> Path:
    return data_dir() / HALT_FILE


def halt_present() -> bool:
    return halt_path().exists()


def clear_halt() -> bool:
    p = halt_path()
    if p.exists():
        p.unlink()
        return True
    return False


def raise_halt(reason: str = "manual") -> Path:
    p = halt_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{datetime.now(timezone.utc).isoformat()} {reason}\n", encoding="utf-8")
    return p


def _day_key(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def day_file() -> Path:
    return data_dir() / DAY_FILE


def load_day_anchor() -> tuple[str, float] | None:
    """The equity this UTC day opened at, so a restart cannot reset the stop."""
    p = day_file()
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        key = str(raw["day_key"])
        eq = float(raw["day_start_equity"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if key != _day_key() or eq <= 0:
        return None
    return key, eq


def save_day_anchor(key: str, equity: float) -> None:
    p = day_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"day_key": key, "day_start_equity": equity}), encoding="utf-8"
    )
    tmp.replace(p)


class RiskGovernor:
    """Owns state.risk. Sleeves ask `may_trade()` before arming anything."""

    def __init__(
        self,
        cfg: AppConfig,
        daily_stop_pct: float | None = None,
        max_drawdown_pct: float = 0.25,
        throttle_at_pct: float = 0.5,
    ):
        self.cfg = cfg
        self.daily_stop_pct = (
            daily_stop_pct if daily_stop_pct is not None else cfg.risk.daily_stop_pct
        )
        self.max_drawdown_pct = max_drawdown_pct
        # fraction of the daily stop at which we start cutting size
        self.throttle_at_pct = throttle_at_pct
        self._halt_logged = False

    def bind(self, state: DeskState) -> None:
        state.risk.daily_stop_pct = self.daily_stop_pct
        state.risk.max_drawdown_pct = self.max_drawdown_pct
        state.risk.day_key = _day_key()
        # A daily stop that resets whenever the process restarts is not a stop.
        # Reuse today's anchor if one was already written.
        anchor = load_day_anchor()
        if anchor is not None:
            state.risk.day_key, state.risk.day_start_equity = anchor
            state.note(
                "info",
                f"daily stop anchored at {anchor[1]:,.2f} from earlier today",
            )
        else:
            state.risk.day_start_equity = state.equity or state.starting_equity
            save_day_anchor(state.risk.day_key, state.risk.day_start_equity)
        state.day_start_equity = state.risk.day_start_equity
        state.risk.peak_equity = max(state.equity, state.starting_equity)
        state.peak_equity = state.risk.peak_equity

    def roll_day_if_needed(self, state: DeskState) -> None:
        key = _day_key()
        if key != state.risk.day_key:
            state.risk.day_key = key
            state.risk.day_start_equity = state.equity
            state.day_start_equity = state.equity
            save_day_anchor(key, state.equity)
            state.note("info", f"UTC day rolled — daily stop re-based at ${state.equity:,.2f}")
            if state.risk.mode == "HALTED" and "daily stop" in state.risk.reason:
                state.risk.mode = "RUNNING"
                state.risk.reason = ""
                self._halt_logged = False
                state.note("info", "daily-stop halt cleared by day roll")

    @staticmethod
    def update_budget(state: DeskState) -> None:
        """How much more can be lost today, and how much may still be staked.

        For a binary bought outright the maximum loss is the stake, so open
        cost is exactly the risk already committed. Sizing against what is left
        of the daily stop is what stops two simultaneous clips from blowing
        through it — the first session put 3.3% at risk against a 2.0% stop and
        overshot it to 221%.
        """
        r = state.risk
        base = r.day_start_equity or state.starting_equity
        r.budget_total = max(0.0, base * r.daily_stop_pct)
        lost = max(0.0, -state.day_pnl)
        r.budget_remaining = max(0.0, r.budget_total - lost)
        r.capacity = max(0.0, r.budget_remaining - state.open_cost)

    def evaluate(self, state: DeskState) -> str:
        """Update and return the risk mode."""
        self.roll_day_if_needed(state)
        self.update_budget(state)
        # mark_equity() also tracks a peak; take the highest of all three so a
        # drop between supervisor ticks cannot quietly reset the high-water mark
        # and disarm the drawdown halt.
        peak = max(state.risk.peak_equity, state.peak_equity, state.equity)
        state.risk.peak_equity = peak
        state.peak_equity = peak

        if state.paused:
            state.risk.mode = "PAUSED"
            state.risk.reason = "paused by operator"
            state.risk.throttle_mult = 0.0
            return state.risk.mode

        if halt_present():
            state.risk.mode = "HALTED"
            state.risk.reason = f"halt file present ({halt_path()})"
            state.risk.throttle_mult = 0.0
            if not self._halt_logged:
                state.note("warn", "halt file present — no new entries")
                self._halt_logged = True
            return state.risk.mode
        self._halt_logged = False

        day_pnl_pct = state.day_pnl_pct
        dd = state.drawdown

        if day_pnl_pct <= -self.daily_stop_pct:
            state.risk.mode = "HALTED"
            state.risk.reason = (
                f"daily stop hit ({day_pnl_pct*100:+.2f}% <= -{self.daily_stop_pct*100:.2f}%)"
            )
            state.risk.throttle_mult = 0.0
            return state.risk.mode

        if dd <= -self.max_drawdown_pct:
            state.risk.mode = "HALTED"
            state.risk.reason = (
                f"max drawdown hit ({dd*100:+.2f}% <= -{self.max_drawdown_pct*100:.2f}%)"
            )
            state.risk.throttle_mult = 0.0
            return state.risk.mode

        # Throttle band: scale size down linearly from full to quarter as the
        # day PnL walks from the throttle trigger toward the stop.
        trigger = -self.daily_stop_pct * self.throttle_at_pct
        if day_pnl_pct <= trigger:
            span = max(1e-9, self.daily_stop_pct - self.daily_stop_pct * self.throttle_at_pct)
            travelled = min(1.0, (trigger - day_pnl_pct) / span)
            state.risk.throttle_mult = max(0.25, 1.0 - 0.75 * travelled)
            state.risk.mode = "THROTTLED"
            state.risk.reason = (
                f"day {day_pnl_pct*100:+.2f}% — size x{state.risk.throttle_mult:.2f}"
            )
            return state.risk.mode

        state.risk.throttle_mult = 1.0
        state.risk.mode = "RUNNING"
        state.risk.reason = ""
        return state.risk.mode

    @staticmethod
    def may_trade(state: DeskState) -> bool:
        return state.risk.mode in {"RUNNING", "THROTTLED"}

    @staticmethod
    def clip_cap(state: DeskState) -> float:
        """Largest new stake the daily-stop budget can still absorb."""
        return max(0.0, state.risk.capacity * state.risk.throttle_mult)
