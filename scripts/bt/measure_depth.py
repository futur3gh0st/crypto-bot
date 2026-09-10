#!/usr/bin/env python3
"""How much size can these books actually absorb at the touch?

The desk stakes $15 a trade. Whether that is timidity or the market's own limit
is an empirical question about depth, so measure it on live books: dollars
resting at the best ask, and cumulative dollars within 1c and 3c of it.
"""
from __future__ import annotations
import asyncio, json, statistics, time
import httpx

H = {"User-Agent": "stablebot/0.1 (research paper-trading; no live orders)",
     "Accept": "application/json"}
COINS = ["btc", "eth", "sol", "xrp", "doge", "bnb"]
KSERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXBNB15M"]
KH = "https://api.elections.kalshi.com/trade-api/v2"


async def jget(c, url, params=None):
    for _ in range(3):
        try:
            r = await c.get(url, params=params)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return None


async def poly(c, out):
    now = time.time()
    for minutes in (5, 15):
        ws = int(now // (minutes * 60)) * (minutes * 60)
        for coin in COINS:
            ev = await jget(c, "https://gamma-api.polymarket.com/events",
                            {"slug": f"{coin}-updown-{minutes}m-{ws}"})
            mk = (ev[0].get("markets") or [None])[0] if ev else None
            if not mk:
                continue
            ids = json.loads(mk.get("clobTokenIds") or "[]")
            outs = json.loads(mk.get("outcomes") or "[]")
            if len(ids) != 2:
                continue
            pair_touch = 0.0
            ok = True
            for name, tok in zip(outs, ids):
                bk = await jget(c, "https://clob.polymarket.com/book", {"token_id": tok})
                asks = sorted(((float(x["price"]), float(x["size"]))
                               for x in (bk or {}).get("asks", [])), key=lambda z: z[0])
                if not asks:
                    ok = False; break
                best = asks[0][0]
                touch = asks[0][0] * asks[0][1]
                w1 = sum(p * s for p, s in asks if p <= best + 0.01)
                w3 = sum(p * s for p, s in asks if p <= best + 0.03)
                out["poly"].append({"coin": coin, "min": minutes, "side": name,
                                    "touch$": touch, "w1c$": w1, "w3c$": w3})
                pair_touch = touch if pair_touch == 0 else min(pair_touch, touch)
            if ok and pair_touch:
                out["poly_pair"].append(pair_touch)


async def kalshi(c, out):
    for ser in KSERIES:
        d = await jget(c, f"{KH}/markets", {"series_ticker": ser, "status": "open",
                                            "limit": 4})
        for m in (d or {}).get("markets", []):
            bk = await jget(c, f"{KH}/markets/{m['ticker']}/orderbook")
            fp = ((bk or {}).get("orderbook_fp") or {})
            def best(levels):
                vals = []
                for lv in (levels or []):
                    try:
                        vals.append((float(lv[0]), float(lv[1])))
                    except Exception:
                        pass
                return max(vals, key=lambda z: z[0]) if vals else None
            yb, nb = best(fp.get("yes_dollars")), best(fp.get("no_dollars"))
            # lifting the YES ask means taking the best NO bid: its size is dollars
            if nb:
                out["kalshi"].append({"series": ser, "side": "yes_ask", "dollars": nb[1]})
            if yb:
                out["kalshi"].append({"series": ser, "side": "no_ask", "dollars": yb[1]})


def show(name, vals, unit="$"):
    if not vals:
        print(f"  {name}: no data"); return
    vals = sorted(vals)
    q = lambda f: vals[int(f * (len(vals) - 1))]
    print(f"  {name}: n={len(vals)}  median={unit}{q(.5):,.0f}  "
          f"p25={unit}{q(.25):,.0f}  p75={unit}{q(.75):,.0f}  max={unit}{vals[-1]:,.0f}")


async def main():
    out = {"poly": [], "poly_pair": [], "kalshi": []}
    async with httpx.AsyncClient(headers=H, timeout=25,
                                 limits=httpx.Limits(max_connections=16)) as c:
        await asyncio.gather(poly(c, out), kalshi(c, out))
    print("POLYMARKET Up/Down — dollars resting on the ask side")
    show("at the best ask     ", [x["touch$"] for x in out["poly"]])
    show("within 1c of it     ", [x["w1c$"] for x in out["poly"]])
    show("within 3c of it     ", [x["w3c$"] for x in out["poly"]])
    show("lock: min of the two", out["poly_pair"])
    print("\nKALSHI 15m — dollars resting behind an ask (the opposite bid)")
    show("liftable at the touch", [x["dollars"] for x in out["kalshi"]])
    print("\ndesk clip = $15/trade")

asyncio.run(main())
