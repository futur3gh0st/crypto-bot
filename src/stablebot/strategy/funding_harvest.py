from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from stablebot.market.funding import (
    cost_usd,
    funding_cash,
    harvest_side,
    open_close_cost_bps,
    should_enter,
    should_exit,
    trailing_avg,
)


@dataclass
class FundingPosition:
    inst_id: str
    side: int  # +1 short perp / long spot
    notional: float
    entry_ts: object
    entry_fee: float
    collected: float = 0.0
    prints: int = 0


@dataclass
class FundingState:
    """No-lookahead harvest state: observe print, then decide."""

    min_funding: float = 0.0003
    exit_funding: float = 0.00005
    trail: int = 3
    cooldown_hours: float = 24.0
    rates: list[float] = field(default_factory=list)
    position: FundingPosition | None = None
    last_exit_ts: datetime | None = None

    def cooldown_clear(self, ts) -> bool:
        """True if this symbol may enter (no exit in the last cooldown_hours)."""
        if self.last_exit_ts is None:
            return True
        try:
            return ts - self.last_exit_ts >= timedelta(hours=self.cooldown_hours)
        except TypeError:
            return True

    def on_print(
        self,
        inst_id: str,
        ts,
        rate: float,
        *,
        can_enter: bool,
        notional: float,
        one_way_cost_bps: float,
    ) -> list[dict]:
        """Process one settled funding print. Collect first if already in, then enter/exit.

        Entry happens AFTER this print (do not collect the signal print).
        Exit happens AFTER collecting this print (held through settlement).
        """
        events: list[dict] = []
        self.rates.append(float(rate))

        if self.position is not None:
            cash = funding_cash(self.position.notional, rate, self.position.side)
            self.position.collected += cash
            self.position.prints += 1
            events.append(
                {
                    "kind": "funding_accrual",
                    "inst_id": inst_id,
                    "ts": ts,
                    "rate": rate,
                    "cash": cash,
                    "side": self.position.side,
                    "notional": self.position.notional,
                }
            )
            if should_exit(self.rates, self.exit_funding, self.trail):
                fee = cost_usd(self.position.notional, one_way_cost_bps)
                events.append(
                    {
                        "kind": "funding_exit",
                        "inst_id": inst_id,
                        "ts": ts,
                        "side": self.position.side,
                        "notional": self.position.notional,
                        "fee": fee,
                        "collected": self.position.collected,
                        "reason": "signal",
                    }
                )
                self.last_exit_ts = ts
                self.position = None
                return events

        if (
            self.position is None
            and can_enter
            and self.cooldown_clear(ts)
            and should_enter(self.rates, self.min_funding, self.trail)
        ):
            side = harvest_side(self.rates, self.trail)
            if side is None or notional <= 0:
                return events
            fee = cost_usd(notional, one_way_cost_bps)
            self.position = FundingPosition(
                inst_id=inst_id,
                side=side,
                notional=notional,
                entry_ts=ts,
                entry_fee=fee,
            )
            events.append(
                {
                    "kind": "funding_enter",
                    "inst_id": inst_id,
                    "ts": ts,
                    "side": side,
                    "notional": notional,
                    "fee": fee,
                    "avg": trailing_avg(self.rates, self.trail),
                }
            )
        return events


def one_way_bps(cfg_funding) -> float:
    spot = cfg_funding.spot_maker_bps if cfg_funding.use_maker else cfg_funding.spot_taker_bps
    perp = cfg_funding.perp_maker_bps if cfg_funding.use_maker else cfg_funding.perp_taker_bps
    return open_close_cost_bps(spot, perp, cfg_funding.half_spread_bps)
