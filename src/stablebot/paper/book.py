from __future__ import annotations

import json
from dataclasses import dataclass

from stablebot.config import AppConfig
from stablebot.paper.ledger import Ledger
from stablebot.strategy.funding_harvest import one_way_bps
from stablebot.strategy.live_book import LiveBookSignals
from stablebot.strategy.risk import RiskDecision
from stablebot.market.funding import cost_usd
from stablebot.market.spreads import SpreadOpportunity
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BookPaperFill:
    strategy: str
    pair: str
    notional: float
    pnl: float
    fees_paid: float
    skipped: bool
    reason: str


class BookPaperEngine:
    """In-process paper book for `run`. Positions persist in ledger meta."""

    META_KEY = "book_positions"

    def __init__(self, cfg: AppConfig, ledger: Ledger, equity: float | None = None):
        self.cfg = cfg
        self.ledger = ledger
        self.equity = float(equity or cfg.strategy.paper_notional_usd)
        raw = ledger.get_note(self.META_KEY)
        self.state = json.loads(raw) if raw else {"funding": {}, "depeg": {}}

    def _save(self) -> None:
        self.ledger.note(self.META_KEY, json.dumps(self.state))

    def _dummy_opp(self, kind: str, pair: str) -> SpreadOpportunity:
        return SpreadOpportunity(
            kind=kind,
            pair=pair,
            buy_venue="paper",
            sell_venue="paper",
            buy_base=pair.split("/")[0] if "/" in pair else pair,
            buy_quote="USDT",
            sell_base=pair.split("/")[0] if "/" in pair else pair,
            sell_quote="USDT",
            buy_px=1.0,
            sell_px=1.0,
            buy_fee_bps=0.0,
            sell_fee_bps=0.0,
            gross_bps=0.0,
            fee_bps=0.0,
            net_bps=0.0,
            ts=_now(),
        )

    def step(self, live: LiveBookSignals, decision: RiskDecision) -> list[BookPaperFill]:
        fills: list[BookPaperFill] = []
        # funding: enter when signal and not already in
        ow = one_way_bps(self.cfg.funding)
        used_f = sum(v.get("notional", 0.0) for v in self.state["funding"].values())
        sleeve_f = self.equity * min(self.cfg.funding.sleeve_max, self.cfg.risk.funding_sleeve_max)
        for row in live.funding:
            if row.error or not row.enter:
                continue
            if row.inst_id in self.state["funding"]:
                fills.append(BookPaperFill("funding", row.inst_id, 0, 0, 0, True, "already in"))
                continue
            room = max(0.0, sleeve_f - used_f)
            notional = min(self.equity * self.cfg.funding.equity_frac, room)
            if notional < 10:
                fills.append(BookPaperFill("funding", row.inst_id, 0, 0, 0, True, "no sleeve room"))
                continue
            fee = cost_usd(notional, ow)
            self.state["funding"][row.inst_id] = {
                "notional": notional,
                "side": 1 if (row.trail_avg or 0) > 0 else -1,
            }
            used_f += notional
            self.equity -= fee
            opp = self._dummy_opp("funding", row.inst_id)
            self.ledger.record_fill(opp, notional, -fee, fee, 1.0, "funding enter (paper)")
            fills.append(BookPaperFill("funding", row.inst_id, notional, -fee, fee, False, "enter"))

        # depeg: X fear cuts size or skips
        used_d = sum(v.get("notional", 0.0) for v in self.state["depeg"].values())
        sleeve_d = self.equity * min(self.cfg.depeg_fade.sleeve_max, self.cfg.risk.depeg_sleeve_max)
        size_mult = 1.0
        skip = False
        reason = "depeg enter (paper)"
        if not decision.trade:
            skip = True
            reason = decision.reason
        else:
            size_mult = decision.size_mult
            if size_mult < 1:
                reason = decision.reason
        for row in live.depegs:
            if not row.enter or row.mid is None:
                continue
            if row.asset in self.state["depeg"]:
                fills.append(BookPaperFill("depeg", row.pair, 0, 0, 0, True, "already in (no restack)"))
                continue
            if skip:
                fills.append(BookPaperFill("depeg", row.pair, 0, 0, 0, True, reason))
                continue
            name_cap = self.equity * min(self.cfg.depeg_fade.per_name_max, self.cfg.risk.depeg_per_name_max)
            room = max(0.0, min(sleeve_d - used_d, name_cap)) * size_mult
            if room < 10:
                fills.append(BookPaperFill("depeg", row.pair, 0, 0, 0, True, "no sleeve / fear cut to ~0"))
                continue
            fee_bps = self.cfg.fee_bps(row.venue) + self.cfg.strategy.hist_half_spread_bps
            fee = room * fee_bps / 10_000.0
            self.state["depeg"][row.asset] = {"notional": room, "px": row.mid, "pair": row.pair}
            used_d += room
            self.equity -= fee
            opp = self._dummy_opp("depeg_fade", row.pair)
            self.ledger.record_fill(opp, room, -fee, fee, size_mult, reason)
            fills.append(BookPaperFill("depeg", row.pair, room, -fee, fee, False, reason))
        self._save()
        return fills
