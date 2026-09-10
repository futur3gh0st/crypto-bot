"""Volatility-aware fair value and signal gating for short-dated binaries.

Why this exists
---------------
The original spot_lag sleeve used two fixed constants for every coin and every
moment in a window:

    fair_up = 0.50 + 25 * ret        (crude_fair_up, scale=25)
    fire if |1m return| >= 0.003     (a flat 0.30%)

Both ignore volatility and time-to-expiry, and that has two consequences that
were visible live as "it never does anything":

1.  A flat 0.30% bar is a ~7.8 sigma event in BTC and a ~3.3 sigma event in
    DOGE. One threshold cannot mean the same thing on both books.

2.  Composing the crude fair with the catch-up entry model makes the edge gate
    binding at a far larger move than the signal gate asks for:

        edge = (1 - catchup) * (fair - 0.5) - slip
             = (1 - catchup) * scale * ret - slip

    With catchup=0.70, slip=0.02, scale=25, min_edge=0.04 that needs a 0.80%
    one-minute move. Over 3000 recent 1m bars on six majors, that happened
    zero times. The sleeve could not have traded.

What replaces them
------------------
A binary that pays if price closes above the window open, with tau minutes
left and per-minute volatility sigma, is just a normal-CDF away:

    z        = log(spot / open) / (sigma * sqrt(tau))
    fair_up  = Phi(z)

Same move, different meaning: with 4 minutes left and BTC sigma ~0.038%/min,
a +0.10% move is z = +1.3 -> fair ~0.90, not the 0.525 the crude model
returns. Sigma is estimated per symbol from a rolling EWMA of realised 1m
returns, so the gates self-calibrate to whatever regime the market is in.

This is still a simple model — driftless, constant-vol within the window, no
jump or microstructure term. It is a large improvement on a fixed constant,
not a claim of a priced vol surface, and it does not by itself make anything
profitable: it only makes "cheap versus fair" mean something comparable
across coins and across the life of a window.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

MIN_SIGMA = 1e-6
CLIP_LO = 0.02
CLIP_HI = 0.98
DEFAULT_HALFLIFE = 120.0   # bars; ~2h of 1m data
MIN_BARS = 20


def norm_cdf(z: float) -> float:
    """Phi(z) via erf. Standard library only."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# volatility
# ---------------------------------------------------------------------------


@dataclass
class VolState:
    var: float = 0.0
    n: int = 0
    recent: Deque[float] = field(default_factory=lambda: deque(maxlen=600))

    @property
    def sigma(self) -> float | None:
        if self.n < MIN_BARS:
            return None
        return math.sqrt(max(self.var, 0.0)) or None


class VolTracker:
    """Per-symbol EWMA volatility of 1-minute log returns."""

    def __init__(self, halflife: float = DEFAULT_HALFLIFE):
        self.halflife = max(2.0, halflife)
        self.lam = 0.5 ** (1.0 / self.halflife)
        self._state: dict[str, VolState] = {}

    def _st(self, symbol: str) -> VolState:
        st = self._state.get(symbol)
        if st is None:
            st = VolState()
            self._state[symbol] = st
        return st

    def update(self, symbol: str, ret: float) -> float | None:
        st = self._st(symbol)
        st.recent.append(ret)
        st.n += 1
        if st.n == 1:
            st.var = ret * ret
        else:
            st.var = self.lam * st.var + (1.0 - self.lam) * ret * ret
        return st.sigma

    def seed(self, symbol: str, rets: list[float]) -> float | None:
        """Warm the estimator from history so the first cycle is usable."""
        for r in rets:
            self.update(symbol, r)
        return self.sigma(symbol)

    def seed_from_closes(self, symbol: str, closes: list[float]) -> float | None:
        rets = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i] > 0 and closes[i - 1] > 0
        ]
        return self.seed(symbol, rets)

    def sigma(self, symbol: str) -> float | None:
        return self._st(symbol).sigma

    def zscore(self, symbol: str, ret: float) -> float | None:
        s = self.sigma(symbol)
        if s is None or s < MIN_SIGMA:
            return None
        return ret / s

    def bars(self, symbol: str) -> int:
        return self._st(symbol).n


# ---------------------------------------------------------------------------
# fair value
# ---------------------------------------------------------------------------


def vol_fair_up(
    spot: float,
    open_px: float,
    sigma_1m: float,
    remaining_min: float,
    clip: bool = True,
) -> float:
    """P(close > open) for a driftless random walk with `remaining_min` left.

    sigma_1m is the per-minute stdev of log returns for this symbol.
    """
    if spot <= 0 or open_px <= 0:
        raise ValueError("spot and open must be positive")
    if sigma_1m < MIN_SIGMA:
        raise ValueError("sigma must be positive")
    tau = max(remaining_min, 1.0 / 60.0)   # never divide by zero at the bell
    move = math.log(spot / open_px)
    z = move / (sigma_1m * math.sqrt(tau))
    p = norm_cdf(z)
    if clip:
        return min(CLIP_HI, max(CLIP_LO, p))
    return p


def implied_sigma_from_scale(scale: float) -> float:
    """What per-window sigma the old linear model was implicitly assuming.

    The crude model's slope at the money is `scale`; a normal CDF has slope
    1/(sigma*sqrt(2*pi)) there. Equating gives sigma = 1/(scale*sqrt(2*pi)).
    For scale=25 that is ~1.6% per window — roughly 20x the realised 5-minute
    move on BTC, which is why the crude fair reads decisive moves as coin flips.
    """
    return 1.0 / (scale * math.sqrt(2.0 * math.pi))



