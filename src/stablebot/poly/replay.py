"""Replay math for Polymarket Up/Down paper backtest. No I/O, no lookahead."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable


FeeFn = Callable[[float], float]


def poly_taker_fee(p: float) -> float:
    """Official-style crypto taker: 0.07 * p * (1-p) per side. No rebate."""
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return 0.07 * p * (1.0 - p)


def zero_fee(_p: float) -> float:
    return 0.0


def filter_window_history(
    points: Iterable[dict[str, Any] | tuple[int, float]],
    window_start: int,
    window_end: int,
) -> list[tuple[int, float]]:
    """Keep prints with t in [window_start, window_end]. Drops pre-market and post-close.

    window_end is the close instant (start + interval). A print at exactly
    window_end is kept; anything after is the next window / resolution tape.
    """
    out: list[tuple[int, float]] = []
    for raw in points:
        if isinstance(raw, dict):
            t = int(raw["t"])
            p = float(raw["p"])
        else:
            t, p = int(raw[0]), float(raw[1])
        if window_start <= t <= window_end:
            out.append((t, p))
    out.sort(key=lambda x: x[0])
    return out


@dataclass(frozen=True)
class AlignedPrint:
    t: int  # max(t_up, t_down) — both prints already exist (no lookahead)
    t_up: int
    t_down: int
    p_up: float
    p_down: float

    @property
    def sum_p(self) -> float:
        return self.p_up + self.p_down

    @property
    def lock(self) -> float:
        return 1.0 - self.sum_p


def aligned_pairs(
    up: Iterable[tuple[int, float]],
    down: Iterable[tuple[int, float]],
    align_tol_sec: int = 15,
) -> list[AlignedPrint]:
    """Pair last prints of each side when |t_up - t_down| <= tol.

    Walks in time order. After each print, if both sides have a last print
    and those two timestamps are within tol, emit one pair. Decision time is
    max(t_up, t_down) so the later print is already on the tape.
    """
    events: list[tuple[int, str, float]] = []
    for t, p in up:
        events.append((int(t), "up", float(p)))
    for t, p in down:
        events.append((int(t), "down", float(p)))
    events.sort(key=lambda x: (x[0], 0 if x[1] == "up" else 1))
    last_p: dict[str, float] = {}
    last_t: dict[str, int] = {}
    out: list[AlignedPrint] = []
    seen: set[tuple[int, int]] = set()
    for t, side, p in events:
        last_p[side] = p
        last_t[side] = t
        if "up" not in last_p or "down" not in last_p:
            continue
        tu, td = last_t["up"], last_t["down"]
        if abs(tu - td) > align_tol_sec:
            continue
        key = (tu, td)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            AlignedPrint(
                t=max(tu, td),
                t_up=tu,
                t_down=td,
                p_up=last_p["up"],
                p_down=last_p["down"],
            )
        )
    return out


def quoted(p: float | None) -> bool:
    return p is not None and 0.0 < p < 1.0


def first_lock(
    pairs: Iterable[AlignedPrint],
    min_lock: float = 0.005,
) -> AlignedPrint | None:
    """First aligned print where p_up + p_down <= 1 - min_lock. No lookahead to the best."""
    for pr in pairs:
        if not quoted(pr.p_up) or not quoted(pr.p_down):
            continue
        if pr.sum_p <= 1.0 - min_lock:
            return pr
    return None


def first_fade(
    pairs: Iterable[AlignedPrint],
    window_start: int,
    interval_sec: int,
    threshold: float = 0.08,
    early_frac: float = 1.0 / 3.0,
) -> tuple[AlignedPrint, str] | None:
    """Buy the cheap side when |p - 0.5| >= threshold, only early in the window.

    If both sides are cheap enough that a lock should have fired, skip fade.
    """
    early_end = window_start + int(interval_sec * early_frac)
    for pr in pairs:
        if pr.t < window_start or pr.t > early_end:
            continue
        if not quoted(pr.p_up) or not quoted(pr.p_down):
            continue
        cheap_up = (0.5 - pr.p_up) >= threshold
        cheap_down = (0.5 - pr.p_down) >= threshold
        if cheap_up and cheap_down:
            return None  # pair-complete territory; do not fade
        if cheap_up:
            return pr, "up"
        if cheap_down:
            return pr, "down"
    return None


def lock_pnl_per_share(p_up: float, p_down: float, fee_fn: FeeFn = zero_fee) -> float:
    """Locked edge: 1 - p_up - p_down - fee(up) - fee(down). Fee never negative."""
    fee = max(0.0, fee_fn(p_up)) + max(0.0, fee_fn(p_down))
    return 1.0 - p_up - p_down - fee


def fade_pnl_per_share(p: float, won: bool, fee_fn: FeeFn = zero_fee) -> float:
    payout = 1.0 if won else 0.0
    return payout - p - max(0.0, fee_fn(p))


def winning_side(outcomes: Any, outcome_prices: Any) -> str | None:
    """Resolution from Gamma outcomePrices only. Not from last/mid tape.

    Requires a resolved 0/1 vector. Ambiguous or in-progress prices → None.
    """
    outs = _as_str_list(outcomes)
    pxs = _as_float_list(outcome_prices)
    if not outs or len(outs) != len(pxs):
        return None
    winners: list[str] = []
    for name, px in zip(outs, pxs):
        key = name.strip().lower()
        if px >= 1.0 - 1e-9:
            winners.append(key)
        elif px <= 1e-9:
            continue
        else:
            return None
    if len(winners) == 1 and winners[0] in {"up", "down"}:
        return winners[0]
    return None


def _as_str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw]


def _as_float_list(raw: Any) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    out: list[float] = []
    for x in raw:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            return []
    return out


@dataclass
class WindowTrade:
    kind: str  # lock | fade
    slug: str
    coin: str
    minutes: int
    start_unix: int
    end_unix: int
    t: int
    p_up: float | None
    p_down: float | None
    side: str | None
    fill_px: float
    shares: float
    pnl: float
    pnl_fee: float
    fee: float
    winner: str | None
    note: str = ""


@dataclass
class WindowResult:
    slug: str
    coin: str
    minutes: int
    start_unix: int
    end_unix: int
    fetched: bool
    resolved: bool
    winner: str | None
    n_up: int
    n_down: int
    n_pairs: int
    lock: WindowTrade | None = None
    fade: WindowTrade | None = None
    skip_reason: str | None = None


def replay_window(
    *,
    slug: str,
    coin: str,
    minutes: int,
    start_unix: int,
    end_unix: int,
    hist_up: Iterable[dict[str, Any] | tuple[int, float]],
    hist_down: Iterable[dict[str, Any] | tuple[int, float]],
    outcomes: Any,
    outcome_prices: Any,
    shares: float = 20.0,
    min_lock: float = 0.005,
    fade_threshold: float = 0.08,
    early_frac: float = 1.0 / 3.0,
    align_tol_sec: int = 15,
    allow_fade: bool = True,
) -> WindowResult:
    """Replay one closed window. Resolution from outcomePrices only."""
    interval = end_unix - start_unix
    winner = winning_side(outcomes, outcome_prices)
    up = filter_window_history(hist_up, start_unix, end_unix)
    down = filter_window_history(hist_down, start_unix, end_unix)
    pairs = aligned_pairs(up, down, align_tol_sec=align_tol_sec)
    res = WindowResult(
        slug=slug,
        coin=coin,
        minutes=minutes,
        start_unix=start_unix,
        end_unix=end_unix,
        fetched=True,
        resolved=winner is not None,
        winner=winner,
        n_up=len(up),
        n_down=len(down),
        n_pairs=len(pairs),
    )
    if winner is None:
        res.skip_reason = "unresolved"
        return res

    lock_pr = first_lock(pairs, min_lock=min_lock)
    if lock_pr is not None:
        pnl0 = shares * lock_pnl_per_share(lock_pr.p_up, lock_pr.p_down, zero_fee)
        pnl_f = shares * lock_pnl_per_share(lock_pr.p_up, lock_pr.p_down, poly_taker_fee)
        fee = abs(pnl0 - pnl_f)
        res.lock = WindowTrade(
            kind="lock",
            slug=slug,
            coin=coin,
            minutes=minutes,
            start_unix=start_unix,
            end_unix=end_unix,
            t=lock_pr.t,
            p_up=lock_pr.p_up,
            p_down=lock_pr.p_down,
            side=None,
            fill_px=lock_pr.sum_p,
            shares=shares,
            pnl=pnl0,
            pnl_fee=pnl_f,
            fee=fee,
            winner=winner,
            note="pair-complete on last/mid; may be unfillable at ask",
        )

    # Fade is residual: only if no lock in this window (lock is primary).
    if allow_fade and res.lock is None:
        faded = first_fade(
            pairs,
            window_start=start_unix,
            interval_sec=interval,
            threshold=fade_threshold,
            early_frac=early_frac,
        )
        if faded is not None:
            pr, side = faded
            px = pr.p_up if side == "up" else pr.p_down
            won = winner == side
            pnl0 = shares * fade_pnl_per_share(px, won, zero_fee)
            pnl_f = shares * fade_pnl_per_share(px, won, poly_taker_fee)
            res.fade = WindowTrade(
                kind="fade",
                slug=slug,
                coin=coin,
                minutes=minutes,
                start_unix=start_unix,
                end_unix=end_unix,
                t=pr.t,
                p_up=pr.p_up,
                p_down=pr.p_down,
                side=side,
                fill_px=px,
                shares=shares,
                pnl=pnl0,
                pnl_fee=pnl_f,
                fee=abs(pnl0 - pnl_f),
                winner=winner,
                note="DIRECTIONAL fade on last/mid; settle at resolution; not a lock",
            )
    return res
