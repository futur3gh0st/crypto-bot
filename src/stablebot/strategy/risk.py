from __future__ import annotations

from dataclasses import dataclass

from stablebot.config import AppConfig
from stablebot.signals.trends import TrendRollup


@dataclass(frozen=True)
class RiskDecision:
    trade: bool
    size_mult: float
    reason: str


def decide(cfg: AppConfig, trend: TrendRollup | None) -> RiskDecision:
    if trend is None:
        return RiskDecision(True, 1.0, "no X overlay")
    if trend.fear_score >= cfg.risk.fear_skip_threshold:
        return RiskDecision(
            False,
            0.0,
            f"skip: hourly fear {trend.fear_score:.2f} >= {cfg.risk.fear_skip_threshold:.2f}",
        )
    if trend.fear_score >= cfg.risk.fear_cut_threshold:
        return RiskDecision(
            True,
            cfg.risk.fear_size_mult,
            f"cut size x{cfg.risk.fear_size_mult}: fear {trend.fear_score:.2f}",
        )
    return RiskDecision(True, 1.0, f"normal size: fear {trend.fear_score:.2f}")