def fair_with_reference_noise(
    spot: float,
    strike: float,
    sigma_1m: float,
    minutes_left: float,
    ref_sigma: float = 0.0,
) -> tuple[float, float]:
    """Fair value and its error bar, given an uncertain reference price.

    Near expiry the distance from the strike shrinks toward zero while the
    disagreement between reference venues does not. A BTC contract three
    minutes from close sat 2.0 bp from its strike while the four CF-constituent
    venues disagreed by 3.7 bp — so the "distance" being measured was smaller
    than the ruler's own error, and the resulting fair of 0.370 was really
    0.37 +/- 0.21.

    Two things follow, and both are handled here:

    * the reference error is a second, independent source of variance, so it
      belongs inside the denominator: sigma_eff = sqrt(sigma_price^2 + ref^2).
      This pulls the fair toward 0.50 exactly when the reference is untrustworthy.

    * the caller needs to know how wide the estimate is, so a fair that cannot
      support a trade can be rejected rather than acted on.

    Returns (fair, fair_error). ref_sigma and the price vol are both fractional.
    """
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if sigma_1m < MIN_SIGMA:
        raise ValueError("sigma must be positive")
    tau = max(minutes_left, 1.0 / 60.0)
    sigma_price = sigma_1m * math.sqrt(tau)
    ref = max(0.0, ref_sigma)
    sigma_eff = math.sqrt(sigma_price * sigma_price + ref * ref)
    if sigma_eff < MIN_SIGMA:
        raise ValueError("effective sigma collapsed")
    d = math.log(spot / strike)
    fair = norm_cdf(d / sigma_eff)
    if ref <= 0:
        return min(CLIP_HI, max(CLIP_LO, fair)), 0.0
    hi = norm_cdf((d + ref) / sigma_eff)
    lo = norm_cdf((d - ref) / sigma_eff)
    err = abs(hi - lo) / 2.0
    return min(CLIP_HI, max(CLIP_LO, fair)), err


def distance_is_measurable(
    spot: float, strike: float, ref_sigma: float, min_ratio: float = 3.0
) -> tuple[bool, float]:
    """Is spot far enough from the strike to be distinguishable from venue noise?

    Returns (ok, ratio) where ratio is |log(spot/strike)| / ref_sigma.
    """
    if spot <= 0 or strike <= 0:
        return False, 0.0
    d = abs(math.log(spot / strike))
    if ref_sigma <= 0:
        return True, float("inf")
    ratio = d / ref_sigma
    return ratio >= min_ratio, ratio


# ---------------------------------------------------------------------------
# gate diagnostics — so "it never does anything" is legible on screen
# ---------------------------------------------------------------------------

GATES = (
    "no_vol",
    "no_signal",
    "window_timing",
    "already_open",
    "max_concurrent",
    "no_quote",
    "reference_noise",
    "wrong_side",
    "edge",
    "sizing",
    "risk",
    "fired",
)


@dataclass
class GateCounter:
    """Counts why entries did not happen, per sleeve, since the desk started."""

    counts: dict[str, int] = field(default_factory=lambda: {g: 0 for g in GATES})
    last_detail: dict[str, str] = field(default_factory=dict)

    def hit(self, gate: str, detail: str = "") -> None:
        self.counts[gate] = self.counts.get(gate, 0) + 1
        if detail:
            self.last_detail[gate] = detail

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def binding(self) -> tuple[str, int] | None:
        """The gate that blocked the most opportunities — the thing to fix."""
        blocked = {k: v for k, v in self.counts.items() if k != "fired" and v > 0}
        if not blocked:
            return None
        k = max(blocked, key=lambda x: blocked[x])
        return k, blocked[k]

    def top(self, n: int = 4) -> list[tuple[str, int]]:
        items = [(k, v) for k, v in self.counts.items() if v > 0]
        items.sort(key=lambda kv: -kv[1])
        return items[:n]


@dataclass
class SignalCfg:
    """Vol-normalised gates. Thresholds are in sigma, not percent."""

    z_entry: float = 2.25          # fire when |1m move| >= this many sigma
    min_edge: float = 0.04         # fair - ask, in probability units
    max_ask: float = 0.92          # never pay more than this for a binary
    min_ask: float = 0.05
    max_elapsed_sec: float = 90.0
    min_remaining_sec: float = 60.0
    vol_halflife: float = DEFAULT_HALFLIFE
    use_vol_fair: bool = True
    # The strike distance must beat the reference venues' own disagreement by
    # this factor before it counts as a measurement rather than as noise.
    min_distance_ratio: float = 3.0
    # Required edge is min_edge plus this many fair-value error bars.
    edge_uncertainty_mult: float = 2.0
    # Never buy a side the model itself does not favour.
    require_model_side: bool = True

    def describe(self) -> str:
        return (
            f"z>={self.z_entry:.1f}sigma  edge>={self.min_edge:.3f}  "
            f"dist>={self.min_distance_ratio:.0f}x ref-noise  "
            f"ask in [{self.min_ask:.2f},{self.max_ask:.2f}]"
        )
