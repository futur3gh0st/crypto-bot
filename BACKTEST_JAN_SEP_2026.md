# Auto trading desk — backtest, 1 Jan → 10 Sep 2026

Starting balance **$10,000**. Paper only — no live order path was used.

## Headline

| Configuration | End balance | P&L |
|---|---:|---:|
| **Autopilot ON** (as the desk ships) | $11,636 | **+1,636 (+16.4%)** |
| Autopilot OFF (flat $15 clips) | $9,603 | -397 (-4.0%) |
| Autopilot ON, excluding Poly Lock | $9,972 | -28 (-0.3%) |
| Autopilot OFF, excluding Poly Lock | $7,939 | -2,061 (-20.6%) |

**Read the second and third rows before the first.** The +16.4% is real only if Poly Lock's fills are real, and those are the least verifiable numbers here (see *Poly Lock* below). Strip that one sleeve out and the desk is flat: **-28**.

## Per-sleeve, at flat $15 clips

| Sleeve | Period covered | Trades | P&L | Notes |
|---|---|---:|---:|---|
| Spot-Lag (Polymarket) | Jan 1 – Sep 10 | 2,957 | -2,059 | fill prices modelled, not observed |
| Poly Lock | Jan 1 – Sep 10 | 1,386 | +1,664 | upper bound; depth not modelled |
| Kalshi Lag | Jul 3 – Sep 10 | 444 | -1.87 | **real order book** |
| Kalshi Lock | Jul 3 – Sep 10 | 0 | 0 | structurally impossible |

## What the allocator did

With autopilot on, the shipped `Allocator` benched **Spot-Lag after 21 trades** and **Kalshi Lag after 8**, both for negative expectancy, and skipped **3,372** subsequent signals. Max drawdown fell from 23.2% (no autopilot, no lock sleeve) to 0.6%.

Note this is a knife-edge: `MIN_SAMPLE` is 8 resolved trades, and a benched sleeve whose rolling window has gone negative never re-probes (the re-probe path only applies to sleeves with no track record). The autopilot's contribution here is a permanent early cut based on a handful of trades — which was the right call in this sample, but is not a robust edge.

## The number you should not quote

Filling from Polymarket's `prices-history` exactly as the shipped replay does gives Spot-Lag **+5,702** on 5,116 trades (84.2% win rate) — a +57% year. That figure is an artifact. Corrected, the same sleeve returns **-2,059** (on 2,957 trades that survive the corrected gates).

### Why: prices-history is not the order book

Measured live during this run (416 paired book-vs-history observations):

* the last print is a median **31 s old**, while the live spread is only **1c**;
* on the side a spot move favours — the side this sleeve always buys — the real ask sits a mean **+13.2c** above the historical print, and **+8.9c** restricted to the sleeve's own decision moment (45–105 s into a window);
* history understates that ask **84%** of the time.

The sleeve's claimed edge averaged **+12.3c**. The measurement error is the same size as the alpha and points in the flattering direction.

A worked example captured live: BTC Up/Down quoted 0.24/0.25 and 0.75/0.76 — asks summing to **1.01**, mids to exactly **1.000** — while `prices-history` reported 0.345/0.655 for the same instant.

### Three independent confirmations

**1. The profit sits where the data is worst.** 94% of the naive profit came from 22% of trades — the ones with a cheap recorded entry — and those show the price jumping **+12.5c within 30 s** of entry. A book does not move 12c in half a minute.

**2. The market was priced correctly.** Bucketing naive trades by price paid:

| Ask paid | Trades | Realised win rate | Edge |
|---:|---:|---:|---:|
| 0.80 | 1,212 | 0.814 | +0.012 |
| 0.85 | 1,431 | 0.862 | +0.015 |
| 0.90 | 1,059 | 0.909 | +0.013 |

Where 72% of trades sat, realised win rates track the price to within 1–2 points — less than the taker fee at those prices. That is an efficiently priced market.

**3. The model is overconfident, not sharp.** Where the vol-aware fair claims 0.90 it wins 0.803; at 0.95 it wins 0.885 — 6–10 points short across every bucket.

## Sleeve detail

### Kalshi Lag — the one trustworthy result

Kalshi publishes real per-minute bid/ask, so no proxy is needed and "would this have filled?" has an honest answer. **444 trades, 84.9% win rate against a 84.0% breakeven, P&L -1.93** after $74.40 in fees.
The +0.9-point edge has a standard error of 1.7 points — indistinguishable from zero. Priced to the cent, over 69 days and 444 trades.

### Kalshi Lock — 0 trades, structurally

Kalshi quotes bids only, and the client derives `no_ask = 1 - yes_bid`. A YES+NO pair therefore costs exactly `1 + spread`, so the lock edge is `-(spread) - fees` and can never be positive. Across 2,945 quoted minute-bars the best edge observed was **-0.0012**. This sleeve cannot fire, on any date, at any gate.

### Poly Lock — real but rare, and the weakest number here

Over 252 days and 148,557 resolved 15-minute windows across 7 coins:

| Fill rule | Locks | % of windows |
|---|---:|---:|
| naive (shipped replay: prints within 15 s) | 8,566 | 5.77% |
| strict (both prints share a timestamp) | 1,915 | 1.29% |
| + per-side ask premium | 1,386 | 0.93% |

The naive rule is not usable: pairing a fresh print against a stale one across a fast move manufactures the gap. Same-timestamp pairs sum to exactly 1.000 in 423 of 467 sampled observations, and only 1 in 467 falls below the 0.97 the 3c gate needs.

Live books confirm genuine locks exist but are rare: the sum of real asks has a median of **1.010**, yet **2.4%** of snapshots did show a fillable 3c lock. Depth is not modelled, so treat +1,664 as an upper bound.

## Coverage and limits

* **Kalshi's 15-minute crypto series did not exist before ~3 July 2026.** Its two sleeves cover 69 of the 253 days. Nothing here is annualised.
* **Polymarket publishes no historical ask book** — only mid/last prints. Every Polymarket figure rests on a fill model calibrated against live sampling, not on observed asks.
* Kalshi series KXADA15M, KXBCH15M and KXTON15M returned no settled markets; 8 of 11 mapped coins had data.
* The reference composite uses Coinbase + Bitstamp (the desk's own `MIN_SOURCES` is 2). Kraken serves only ~720 recent 1m bars and Gemini little more, so neither can be reconstructed historically. Live, the desk would poll up to four venues and compute a slightly different dispersion.
* Order-book depth is not modelled for the Polymarket sleeves, and the Kalshi lag depth gate is skipped. Both omissions can only reduce fills, never increase them.
* The X/sentiment overlay is live-only and was not applied, per the project's own rule that backtests are price-only.

## Bottom line

Over eight and a half months the desk does not make money in any way I can verify. The one sleeve measurable against a real order book is flat to the cent. One lock sleeve cannot fire at all; the other fires on nine windows in a thousand and its profit depends on depth I cannot see. The sleeve that appears to print +57% is filling at quotes that did not exist.

That is the expected result for short-dated binaries on two reasonably efficient venues, and it matches the project's own stated prior.

---

Reproduce: `scripts/bt/` — fetch stages, then `signals_*`, `replay_*`, `portfolio.py`. Data caches live under `data/bt_cache/` (gitignored, ~250 MB).
