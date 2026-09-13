#!/usr/bin/env python3
"""Record what a passive quoter on Kalshi + Polymarket daily-high-temperature
markets would have seen: top-of-book with size, and every trade print.

Paper only. GET/POST-read only. No orders, no keys.

Everything is normalised to YES terms so one fill model serves both venues:
  book  : best bid / ask on YES with resting size at each
  trade : price on YES, size, aggressor ('buy' lifted the ask, 'sell' hit the bid)

Output (append-only, resumable) under data/bt_cache/maker_wx/:
  markets.jsonl  one row per market seen, refreshed with the result once settled
  books.jsonl    one row per market per poll
  trades.jsonl   one row per print, de-duplicated on (venue, id, trade_id)

Run:  .venv/bin/python scripts/bt/maker_wx_poll.py            # foreground, ctrl-c to stop
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "bt_cache" / "maker_wx"
KH = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
PDATA = "https://data-api.polymarket.com"
UA = {"User-Agent": "stablebot/0.1 (research paper-trading; no live orders)"}

# Kalshi series -> Polymarket event-slug city. Ten highest-volume daily highs
# from the 90-day settled pull; Phoenix has no Polymarket twin.
CITIES = {
    "KXHIGHLAX": "los-angeles", "KXHIGHNY": "nyc", "KXHIGHMIA": "miami",
    "KXHIGHCHI": "chicago", "KXHIGHTPHX": None, "KXHIGHTSEA": "seattle",
    "KXHIGHTSFO": "san-francisco", "KXHIGHAUS": "austin", "KXHIGHTDAL": "dallas",
    "KXHIGHTATL": "atlanta",
}
POLL_SEC = 120
DISCOVER_SEC = 1800
RATE_SLEEP = 0.25          # Kalshi caps ~4 req/s; stay under it


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(raw: str) -> float:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()


def append(name: str, rows: list[dict]) -> None:
    if not rows:
        return
    with (OUT / name).open("a") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def getj(c: httpx.Client, url: str, params: dict | None = None, tries: int = 4):
    for a in range(tries):
        try:
            r = c.get(url, params=params)
        except (httpx.HTTPError, OSError):
            time.sleep(1 + a)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 * (a + 1))
            continue
        return None
    return None


def bracket(kind: str, lo, hi) -> tuple[float | None, float | None]:
    """Half-open temperature interval [lo, hi) on the 0.5-degree grid."""
    if kind == "between":
        return (lo - 0.5, hi + 0.5)
    if kind == "greater":
        return (lo + 0.5, None)
    return (None, hi - 0.5)


def poly_bracket(question: str) -> tuple[float | None, float | None] | None:
    m = re.search(r"between (\d+)-(\d+)", question)
    if m:
        return (int(m.group(1)) - 0.5, int(m.group(2)) + 0.5)
    m = re.search(r"(\d+)°F or below", question)
    if m:
        return (None, int(m.group(1)) + 0.5)
    m = re.search(r"(\d+)°F or higher", question)
    if m:
        return (int(m.group(1)) - 0.5, None)
    return None


class Poller:
    def __init__(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        self.c = httpx.Client(headers=UA, timeout=30)
        self.markets: dict[tuple[str, str], dict] = {}      # (venue, id) -> meta
        self.seen_trades: set[tuple[str, str, str]] = set()
        self.last_discover = 0.0
        self._load()

    def _load(self) -> None:
        p = OUT / "markets.jsonl"
        if p.exists():
            for line in p.open():
                m = json.loads(line)
                self.markets[(m["venue"], m["id"])] = m
        p = OUT / "trades.jsonl"
        if p.exists():
            for line in p.open():
                t = json.loads(line)
                self.seen_trades.add((t["venue"], t["id"], t["trade_id"]))

    # ---- discovery -----------------------------------------------------
    def discover(self) -> None:
        new: list[dict] = []
        today = utcnow().date()
        dates = [today + timedelta(days=i) for i in range(-2, 3)]
        for series, city in CITIES.items():
            d = getj(self.c, f"{KH}/markets", {"series_ticker": series, "status": "open", "limit": 100})
            time.sleep(RATE_SLEEP)
            for m in (d or {}).get("markets", []):
                key = ("kalshi", m["ticker"])
                if key in self.markets:
                    continue
                lo, hi = bracket(m["strike_type"], m.get("floor_strike"), m.get("cap_strike"))
                row = {"venue": "kalshi", "id": m["ticker"], "series": series, "city": city or series,
                       "date": m["ticker"].split("-")[1], "lo": lo, "hi": hi,
                       "close_ts": parse_ts(m["close_time"]), "result": None}
                self.markets[key] = row
                new.append(row)
            if not city:
                continue
            for dt in dates:
                slug = f"highest-temperature-in-{city}-on-{dt.strftime('%B').lower()}-{dt.day}-{dt.year}"
                ev = getj(self.c, f"{GAMMA}/events", {"slug": slug})
                if not ev:
                    continue
                for m in ev[0].get("markets", []):
                    toks = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                    key = ("poly", toks[0])
                    if key in self.markets:
                        continue
                    br = poly_bracket(m["question"])
                    if not br:
                        continue
                    # Polymarket's endDate is noon UTC on the market day, before the high
                    # has happened, and trading continues past it. The quoting window is
                    # defined by the Kalshi twin's close (midnight local) instead.
                    kdate = dt.strftime("%y%b%d").upper()
                    twin = next((k for k in self.markets.values()
                                 if k["venue"] == "kalshi" and k["series"] == series and k["date"] == kdate), None)
                    if twin is None:
                        continue
                    row = {"venue": "poly", "id": toks[0], "no_token": toks[1], "condition": m["conditionId"],
                           "series": series, "city": city, "date": kdate,
                           "lo": br[0], "hi": br[1], "close_ts": twin["close_ts"], "result": None}
                    self.markets[key] = row
                    new.append(row)
        append("markets.jsonl", new)
        self.settle()
        self.last_discover = time.time()

    def settle(self) -> None:
        """Fill in results for markets past close. Rewrites markets.jsonl in place."""
        changed = False
        for series in CITIES:
            pending = [m for m in self.markets.values()
                       if m["venue"] == "kalshi" and m["series"] == series and m["result"] is None
                       and m["close_ts"] < time.time()]
            if not pending:
                continue
            d = getj(self.c, f"{KH}/markets", {"series_ticker": series, "status": "settled", "limit": 200,
                                                "min_close_ts": int(time.time()) - 5 * 86400})
            time.sleep(RATE_SLEEP)
            for m in (d or {}).get("markets", []):
                key = ("kalshi", m["ticker"])
                if key in self.markets and m.get("result") in ("yes", "no"):
                    self.markets[key]["result"] = m["result"]
                    changed = True
        pending_p = [m for m in self.markets.values()
                     if m["venue"] == "poly" and m["result"] is None and m["close_ts"] < time.time() - 6 * 3600]
        for cond in {m["condition"] for m in pending_p}:
            d = getj(self.c, f"{GAMMA}/markets", {"condition_ids": cond})
            if not d:
                continue
            m = d[0]
            op = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m["outcomePrices"]
            if m.get("closed") and op and float(op[0]) in (0.0, 1.0):
                toks = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                key = ("poly", toks[0])
                if key in self.markets:
                    self.markets[key]["result"] = "yes" if float(op[0]) == 1.0 else "no"
                    changed = True
        if changed:
            with (OUT / "markets.jsonl").open("w") as f:
                for m in self.markets.values():
                    f.write(json.dumps(m, separators=(",", ":")) + "\n")

    # ---- per-poll ------------------------------------------------------
    def live(self) -> list[dict]:
        now = time.time()
        return [m for m in self.markets.values() if m["result"] is None and m["close_ts"] > now - 3600]

    def poll_books(self, live: list[dict]) -> None:
        ts = time.time()
        rows: list[dict] = []
        for series in {m["series"] for m in live if m["venue"] == "kalshi"}:
            d = getj(self.c, f"{KH}/markets", {"series_ticker": series, "status": "open", "limit": 100})
            time.sleep(RATE_SLEEP)
            for m in (d or {}).get("markets", []):
                if ("kalshi", m["ticker"]) not in self.markets:
                    continue
                rows.append({"ts": ts, "venue": "kalshi", "id": m["ticker"],
                             "bid": float(m["yes_bid_dollars"]), "bid_sz": float(m["yes_bid_size_fp"]),
                             "ask": float(m["yes_ask_dollars"]), "ask_sz": float(m["yes_ask_size_fp"])})
        toks = [m["id"] for m in live if m["venue"] == "poly"]
        for i in range(0, len(toks), 50):
            chunk = toks[i:i + 50]
            try:
                r = self.c.post(f"{CLOB}/books", json=[{"token_id": t} for t in chunk])
                books = r.json() if r.status_code == 200 else []
            except (httpx.HTTPError, OSError, ValueError):
                books = []
            for b in books:
                bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
                asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
                bb = max(bids) if bids else (0.0, 0.0)
                ba = min(asks) if asks else (1.0, 0.0)
                rows.append({"ts": ts, "venue": "poly", "id": b["asset_id"],
                             "bid": bb[0], "bid_sz": bb[1], "ask": ba[0], "ask_sz": ba[1]})
        append("books.jsonl", rows)

    def poll_trades(self, live: list[dict], since: float) -> None:
        rows: list[dict] = []
        for m in live:
            if m["venue"] == "kalshi":
                d = getj(self.c, f"{KH}/markets/trades",
                         {"ticker": m["id"], "limit": 200, "min_ts": int(since) - 5})
                time.sleep(RATE_SLEEP)
                for t in (d or {}).get("trades", []):
                    key = ("kalshi", m["id"], t["trade_id"])
                    if key in self.seen_trades:
                        continue
                    self.seen_trades.add(key)
                    rows.append({"ts": parse_ts(t["created_time"]), "venue": "kalshi", "id": m["id"],
                                 "trade_id": t["trade_id"], "price": float(t["yes_price_dollars"]),
                                 "size": float(t["count_fp"]),
                                 "aggressor": "buy" if t["taker_side"] == "yes" else "sell"})
        # Polymarket: one call per city-day, all brackets' condition ids comma-joined.
        by_cond = {m["condition"]: m for m in live if m["venue"] == "poly"}
        groups: dict[tuple[str, str], list[str]] = {}
        for m in by_cond.values():
            groups.setdefault((m["city"], m["date"]), []).append(m["condition"])
        for conds in groups.values():
            d = getj(self.c, f"{PDATA}/trades", {"market": ",".join(conds), "limit": 500})
            if d and len(d) >= 500 and min(float(t["timestamp"]) for t in d) > since:
                print("  warn: poly trade page full within one poll; prints may be missing", flush=True)
            for t in d or []:
                m = by_cond.get(t.get("conditionId"))
                if m is None or float(t["timestamp"]) < since - 5:
                    continue
                tid = f"{t.get('transactionHash', '')}:{t.get('asset')}:{t.get('timestamp')}:{t.get('size')}"
                key = ("poly", m["id"], tid)
                if key in self.seen_trades:
                    continue
                self.seen_trades.add(key)
                # Normalise NO-token prints into YES terms: buying NO at q == selling YES at 1-q.
                is_yes = t["asset"] == m["id"]
                px = float(t["price"]) if is_yes else 1.0 - float(t["price"])
                buy = (t["side"] == "BUY") == is_yes
                rows.append({"ts": float(t["timestamp"]), "venue": "poly", "id": m["id"], "trade_id": tid,
                             "price": px, "size": float(t["size"]), "aggressor": "buy" if buy else "sell"})
        append("trades.jsonl", rows)

    def run(self) -> None:
        last = time.time() - POLL_SEC
        while True:
            t0 = time.time()
            if t0 - self.last_discover > DISCOVER_SEC:
                self.discover()
            live = self.live()
            self.poll_books(live)
            self.poll_trades(live, since=last)
            last = t0
            print(f"{utcnow().isoformat(timespec='seconds')} live={len(live)} "
                  f"trades_seen={len(self.seen_trades)} took={time.time() - t0:.0f}s", flush=True)
            time.sleep(max(0.0, POLL_SEC - (time.time() - t0)))


if __name__ == "__main__":
    try:
        Poller().run()
    except KeyboardInterrupt:
        sys.exit(0)
