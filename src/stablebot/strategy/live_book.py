from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from stablebot.config import AppConfig
from stablebot.exchanges.base import USER_AGENT
from stablebot.exchanges.okx import fetch_current_funding, fetch_funding_history
from stablebot.market.book import Quote
from stablebot.market.depeg import deviation_bps
from stablebot.market.funding import FundingPrint, should_enter, trailing_avg
from stablebot.strategy.depeg_fade import acute_entry, consecutive_cheap


@dataclass
class LiveFundingRow:
    inst_id: str
    rate: float | None
    next_funding_time: datetime | None
    trail: list[float]
    trail_avg: float | None
    enter: bool
    error: str | None = None


@dataclass
class LiveDepegRow:
    asset: str
    pair: str
    venue: str
    mid: float | None
    dev_bps: float | None
    last_closes: list[float]
    enter: bool
    note: str = ""


@dataclass
class LiveBookSignals:
    funding: list[LiveFundingRow] = field(default_factory=list)
    depegs: list[LiveDepegRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


VISION_KLINES = "https://data-api.binance.vision/api/v3/klines"


async def _recent_closes(client: httpx.AsyncClient, symbol: str, n: int = 4) -> list[float]:
    resp = await client.get(
        VISION_KLINES,
        params={"symbol": symbol, "interval": "1h", "limit": n},
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    closes = [float(row[4]) for row in data]
    return closes


def _mid_for(quotes: list[Quote], base: str, quote: str) -> tuple[Quote | None, float | None]:
    hits = [q for q in quotes if q.base.upper() == base and q.quote.upper() == quote and q.mid]
    if not hits:
        return None, None
    # prefer binance
    hits.sort(key=lambda q: 0 if q.venue == "binance" else 1)
    return hits[0], hits[0].mid


async def collect_live_signals(cfg: AppConfig, quotes: list[Quote]) -> LiveBookSignals:
    out = LiveBookSignals()
    out.notes.append(
        "Funding live feed is OKX (Binance USD-M fapi is HTTP 451 from this host)."
    )
    insts = list(dict.fromkeys([*cfg.funding.core, *cfg.funding.alt_candidates[: cfg.funding.n_alts]]))
    timeout = httpx.Timeout(20.0)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as http:
        try:
            current = await fetch_current_funding(http, insts)
        except Exception as exc:  # noqa: BLE001
            out.notes.append(f"OKX current funding failed: {exc}")
            current = []
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=3)
        hist: dict[str, list[FundingPrint]] = {}
        for inst in insts:
            try:
                hist[inst] = await fetch_funding_history(http, inst, start, now, warmup_prints=6)
            except Exception as exc:  # noqa: BLE001
                hist[inst] = []
                out.notes.append(f"{inst} history: {exc}")

        by_cur = {r.get("instId"): r for r in current}
        for inst in insts:
            row = by_cur.get(inst) or {}
            err = row.get("error")
            rate = None
            nxt = None
            if not err and row.get("fundingRate") is not None:
                try:
                    rate = float(row["fundingRate"])
                except (TypeError, ValueError):
                    rate = None
                raw_n = row.get("nextFundingTime") or row.get("fundingTime")
                if raw_n:
                    try:
                        nxt = datetime.fromtimestamp(int(raw_n) / 1000.0, tz=timezone.utc)
                    except (TypeError, ValueError):
                        nxt = None
            rates = [p.rate for p in hist.get(inst, [])]
            # if live rate is newer than last hist print, append for the signal
            if rate is not None and (not hist.get(inst) or hist[inst][-1].rate != rate):
                # do not double-count if last hist already is this print
                if not hist.get(inst) or abs(hist[inst][-1].rate - rate) > 1e-12:
                    pass
            trail = rates[-cfg.funding.trail_prints :]
            avg = trailing_avg(rates, cfg.funding.trail_prints)
            enter = should_enter(rates, cfg.funding.min_funding, cfg.funding.trail_prints)
            out.funding.append(
                LiveFundingRow(
                    inst_id=inst,
                    rate=rate,
                    next_funding_time=nxt,
                    trail=trail,
                    trail_avg=avg,
                    enter=enter,
                    error=err,
                )
            )

        # depeg fade candidates from quotes + last hourly closes
        fade = cfg.depeg_fade
        assets = list(fade.candidates)
        for extra in fade.fiat_only_extras:
            assets.append(extra)
        seen: set[str] = set()
        for asset in assets:
            if asset in seen:
                continue
            seen.add(asset)
            quote_pref = ["USDT", "USD"] if asset not in fade.fiat_only_extras else ["USD"]
            qobj = None
            mid = None
            used_quote = None
            for q in quote_pref:
                qobj, mid = _mid_for(quotes, asset, q)
                if mid is not None:
                    used_quote = q
                    break
            closes: list[float] = []
            note = ""
            if asset in fade.fiat_only_extras:
                # only fade vs USD
                symbol = f"{asset}USD"
            else:
                symbol = f"{asset}USDT"
            try:
                closes = await _recent_closes(
                    http,
                    symbol,
                    n=max(26, fade.lookback_hours + fade.consecutive_hours + 1),
                )
            except Exception as exc:  # noqa: BLE001
                note = f"klines: {type(exc).__name__}"
                closes = []
            if closes and closes[-1] <= 0:
                note = "stale/zero close"
                closes = []
            if mid is None and closes:
                mid = closes[-1]
                used_quote = used_quote or ("USD" if asset in fade.fiat_only_extras else "USDT")
            dev = deviation_bps(mid, 1.0) if mid else None
            enter = False
            if closes:
                enter = acute_entry(
                    closes,
                    fade.consecutive_hours,
                    fade.entry_bps,
                    fade.peg_band_bps,
                    fade.lookback_hours,
                )
                if not enter and consecutive_cheap(closes, fade.consecutive_hours, fade.entry_bps):
                    note = (note + " " if note else "") + "chronic/not acute (no near-peg in 24h)"
            elif mid is not None:
                enter = False
                note = (note + " " if note else "") + "live mid only — need 24h history for acute gate"
            if asset in fade.fiat_only_extras and (dev is None or abs(dev) < fade.entry_bps):
                enter = False
            out.depegs.append(
                LiveDepegRow(
                    asset=asset,
                    pair=f"{asset}/{used_quote or '?'}",
                    venue=qobj.venue if qobj else "vision",
                    mid=mid,
                    dev_bps=dev,
                    last_closes=closes[-fade.consecutive_hours :],
                    enter=enter,
                    note=note,
                )
            )
    return out
