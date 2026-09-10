from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from stablebot.config import AppConfig
from stablebot.exchanges.okx import default_http, fetch_funding_history
from stablebot.market.funding import FundingPrint


@dataclass
class FundingBook:
    prints: list[FundingPrint] = field(default_factory=list)
    universe: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ranking: list[tuple[str, float, int]] = field(default_factory=list)

    def by_inst(self) -> dict[str, list[FundingPrint]]:
        out: dict[str, list[FundingPrint]] = {}
        for p in self.prints:
            out.setdefault(p.inst_id, []).append(p)
        for k in out:
            out[k].sort(key=lambda x: x.ts)
        return out


def _abs_mean(prints: list[FundingPrint], start: datetime, end: datetime) -> tuple[float, int]:
    rows = [p for p in prints if start <= p.ts <= end]
    if not rows:
        return 0.0, 0
    return sum(abs(p.rate) for p in rows) / len(rows), len(rows)


def select_universe(
    by_inst: dict[str, list[FundingPrint]],
    cfg: AppConfig,
    start: datetime,
    end: datetime,
) -> tuple[list[str], list[tuple[str, float, int]]]:
    """Core names always; add the fattest alts by |mean funding| in [start, end]."""
    ranking: list[tuple[str, float, int]] = []
    for inst, rows in by_inst.items():
        mu, n = _abs_mean(rows, start, end)
        ranking.append((inst, mu, n))
    ranking.sort(key=lambda x: x[1], reverse=True)
    core = [i for i in cfg.funding.core if i in by_inst and by_inst[i]]
    alts = [i for i in cfg.funding.alt_candidates if i in by_inst and by_inst[i]]
    alts_ranked = [inst for inst, mu, n in ranking if inst in alts and n >= 6]
    picked_alts = alts_ranked[: max(0, cfg.funding.n_alts)]
    universe: list[str] = []
    for inst in [*core, *picked_alts]:
        if inst not in universe:
            universe.append(inst)
    return universe, ranking


async def fetch_funding_book(
    cfg: AppConfig,
    start: datetime,
    end: datetime,
    client: httpx.AsyncClient | None = None,
) -> FundingBook:
    book = FundingBook()
    book.notes.append(
        "Funding source: OKX public /api/v5/public/funding-rate-history "
        "(Binance fapi.binance.com returns HTTP 451 from this host; "
        "data-api.binance.vision has no futures mirror)."
    )
    book.notes.append(
        "Basis PnL approximated as 0 — funding minus round-trip spot+perp fees + half-spread only."
    )
    warmup_start = start - timedelta(days=3)
    candidates = list(dict.fromkeys([*cfg.funding.core, *cfg.funding.alt_candidates]))
    own = client is None
    http = client or default_http()
    try:
        for inst in candidates:
            try:
                rows = await fetch_funding_history(http, inst, warmup_start, end)
            except Exception as exc:  # noqa: BLE001
                book.skipped.append(f"{inst}: {type(exc).__name__}: {exc}")
                continue
            if not rows:
                book.skipped.append(f"{inst}: no funding history")
                continue
            book.prints.extend(rows)
            book.notes.append(f"{inst}: {len(rows)} funding prints")
    finally:
        if own:
            await http.aclose()
    by = book.by_inst()
    # rank on the requested window (disclosed in-sample)
    universe, ranking = select_universe(by, cfg, start, end)
    book.universe = universe
    book.ranking = ranking
    if universe:
        book.notes.append("Universe: " + ", ".join(universe))
        fat = ", ".join(f"{i} |mean|={mu:.6f} n={n}" for i, mu, n in ranking[:8])
        book.notes.append(f"In-window |mean| rank (selection, disclosed): {fat}")
    else:
        book.notes.append("Universe empty — funding sleeve idle.")
    return book
