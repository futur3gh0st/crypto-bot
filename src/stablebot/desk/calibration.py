"""Model calibration — does the fair-value model beat the price you paid?

A fair value is a probability claim, so the honest test is not P&L over a
handful of trades. It is whether the claimed probabilities match observed
frequencies, and whether they match them *better than the ask*, because the
ask is the market's own probability claim. A model that cannot beat the price
is paying fees to express a view no sharper than the quote it is lifting.

Two numbers carry the verdict:

  Brier(model)  = mean (p_model  - outcome)^2
  Brier(market) = mean (p_market - outcome)^2   where p_market = entry price

Lower is better. A skill score of 0 means the model matched the market; below
0 means the price was the better forecast.

Everything here reads the paper ledgers the sleeves already write. Nothing
here places, sizes or blocks a trade.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Below this many resolved trades the buckets are noise. Say so, rather than
# letting a 2-of-3 bucket read as a finding.
MIN_MEANINGFUL = 30


@dataclass(frozen=True)
class Outcome:
    """One resolved position, joined from its fill record and its resolve."""

    ticker: str
    coin: str
    side: str
    p_model: float       # model's P(this side wins)
    p_market: float      # entry price = market's P(this side wins)
    won: bool
    pnl: float
    ts: str

    @property
    def y(self) -> float:
        return 1.0 if self.won else 0.0


@dataclass(frozen=True)
class Bucket:
    lo: float
    hi: float
    n: int
    mean_p: float        # mean predicted probability in the bucket
    realized: float      # observed win frequency

    @property
    def gap(self) -> float:
        """Positive means the model claimed more than it delivered."""
        return self.mean_p - self.realized


@dataclass(frozen=True)
class Report:
    outcomes: list[Outcome]
    buckets: list[Bucket]
    brier_model: float
    brier_market: float
    brier_base: float    # always predicting the overall base rate
    base_rate: float

    @property
    def n(self) -> int:
        return len(self.outcomes)

    @property
    def skill_vs_market(self) -> float:
        """1 - Brier(model)/Brier(market). Above 0 = model beat the price."""
        if self.brier_market <= 0:
            return 0.0
        return 1.0 - self.brier_model / self.brier_market

    @property
    def meaningful(self) -> bool:
        return self.n >= MIN_MEANINGFUL


def _read_jsonl(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def load_outcomes(path: Path, fill_kind: str = "kalshi_lag", key: str = "ticker") -> list[Outcome]:
    """Join fills (which carry the model's fair) to resolves (which carry the result).

    The resolve record does not repeat `fair`, so the model's claim has to come
    from the fill. `fair` is P(YES) — see fair_yes() in kalshi_lag — so a NO
    position's claim is its complement.
    """
    recs = _read_jsonl(path)
    resolve_kind = f"{fill_kind}_resolve"
    fills: dict[str, dict] = {}
    for r in recs:
        if r.get("kind") == fill_kind:
            k = str(r.get(key) or "")
            if k:
                fills[k] = r

    seen: set[str] = set()
    out: list[Outcome] = []
    for r in recs:
        if r.get("kind") != resolve_kind:
            continue
        if r.get("scratched"):
            continue
        k = str(r.get(key) or "")
        if not k or k in seen:
            continue
        fill = fills.get(k)
        if fill is None:
            continue
        try:
            fair_yes_p = float(fill["fair"])
            p_market = float(r.get("entry_p", fill.get("entry_p")))
        except (KeyError, TypeError, ValueError):
            continue
        if not (0.0 <= fair_yes_p <= 1.0) or not (0.0 < p_market < 1.0):
            continue
        side = str(r.get("side") or fill.get("side") or "yes").lower()
        p_model = fair_yes_p if side == "yes" else 1.0 - fair_yes_p
        seen.add(k)
        out.append(
            Outcome(
                ticker=k,
                coin=str(r.get("coin") or fill.get("coin") or ""),
                side=side,
                p_model=p_model,
                p_market=p_market,
                won=bool(r.get("won")),
                pnl=float(r.get("pnl_after_fee") or 0.0),
                ts=str(r.get("ts") or ""),
            )
        )
    out.sort(key=lambda o: o.ts)
    return out


def brier(pairs: list[tuple[float, float]]) -> float:
    """Mean squared error of probabilistic forecasts. 0 is perfect, 0.25 is a coin flip."""
    if not pairs:
        return 0.0
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def bucketise(outcomes: list[Outcome], n_buckets: int = 5) -> list[Bucket]:
    """Group by predicted probability so claimed vs observed can be compared."""
    if not outcomes or n_buckets < 1:
        return []
    width = 1.0 / n_buckets
    out: list[Bucket] = []
    for i in range(n_buckets):
        lo = i * width
        hi = lo + width
        # last bucket is closed on the right so p == 1.0 lands somewhere
        members = [
            o for o in outcomes
            if (lo <= o.p_model < hi) or (i == n_buckets - 1 and o.p_model == 1.0)
        ]
        if not members:
            continue
        out.append(
            Bucket(
                lo=lo,
                hi=hi,
                n=len(members),
                mean_p=sum(o.p_model for o in members) / len(members),
                realized=sum(o.y for o in members) / len(members),
            )
        )
    return out


def build_report(outcomes: list[Outcome], n_buckets: int = 5) -> Report:
    base = (sum(o.y for o in outcomes) / len(outcomes)) if outcomes else 0.0
    return Report(
        outcomes=outcomes,
        buckets=bucketise(outcomes, n_buckets),
        brier_model=brier([(o.p_model, o.y) for o in outcomes]),
        brier_market=brier([(o.p_market, o.y) for o in outcomes]),
        brier_base=brier([(base, o.y) for o in outcomes]),
        base_rate=base,
    )
