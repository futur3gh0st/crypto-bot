"""Adaptive capital allocator + auto-tuner. This is the "runs itself" brain.

Every rebalance it does three things:

  1. Scores each sleeve from its own realised results (expectancy per trade,
     normalised by the clip it was risking) and hands out capital in
     proportion to score. No score, no history -> a small exploration stake.

  2. Cuts off sleeves that are demonstrably losing (negative expectancy over a
     minimum sample) and re-probes them later with a token stake, so a sleeve
     that stops working gets benched but never permanently written off.

  3. Auto-tunes the one parameter that actually moves the needle on a binary
     book: the minimum edge required to enter. A binary bought at p needs a
     true hit rate above p to break even, so when realised hit rate sits below
     the average entry price the entry bar is raised, and when it sits
     comfortably above, the bar relaxes back toward the configured floor.

None of this manufactures edge. It reallocates toward whatever is currently
paying and shrinks whatever is not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from stablebot.desk.state import DeskState, SleeveStat

MIN_SAMPLE = 8            # resolved trades before a sleeve can be judged
# A sleeve losing this much of the pot *since it last came back* is benched.
# Measured from a baseline rather than from zero: session realized PnL never
# recovers, so a fixed lifetime threshold pins a sleeve off permanently — it
# gets re-probed, is still past the line, and is benched again on the very
# next rebalance. Overridable per desk via AllocatorCfg.loss_bench_frac.
LOSS_BENCH_FRAC = 0.06
PROBE_STAKE = 0.05        # fraction of pot kept on a benched sleeve's re-probe
BENCH_SECONDS = 45 * 60   # how long a losing sleeve sits out before re-probing
EXPLORE_FRAC = 0.20       # pot reserved for sleeves with no track record yet


@dataclass
class AllocatorCfg:
    pot: float = 1000.0
    max_sleeve_frac: float = 0.60      # no sleeve gets more than this of the pot
    min_sleeve_frac: float = 0.05
    clip_frac: float = 0.05            # per-trade clip as a fraction of allocation
    # A binary bought outright loses the whole stake, so the clip is the unit
    # the daily stop is spent in. $15 against a 4% stop leaves room for a
    # losing run; $50 left room for one.
    max_clip: float = 15.0
    min_clip: float = 5.0
    rebalance_every: float = 60.0      # seconds
    tune_every: float = 300.0          # seconds
    min_edge_floor: float = 0.03
    min_edge_ceiling: float = 0.15
    edge_step: float = 0.01
    z_floor: float = 1.5
    z_ceiling: float = 4.0
    z_step: float = 0.25
    loss_bench_frac: float = LOSS_BENCH_FRAC


class Allocator:
    def __init__(self, cfg: AllocatorCfg | None = None):
        self.cfg = cfg or AllocatorCfg()
        self._last_rebalance = 0.0
        self._last_tune = 0.0

    # ---- scoring -------------------------------------------------------

    @staticmethod
    def score(stat: SleeveStat) -> float | None:
        """Expectancy per dollar risked, over the rolling window. None = unproven."""
        exp = stat.expectancy
        if exp is None or len(stat.recent) < MIN_SAMPLE:
            return None
        risked = stat.clip if stat.clip > 0 else 1.0
        return exp / risked

    # ---- allocation ----------------------------------------------------

    def rebalance(self, state: DeskState, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._last_rebalance < self.cfg.rebalance_every:
            return False
        self._last_rebalance = now

        sleeves = list(state.sleeves.values())
        if not sleeves:
            return False

        pot = self.cfg.pot if self.cfg.pot > 0 else state.equity
        pot = max(pot, 0.0)

        # 1. bench / un-bench on realised performance
        for s in sleeves:
            # Waiting for a full sample while a sleeve bleeds is itself a
            # decision. A sleeve down this much of the pot is benched now, at
            # whatever sample it has, and re-probed later like any other.
            loss_cap = pot * self.cfg.loss_bench_frac
            drop = s.realized - s.bench_baseline
            if (
                state.autopilot
                and s.enabled
                and loss_cap > 0
                and drop <= -loss_cap
                and s.decided >= 1
            ):
                s.enabled = False
                s.disabled_reason = (
                    f"auto: down {drop:+.2f} since it came back "
                    f"({self.cfg.loss_bench_frac*100:.0f}% of pot) after {s.decided} trades "
                    f"— [e] to resume"
                )
                s.probe_at = now + BENCH_SECONDS
                s.status = "off"
                state.note("warn", f"{s.label} benched — {s.disabled_reason}")
                continue
            sc = self.score(s)
            if sc is None:
                # unproven: leave enabled unless the operator turned it off
                if not s.enabled and s.disabled_reason.startswith("auto:") and now >= s.probe_at:
                    s.enabled = True
                    s.disabled_reason = ""
                    s.status = "idle"
                    # Forgive what is already lost, or the next rebalance benches
                    # it straight back on the same stale number.
                    s.bench_baseline = s.realized
                    state.note("info", f"{s.label}: re-probing after bench")
                continue
            if sc < 0 and s.enabled and state.autopilot:
                s.enabled = False
                s.disabled_reason = f"auto: negative expectancy ({s.expectancy:+.3f}/trade)"
                s.probe_at = now + BENCH_SECONDS
                s.status = "off"
                state.note(
                    "warn",
                    f"{s.label} benched — expectancy {s.expectancy:+.3f}/trade over "
                    f"{len(s.recent)} trades; re-probe in {BENCH_SECONDS//60}m",
                )
            elif sc > 0 and not s.enabled and s.disabled_reason.startswith("auto:"):
                s.enabled = True
                s.disabled_reason = ""
                s.status = "idle"
                state.note("info", f"{s.label} un-benched — expectancy back positive")

        # 2. split the pot
        active = [s for s in sleeves if s.enabled]
        benched = [s for s in sleeves if not s.enabled]

        for s in benched:
            s.allocation = 0.0
            s.alloc_frac = 0.0
            s.clip = 0.0

        if not active:
            return True

        scored = {s.name: self.score(s) for s in active}
        proven = [s for s in active if scored[s.name] is not None]
        unproven = [s for s in active if scored[s.name] is None]

        weights: dict[str, float] = {}
        if proven:
            # shift the scores positive so the worst proven sleeve still gets a sliver
            raw = [max(0.0, scored[s.name] or 0.0) for s in proven]
            total = sum(raw)
            proven_pot = 1.0 - (EXPLORE_FRAC if unproven else 0.0)
            if total <= 0:
                for s in proven:
                    weights[s.name] = proven_pot / len(proven)
            else:
                for s, r in zip(proven, raw, strict=True):   # raw is built from proven
                    weights[s.name] = proven_pot * (r / total)
        if unproven:
            share = (EXPLORE_FRAC if proven else 1.0) / len(unproven)
            for s in unproven:
                weights[s.name] = share

        weights = self._normalise(weights)
        throttle = state.risk.throttle_mult

        for s in active:
            frac = weights.get(s.name, 0.0)
            s.alloc_frac = frac
            s.allocation = pot * frac * throttle
            clip = s.allocation * self.cfg.clip_frac
            s.clip = max(0.0, min(self.cfg.max_clip, max(self.cfg.min_clip, clip)))
            if s.allocation < self.cfg.min_clip:
                s.clip = 0.0
        return True

    def _normalise(self, weights: dict[str, float]) -> dict[str, float]:
        """Weights that sum to 1 and respect the per-sleeve floor and cap.

        Clamping and then renormalising puts a capped sleeve straight back over
        its cap, so this water-fills in two phases: pin whoever exceeds the cap
        and re-split the rest among those still free, then lift anyone under the
        floor and take the difference proportionally from the others.
        """
        lo, hi = self.cfg.min_sleeve_frac, self.cfg.max_sleeve_frac
        n = len(weights)
        if n == 0:
            return {}
        if hi * n <= 1.0:
            # their caps cannot cover the pot between them; split it evenly
            return {k: 1.0 / n for k in weights}
        lo = min(lo, 1.0 / n)

        out: dict[str, float] = {k: 0.0 for k in weights}

        # phase 1 — caps
        pinned: set[str] = set()
        for _ in range(n + 2):
            free = [k for k in weights if k not in pinned]
            if not free:
                break
            mass = 1.0 - hi * len(pinned)
            raw = {k: max(0.0, weights[k]) for k in free}
            total = sum(raw.values())
            if total <= 0:
                for k in free:
                    out[k] = mass / len(free)
            else:
                for k in free:
                    out[k] = mass * raw[k] / total
            newly = {k for k in free if out[k] > hi + 1e-12}
            if not newly:
                break
            pinned |= newly
        for k in pinned:
            out[k] = hi

        # phase 2 — floors
        for _ in range(n + 2):
            below = {k for k in out if out[k] < lo - 1e-12}
            if not below:
                break
            rest = [k for k in out if k not in below]
            if not rest:
                break
            for k in below:
                out[k] = lo
            remaining = 1.0 - lo * len(below)
            total = sum(out[k] for k in rest)
            if total <= 0:
                for k in rest:
                    out[k] = remaining / len(rest)
            else:
                for k in rest:
                    out[k] = min(hi, remaining * out[k] / total)

        total = sum(out.values())
        if total > 0 and abs(total - 1.0) > 1e-9:
            out = {k: v / total for k, v in out.items()}
        return out

    # ---- parameter auto-tuning ----------------------------------------

    def tune(self, state: DeskState, force: bool = False) -> list[str]:
        """Nudge each sleeve's entry bar toward where its results say it belongs."""
        now = time.monotonic()
        if not force and now - self._last_tune < self.cfg.tune_every:
            return []
        self._last_tune = now
        if not state.autopilot:
            return []

        changes: list[str] = []
        for s in state.sleeves.values():
            # A sleeve that never fires earns nothing. If the gate data says the
            # signal bar is what is blocking everything and nothing has traded,
            # walk the z threshold down toward the floor rather than sit idle.
            #
            # "Never fired" means never, not just not-since-this-restart: the
            # scoreboard is replayed from the ledger at startup so a sleeve that
            # traded yesterday is not mistaken for an idle one. And a sleeve that
            # is down on its pot never gets loosened — buying more of a signal
            # that has already lost money is the wrong direction to tune.
            gc = state.gates.get(s.name)
            idle = s.trades == 0 and s.realized >= 0.0
            if gc is not None and "z_entry" in s.params and idle and gc.total > 200:
                binding = gc.binding()
                if binding and binding[0] == "no_signal":
                    cur_z = float(s.params["z_entry"])
                    new_z = max(self.cfg.z_floor, cur_z - self.cfg.z_step)
                    if new_z < cur_z:
                        s.params["z_entry"] = new_z
                        s.tuning = f"idle {gc.total} checks — z {cur_z:.1f}->{new_z:.1f}"
                        changes.append(f"{s.label}: z_entry {cur_z:.2f} -> {new_z:.2f} (no fills)")
                        continue
            if "min_edge" not in s.params:
                continue
            if s.decided < MIN_SAMPLE:
                s.tuning = f"sampling {s.decided}/{MIN_SAMPLE}"
                continue
            hit = s.win_rate
            breakeven = s.breakeven_hit_rate
            if hit is None or breakeven is None:
                continue
            cur = float(s.params["min_edge"])
            margin = hit - breakeven
            if margin < -0.02:
                new = min(self.cfg.min_edge_ceiling, cur + self.cfg.edge_step)
                s.tuning = f"hit {hit*100:.0f}% < breakeven {breakeven*100:.0f}% — tighten"
            elif margin > 0.06:
                new = max(self.cfg.min_edge_floor, cur - self.cfg.edge_step)
                s.tuning = f"hit {hit*100:.0f}% > breakeven {breakeven*100:.0f}% — relax"
            else:
                s.tuning = f"hit {hit*100:.0f}% vs breakeven {breakeven*100:.0f}% — hold"
                continue
            if abs(new - cur) > 1e-9:
                s.params["min_edge"] = new
                changes.append(f"{s.label}: min_edge {cur:.3f} -> {new:.3f} ({s.tuning})")
        for c in changes:
            state.note("info", f"auto-tune {c}")
        return changes
