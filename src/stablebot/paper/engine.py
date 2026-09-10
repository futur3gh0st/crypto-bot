from __future__ import annotations

from dataclasses import dataclass

from stablebot.config import AppConfig
from stablebot.market.spreads import SpreadOpportunity
from stablebot.paper.ledger import Ledger
from stablebot.strategy.risk import RiskDecision
from stablebot.strategy.stable_spread import is_fillable, opp_key


@dataclass
class PaperFill:
    notional: float
    pnl: float
    fees_paid: float
    size_mult: float
    skipped: bool
    reason: str


def simulate_fill(opp: SpreadOpportunity, notional: float) -> tuple[float, float]:
    """Return (pnl, fees_paid) for a two-leg taker arb of `notional` quote units."""
    units = notional / opp.buy_px
    buy_cost = units * opp.buy_px * (1.0 + opp.buy_fee_bps / 10_000.0)
    sell_proceeds = units * opp.sell_px * (1.0 - opp.sell_fee_bps / 10_000.0)
    fees = (buy_cost - units * opp.buy_px) + (units * opp.sell_px - sell_proceeds)
    return sell_proceeds - buy_cost, fees


class PaperEngine:
    def __init__(self, cfg: AppConfig, ledger: Ledger | None = None):
        self.cfg = cfg
        self.ledger = ledger or Ledger()
        self.open: set[tuple] = set()

    def sync_open(self, live_opps: list[SpreadOpportunity]) -> None:
        live = {opp_key(o) for o in live_opps if is_fillable(o, self.cfg)}
        self.open &= live

    def maybe_fill(
        self,
        opp: SpreadOpportunity,
        decision: RiskDecision,
        notional: float | None = None,
    ) -> PaperFill:
        if not is_fillable(opp, self.cfg):
            return PaperFill(0.0, 0.0, 0.0, 0.0, True, "cross_pair signal only (not a closed arb)")
        key = opp_key(opp)
        if key in self.open:
            return PaperFill(0.0, 0.0, 0.0, 0.0, True, "already in this basis")
        if not decision.trade:
            return PaperFill(0.0, 0.0, 0.0, 0.0, True, decision.reason)
        base = notional if notional is not None else self.cfg.strategy.paper_notional_usd
        sized = max(0.0, base * decision.size_mult)
        if sized <= 0:
            return PaperFill(0.0, 0.0, 0.0, decision.size_mult, True, "zero size")
        pnl, fees = simulate_fill(opp, sized)
        self.ledger.record_fill(
            opp, sized, pnl, fees, decision.size_mult, decision.reason
        )
        self.open.add(key)
        return PaperFill(sized, pnl, fees, decision.size_mult, False, decision.reason)
