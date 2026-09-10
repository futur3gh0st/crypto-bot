from __future__ import annotations

from datetime import datetime, timezone

import httpx

from stablebot.exchanges.base import USER_AGENT
from stablebot.market.funding import FundingPrint

OKX_BASES = (
    "https://www.okx.com",
    "https://aws.okx.com",
)


async def _get_json(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
    last_err: Exception | None = None
    for base in OKX_BASES:
        try:
            resp = await client.get(f"{base}{path}", params=params)
            resp.raise_for_status()
            payload = resp.json()
            if str(payload.get("code", "0")) != "0":
                raise RuntimeError(f"okx {path}: {payload.get('msg') or payload}")
            return payload
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise last_err or RuntimeError(f"okx {path} failed")


def _from_ms(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)


async def fetch_current_funding(
    client: httpx.AsyncClient, inst_ids: list[str]
) -> list[dict]:
    out: list[dict] = []
    for inst_id in inst_ids:
        try:
            payload = await _get_json(
                client, "/api/v5/public/funding-rate", {"instId": inst_id}
            )
        except Exception as exc:  # noqa: BLE001
            out.append({"instId": inst_id, "error": f"{type(exc).__name__}: {exc}"})
            continue
        rows = payload.get("data") or []
        if not rows:
            out.append({"instId": inst_id, "error": "empty"})
            continue
        row = dict(rows[0])
        row["error"] = None
        out.append(row)
    return out


async def fetch_funding_history(
    client: httpx.AsyncClient,
    inst_id: str,
    start: datetime,
    end: datetime,
    warmup_prints: int = 6,
) -> list[FundingPrint]:
    """Paginate OKX public funding-rate-history (max 100 / call)."""
    need_from = start
    # pull extra so the trailing-3 window is defined at `start`
    out: list[FundingPrint] = []
    before: str | None = None
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    for _ in range(8):
        params: dict[str, str] = {"instId": inst_id, "limit": "100"}
        if before:
            params["before"] = before
        payload = await _get_json(client, "/api/v5/public/funding-rate-history", params)
        rows = payload.get("data") or []
        if not rows:
            break
        for row in rows:
            ts = _from_ms(row["fundingTime"])
            raw = row.get("realizedRate") or row.get("fundingRate")
            if raw is None:
                continue
            out.append(
                FundingPrint(
                    inst_id=inst_id,
                    ts=ts,
                    rate=float(raw),
                    venue="okx",
                )
            )
        oldest = min(int(r["fundingTime"]) for r in rows)
        if oldest <= start_ms or len(rows) < 100:
            break
        before = str(oldest)
    # unique + sort
    seen: set[tuple[str, datetime]] = set()
    uniq: list[FundingPrint] = []
    for p in sorted(out, key=lambda x: x.ts):
        key = (p.inst_id, p.ts)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    # keep warmup prints before start plus everything through end
    pre = [p for p in uniq if p.ts < need_from]
    mid = [p for p in uniq if need_from <= p.ts <= end]
    kept = pre[-warmup_prints:] + mid
    # drop anything after end
    kept = [p for p in kept if p.ts <= datetime.fromtimestamp(end_ms / 1000.0, tz=timezone.utc)]
    return kept


def default_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(20.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )
