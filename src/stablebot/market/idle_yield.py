from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from stablebot.exchanges.base import USER_AGENT

LLAMA_POOLS = "https://yields.llama.fi/pools"
AAVE_DUMP = "https://th3nolo.github.io/aave-v3-data/aave_v3_data.json"


@dataclass(frozen=True)
class IdleYield:
    """Live public USDC supply APY. apy is a decimal (0.033 = 3.3%)."""

    apy: float
    source: str
    symbol: str = "USDC"
    asof: str | None = None

    @property
    def apy_pct(self) -> float:
        return self.apy * 100.0

    def label(self) -> str:
        pct = f"{self.apy_pct:.4f}%"
        when = f" asof {self.asof}" if self.asof else ""
        return f"idle_yield {self.symbol} {pct} from {self.source}{when}"


def idle_cash_pnl(idle: float, apy: float, hours: float) -> float:
    """Simple interest on unallocated cash over `hours` at annual `apy` (decimal)."""
    if idle <= 0 or apy <= 0 or hours <= 0:
        return 0.0
    return float(idle) * float(apy) * float(hours) / (365.0 * 24.0)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _from_llama(payload: object) -> IdleYield | None:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    best = None
    best_tvl = -1.0
    for p in rows:
        if not isinstance(p, dict):
            continue
        if (p.get("project") or "").lower() != "aave-v3":
            continue
        if (p.get("chain") or "") != "Ethereum":
            continue
        if (p.get("symbol") or "").upper() != "USDC":
            continue
        tvl = float(p.get("tvlUsd") or 0.0)
        if tvl > best_tvl:
            best = p
            best_tvl = tvl
    if best is None:
        return None
    raw = best.get("apyBase")
    if raw is None:
        raw = best.get("apy")
    try:
        pct = float(raw)
    except (TypeError, ValueError):
        return None
    if pct <= 0 or pct > 50:
        return None
    pool = best.get("pool") or "?"
    return IdleYield(
        apy=pct / 100.0,
        source=(
            "DefiLlama yields.llama.fi "
            f"Aave v3 Ethereum USDC (pool {pool}, apyBase)"
        ),
        symbol="USDC",
        asof=_now_iso(),
    )


def _from_aave_dump(payload: object) -> IdleYield | None:
    if not isinstance(payload, dict):
        return None
    eth = (payload.get("networks") or {}).get("ethereum") or []
    if not isinstance(eth, list):
        return None
    for row in eth:
        if not isinstance(row, dict) or row.get("symbol") != "USDC":
            continue
        try:
            rate = float(row.get("current_liquidity_rate"))
        except (TypeError, ValueError):
            return None
        if rate <= 0 or rate > 0.5:
            return None
        fresh = ((payload.get("metadata") or {}).get("data_freshness") or {}).get("last_update")
        return IdleYield(
            apy=rate,
            source="th3nolo.github.io/aave-v3-data Aave v3 Ethereum USDC current_liquidity_rate",
            symbol="USDC",
            asof=str(fresh) if fresh else _now_iso(),
        )
    return None


async def fetch_idle_yield(client: httpx.AsyncClient | None = None) -> IdleYield | None:
    """Fetch a live public USDC supply APY. Returns None if nothing usable."""
    own = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(20.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )
    try:
        try:
            resp = await http.get(LLAMA_POOLS)
            resp.raise_for_status()
            hit = _from_llama(resp.json())
            if hit is not None:
                return hit
        except Exception:
            pass
        try:
            resp = await http.get(AAVE_DUMP)
            resp.raise_for_status()
            return _from_aave_dump(resp.json())
        except Exception:
            return None
    finally:
        if own:
            await http.aclose()
