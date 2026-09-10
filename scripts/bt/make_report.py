#!/usr/bin/env python3
"""Assemble the final backtest report from every stage's output."""
from __future__ import annotations
import json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "bt_cache"
OUT = ROOT / "BACKTEST_JAN_SEP_2026.md"
START = 10000.0


def jl(name):
    p = CACHE / name
    return [json.loads(l) for l in p.open()] if p.exists() else []


def stat(tr):
    n = len(tr)
    if not n:
        return dict(n=0, pnl=0.0, win=0.0, be=0.0, fees=0.0)
    return dict(n=n, pnl=sum(t["pnl"] for t in tr),
                win=sum(1 for t in tr if t.get("won")) / n,
                fees=sum(t.get("fee", 0.0) for t in tr),
                be=sum(t.get("ask", 0.0) for t in tr) / n)


def main():
    naive = stat(jl("spotlag_trades.jsonl"))
    sl = stat(jl("spotlag_trades_final.jsonl"))
    kl = stat(jl("kalshilag_trades.jsonl"))
    locks = jl("polylock_stream.jsonl")
    lock_n = len(locks)

    # portfolio results (measured, see scripts/bt/portfolio.py)
    P = dict(on=193.95, off=-1838.93, on_nolock=-27.93, off_nolock=-2060.82,
             on_full=1636.19, off_full=-396.70,
             lock_pnl=221.88, lock_pnl_full=1664.12, sl_pnl=-2058.95, kl_pnl=-1.87,
             avg_deployed=1.45, peak_deployed=120.0,
             sl_trades=2957, kl_trades=444,
             sl_benched_after=21, kl_benched_after=8, skipped=3372,
             dd_on=0.6, dd_off=9.2, dd_off_nolock=23.2)

    L = []
    A = L.append
    A("# Auto trading desk — backtest, 1 Jan → 10 Sep 2026\n")
    A(f"Starting balance **${START:,.0f}**. Paper only — no live order path was used.\n")

    A("## Headline\n")
    A(f"| Configuration | End balance | P&L |")
    A(f"|---|---:|---:|")
    A(f"| **Autopilot ON**, locks sized to real book depth | ${START+P['on']:,.0f} | **{P['on']:+,.0f} ({100*P['on']/START:+.1f}%)** |")
    A(f"| Autopilot OFF, locks sized to real book depth | ${START+P['off']:,.0f} | {P['off']:+,.0f} ({100*P['off']/START:+.1f}%) |")
    A(f"| Autopilot ON, excluding Poly Lock | ${START+P['on_nolock']:,.0f} | {P['on_nolock']:+,.0f} ({100*P['on_nolock']/START:+.1f}%) |")
    A(f"| Autopilot OFF, excluding Poly Lock | ${START+P['off_nolock']:,.0f} | {P['off_nolock']:+,.0f} ({100*P['off_nolock']/START:+.1f}%) |\n")
    A(f"An earlier draft of this report headlined **{P['on_full']:+,.0f} (+16.4%)**. That "
      f"assumed the lock sleeve could fill $15 a side. Measured on live books, the median "
      f"two-sided depth on these markets is **$2**, so that figure was roughly 7x too "
      f"generous. Corrected, the best case is **{P['on']:+,.0f} on $10,000 over 8.5 "
      f"months**.\n")

    A("## The $10,000 is never used\n")
    A(f"Average capital deployed across the whole run: **${P['avg_deployed']:.2f}**. Peak: "
      f"**${P['peak_deployed']:,.0f}**. That is 0.014% of the pot on average.\n")
    A("Two things cause it, and only one of them is a setting:\n")
    A("* the clip is `min(risk_frac * equity, fixed_clip)` = `min($500, $15)`, and the "
      "allocator's own `max_clip` is 15.0 — so the $15 constant binds and your balance "
      "never enters the calculation;")
    A("* **the books cannot absorb more.** Sampled live across BTC/ETH/SOL/XRP/DOGE/BNB "
      "on both 5m and 15m windows:\n")
    A("| Venue | Resting size |")
    A("|---|---:|")
    A("| Polymarket, best ask, one side | median **$9** (p25 $2, p75 $20) |")
    A("| Polymarket, within 1c of the ask | median **$25** |")
    A("| Polymarket, **lock** (both sides at once) | median **$2** (p75 $9, max $25) |")
    A("| Kalshi 15m, liftable at the touch | median **$59** (p75 $148) |\n")
    A("So raising the clip does not raise the profit. It raises the losses, because the "
      "only sleeve that makes money is the one with $2 of depth:\n")
    A("| Clip | Spot-Lag | Poly Lock | Kalshi Lag | Total |")
    A("|---|---:|---:|---:|---:|")
    A("| $15 (as shipped) | -2,059 | +197 | -2 | **-1,863** |")
    A("| $30 | -4,118 | +197 | -4 | **-3,924** |")
    A("| $75 | -10,295 | +197 | -9 | **-10,107** |")
    A("| $150 | -20,590 | +197 | -19 | **-20,411** |\n")
    A("Poly Lock stays pinned at ~$197 in every row: the order book does not deepen "
      "because we would like it to. Everything else scales linearly with the stake, and "
      "everything else loses. **This strategy has no capacity.**\n")
    A("## Per-sleeve, at flat $15 clips\n")
    A("| Sleeve | Period covered | Trades | P&L | Notes |")
    A("|---|---|---:|---:|---|")
    A(f"| Spot-Lag (Polymarket) | Jan 1 – Sep 10 | {P['sl_trades']:,} | {P['sl_pnl']:+,.0f} | fill prices modelled, not observed |")
    A(f"| Poly Lock | Jan 1 – Sep 10 | {lock_n:,} | {P['lock_pnl']:+,.0f} | sized to the $2 median depth |")
    A(f"| Kalshi Lag | Jul 3 – Sep 10 | {P['kl_trades']:,} | {P['kl_pnl']:+,.2f} | **real order book** |")
    A(f"| Kalshi Lock | Jul 3 – Sep 10 | 0 | 0 | structurally impossible |\n")

    A("## What the allocator did\n")
    A(f"With autopilot on, the shipped `Allocator` benched **Spot-Lag after "
      f"{P['sl_benched_after']} trades** and **Kalshi Lag after {P['kl_benched_after']}**, "
      f"both for negative expectancy, and skipped **{P['skipped']:,}** subsequent signals. "
      f"Max drawdown fell from {P['dd_off_nolock']:.1f}% (no autopilot, no lock sleeve) to "
      f"{P['dd_on']:.1f}%.\n")
    A("Note this is a knife-edge: `MIN_SAMPLE` is 8 resolved trades, and a benched "
      "sleeve whose rolling window has gone negative never re-probes (the re-probe path "
      "only applies to sleeves with no track record). The autopilot's contribution here "
      "is a permanent early cut based on a handful of trades — which was the right call "
      "in this sample, but is not a robust edge.\n")

    A("## The number you should not quote\n")
    A(f"Filling from Polymarket's `prices-history` exactly as the shipped replay does gives "
      f"Spot-Lag **{naive['pnl']:+,.0f}** on {naive['n']:,} trades "
      f"({100*naive['win']:.1f}% win rate) — a +57% year. That figure is an artifact. "
      f"Corrected, the same sleeve returns **{P['sl_pnl']:+,.0f}** "
      f"(on {sl['n']:,} trades that survive the corrected gates).\n")

    A("### Why: prices-history is not the order book\n")
    A("Measured live during this run (416 paired book-vs-history observations):\n")
    A("* the last print is a median **31 s old**, while the live spread is only **1c**;")
    A("* on the side a spot move favours — the side this sleeve always buys — the real ask "
      "sits a mean **+13.2c** above the historical print, and **+8.9c** restricted to the "
      "sleeve's own decision moment (45–105 s into a window);")
    A("* history understates that ask **84%** of the time.\n")
    A("The sleeve's claimed edge averaged **+12.3c**. The measurement error is the same size "
      "as the alpha and points in the flattering direction.\n")
    A("A worked example captured live: BTC Up/Down quoted 0.24/0.25 and 0.75/0.76 — asks "
      "summing to **1.01**, mids to exactly **1.000** — while `prices-history` reported "
      "0.345/0.655 for the same instant.\n")

    A("### Three independent confirmations\n")
    A("**1. The profit sits where the data is worst.** 94% of the naive profit came from "
      "22% of trades — the ones with a cheap recorded entry — and those show the price "
      "jumping **+12.5c within 30 s** of entry. A book does not move 12c in half a minute.\n")
    A("**2. The market was priced correctly.** Bucketing naive trades by price paid:\n")
    A("| Ask paid | Trades | Realised win rate | Edge |")
    A("|---:|---:|---:|---:|")
    for lo, hi, in ((0.775, 0.825), (0.825, 0.875), (0.875, 0.925)):
        g = [t for t in jl("spotlag_trades.jsonl") if lo <= t["ask"] < hi]
        if not g:
            continue
        wr = sum(1 for t in g if t["won"]) / len(g)
        ask = sum(t["ask"] for t in g) / len(g)
        A(f"| {ask:.2f} | {len(g):,} | {wr:.3f} | {wr-ask:+.3f} |")
    A("\nWhere 72% of trades sat, realised win rates track the price to within 1–2 points — "
      "less than the taker fee at those prices. That is an efficiently priced market.\n")
    A("**3. The model is overconfident, not sharp.** Where the vol-aware fair claims 0.90 it "
      "wins 0.803; at 0.95 it wins 0.885 — 6–10 points short across every bucket.\n")

    A("## Sleeve detail\n")
    A(f"### Kalshi Lag — the one trustworthy result\n")
    A(f"Kalshi publishes real per-minute bid/ask, so no proxy is needed and "
      f"\"would this have filled?\" has an honest answer. **{kl['n']:,} trades, "
      f"{100*kl['win']:.1f}% win rate against a {100*kl['be']:.1f}% breakeven, "
      f"P&L {kl['pnl']:+,.2f}** after ${kl['fees']:,.2f} in fees.")
    if kl["n"]:
        se = (kl["win"] * (1 - kl["win"]) / kl["n"]) ** 0.5
        A(f"The {100*(kl['win']-kl['be']):+.1f}-point edge has a standard error of "
          f"{100*se:.1f} points — indistinguishable from zero. Priced to the cent, over "
          f"69 days and 444 trades.\n")

    A("### Kalshi Lock — 0 trades, structurally\n")
    A("Kalshi quotes bids only, and the client derives `no_ask = 1 - yes_bid`. A YES+NO pair "
      "therefore costs exactly `1 + spread`, so the lock edge is `-(spread) - fees` and can "
      "never be positive. Across 2,945 quoted minute-bars the best edge observed was "
      "**-0.0012**. This sleeve cannot fire, on any date, at any gate.\n")

    A("### Poly Lock — real but rare, and the weakest number here\n")
    A(f"Over {252} days and 148,557 resolved 15-minute windows across 7 coins:\n")
    A("| Fill rule | Locks | % of windows |")
    A("|---|---:|---:|")
    A("| naive (shipped replay: prints within 15 s) | 8,566 | 5.77% |")
    A("| strict (both prints share a timestamp) | 1,915 | 1.29% |")
    A(f"| + per-side ask premium | {lock_n:,} | 0.93% |\n")
    A("The naive rule is not usable: pairing a fresh print against a stale one across a fast "
      "move manufactures the gap. Same-timestamp pairs sum to exactly 1.000 in 423 of 467 "
      "sampled observations, and only 1 in 467 falls below the 0.97 the 3c gate needs.\n")
    A("Live books confirm genuine locks exist but are rare: the sum of real asks has a median "
      "of **1.010**, yet **2.4%** of snapshots did show a fillable 3c lock. Depth is not "
      f"modelled in the replay itself, so the sleeve is re-sized to the $2 median "
      f"two-sided depth measured live: {P['lock_pnl']:+,.0f}, against "
      f"{P['lock_pnl_full']:+,.0f} if $15 a side were fillable.\n")

    A("## Coverage and limits\n")
    A("* **Kalshi's 15-minute crypto series did not exist before ~3 July 2026.** Its two "
      "sleeves cover 69 of the 253 days. Nothing here is annualised.")
    A("* **Polymarket publishes no historical ask book** — only mid/last prints. Every "
      "Polymarket figure rests on a fill model calibrated against live sampling, not on "
      "observed asks.")
    A("* Kalshi series KXADA15M, KXBCH15M and KXTON15M returned no settled markets; "
      "8 of 11 mapped coins had data.")
    A("* The reference composite uses Coinbase + Bitstamp (the desk's own `MIN_SOURCES` is "
      "2). Kraken serves only ~720 recent 1m bars and Gemini little more, so neither can be "
      "reconstructed historically. Live, the desk would poll up to four venues and compute a "
      "slightly different dispersion.")
    A("* Order-book depth is not modelled for the Polymarket sleeves, and the Kalshi lag "
      "depth gate is skipped. Both omissions can only reduce fills, never increase them.")
    A("* The X/sentiment overlay is live-only and was not applied, per the project's own "
      "rule that backtests are price-only.\n")

    A("## Bottom line\n")
    A(f"Over eight and a half months the best configuration returns "
      f"**{P['on']:+,.0f} on $10,000 — {100*P['on']/START:+.1f}%** — and it gets there by "
      f"trading an average of $1.45 at a time into books holding $2. "
      "The desk does not make money in any way I can verify. "
      "The one sleeve measurable against a real order book is flat to the cent. One lock "
      "sleeve cannot fire at all; the other fires on nine windows in a thousand and its "
      "profit depends on depth I cannot see. The sleeve that appears to print +57% is "
      "filling at quotes that did not exist.\n")
    A("That is the expected result for short-dated binaries on two reasonably efficient "
      "venues, and it matches the project's own stated prior.\n")

    A("---\n")
    A("Reproduce: `scripts/bt/` — fetch stages, then `signals_*`, `replay_*`, "
      "`portfolio.py`. Data caches live under `data/bt_cache/` (gitignored, ~250 MB).")

    OUT.write_text("\n".join(L) + "\n")
    print(f"[written to {OUT}]")


main()
