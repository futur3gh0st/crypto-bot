# The trading desk

A single-screen TUI that runs the paper sleeves for you. You do not pick
trades: it allocates capital across strategies from their own realised
results, sizes each entry, benches whatever stops paying, and stops itself
before a bad day becomes a bad week.

```bash
cd crypto-bot
export STABLEBOT_ROOT="$(pwd)"
.venv/bin/python -m stablebot desk
```

That opens the menu. Do nothing for 20 seconds and it starts the auto desk on
its own. To skip the menu entirely — for `tmux`, `nohup`, or a launch agent:

```bash
.venv/bin/python -m stablebot desk --auto
.venv/bin/python -m stablebot desk --auto --sleeves locks     # only the lock venues
```

Everything below is **paper**. Nothing in the desk can place a live order; the
Polymarket live path still lives behind its own gates in `poly-run --live`.

---

## 1. What was wrong, and what changed

You ran the spot-lag sleeve and it sat there printing `no_signal` forever.
That was not bad luck. Two things were wrong.

### The entry gates could not both be satisfied

The old rule composed a crude fair value with a catch-up entry model:

```
fair  = 0.50 + 25 * ret
entry = 0.50 + catchup * (fair - 0.50) + slip
edge  = fair - entry = (1 - catchup) * 25 * ret - slip
```

With the shipped defaults (`catchup=0.70`, `slip=0.02`, `min_edge=0.04`) that
needs `edge >= 0.04`, which solves to a **0.80% move in a single one-minute
bar**. The signal threshold only asked for 0.30%, so the edge gate was the
real constraint and it was 2.7x tighter.

Measured over ~3,000 recent 1m bars on each of six majors:

| symbol | sigma (1m) | bars >= 0.30% | bars >= 0.80% |
|---|---|---|---|
| BTCUSDT | 0.038% | 0.000% | **0.000%** |
| ETHUSDT | 0.052% | 0.000% | **0.000%** |
| SOLUSDT | 0.065% | 0.200% | **0.000%** |
| XRPUSDT | 0.076% | 0.467% | **0.000%** |
| DOGEUSDT | 0.091% | 0.800% | **0.000%** |
| BNBUSDT | 0.053% | 0.067% | **0.000%** |

Zero qualifying bars anywhere. The sleeve was not quiet, it was arithmetically
incapable of trading. A 0.80% BTC move is a ~21 sigma event.

### One threshold cannot mean the same thing on two coins

A flat 0.30% bar is 7.8 sigma on BTC and 3.3 sigma on DOGE. And the fair value
ignored volatility *and* time-to-expiry entirely: `scale=25` implicitly assumes
about **1.6% of movement per window**, roughly twenty times what BTC actually
does in five minutes. So the model read genuinely decisive moves as coin flips.

### The replacement

A binary that pays if price closes above the window open, with `tau` minutes
left and per-minute volatility `sigma`, is a normal CDF:

```
z       = log(spot / open) / (sigma * sqrt(tau))
fair_up = Phi(z)
```

`sigma` is estimated per symbol from an EWMA of realised 1m returns, so the
gates self-calibrate to the current regime. The signal threshold moves from a
fixed percent to `|z| >= z_entry` sigma, which fires at a comparable rate on
every coin.

### Does it actually predict better?

`scripts/vol_signal_study.py` walks real 1m bars, rebuilds the same windows the
bot trades, and scores both models against what actually happened. Volatility
is estimated only from bars strictly before each observation, so there is no
lookahead.

```bash
.venv/bin/python scripts/vol_signal_study.py --window 5 --pulls 4
```

Pooled over six majors, ~19,000 observations:

| | Brier | log loss |
|---|---|---|
| always guess 0.50 | 0.2500 | 0.6931 |
| crude linear fair | 0.2382 | 0.6694 |
| **vol-aware fair** | **0.1661** | **0.4997** |

Lower is better. The crude model was barely distinguishable from having no
model at all. Calibration is the sharper picture — predicted probability
versus how often it actually happened:

| bucket | crude says | truth | vol-aware says | truth |
|---|---|---|---|---|
| ~0.48 | 0.483 | **0.240** | — | — |
| ~0.52 | 0.516 | **0.745** | — | — |
| 0.2–0.3 | — | — | 0.252 | 0.208 |
| 0.5–0.6 | — | — | 0.543 | 0.568 |
| 0.8–0.9 | — | — | 0.848 | 0.876 |

The crude model put 99% of its observations into the 0.4–0.6 band and was
wrong by up to 24 points inside it. The vol-aware model tracks the diagonal
within about 4 points across the whole range.

**This is not a profit claim.** Better calibration does not create edge — the
venue prices these markets too, and whether a lag is exploitable after fees and
adverse selection is a separate empirical question. What it buys you is that
"cheap versus fair" becomes a statement about the market rather than about a
broken constant. With a fair pinned at 0.50 +/- 0.02 you cannot detect a lag at
all.

Two related changes came with it:

- **No quote, no trade.** The old code fell back to the catch-up model when the
  order book was unavailable and booked a fill against a book it could not see.
  The desk now requires a live ask and counts a missing quote as a blocked entry.
- **Fees are inside the edge test.** `edge = fair - ask - fee` rather than
  checking the edge and paying the fee afterwards.

Run the old behaviour any time with `--legacy-signal`.

---

## 2. What the first live session showed, and what it cost

The Kalshi sleeve ran, took four trades, lost $135.98 on a $3,000 pot and hit
the daily stop at **-4.42% against a -2.00% limit**. The ledger explains all of
it, and none of the explanation is bad luck.

| trade | 1m signal | spot vs strike | model fair | bought | outcome |
|---|---|---|---|---|---|
| SOL | z = **+3.27** (up) | +7.9 bp | YES 0.707 | **NO** @0.230 | −52.70 |
| ETH | z = **+2.61** (up) | +8.2 bp | YES 0.776 | YES @0.720 | **+18.41** |
| BTC | z = **−2.94** (down) | −2.0 bp | YES 0.370 | YES @0.170 | −51.09 |
| SOL | z = **−3.55** (down) | −4.1 bp | YES 0.357 | YES @0.270 | −50.61 |

### Bug 1 — it traded on measurement noise

The BTC contract sat **2.03 bp** from its strike. The four reference venues
disagreed with each other by **3.66 bp** at that moment. The distance being
measured was smaller than the ruler's own error.

Propagating that uncertainty gives a fair of 0.370 **± 0.214**. The market's
0.170 sat comfortably inside that error bar. The "+0.19 edge" the sleeve acted
on was an artefact, and it produced the largest single loss of the session.

Distance versus reference noise, for all four trades:

| trade | distance | venue disagreement | ratio | fair error |
|---|---|---|---|---|
| SOL | +7.90 bp | 2.70 bp | 2.9x | ±0.064 |
| ETH | +8.17 bp | 2.44 bp | 3.3x | ±0.068 |
| **BTC** | **−2.03 bp** | **3.66 bp** | **0.6x** | **±0.214** |
| SOL | −4.13 bp | 3.57 bp | 1.2x | ±0.116 |

Fixed three ways. Reference uncertainty now enters the model as a second
variance term, `sigma_eff = sqrt(sigma_price^2 + ref^2)`, which pulls the fair
toward 0.50 exactly when the reference cannot be trusted. The strike distance
must beat the venue disagreement by 3x before it counts as a measurement at
all. And the required edge is now `min_edge + 2 x fair_error`, so an edge
smaller than its own uncertainty is not an edge.

### Bug 2 — it faded its own model

The rule was "take whichever side shows more edge". That sounds neutral. It
is not: whenever the model is *less* confident than the market, the cheap side
always wins the comparison. So a sleeve whose entire thesis is *follow the
move* systematically bet against it.

Look at the SOL trade. The signal was **+3.27 sigma up**, spot was **above**
the strike, and the model said YES was worth **0.707** — and it bought **NO**.
Three of the four trades bought the side opposite the move. All three lost. The
one trade that followed the move is the one that won.

The desk now refuses to buy a side its own model does not favour
(`require_model_side`, on by default).

### Bug 3 — sizing ignored the risk budget

Two $50 clips on a $3,000 pot put **3.3% at risk against a 2.0% daily stop**.
A single pair of simultaneous losses could breach the stop before it was ever
consulted, which is exactly what happened: the day ended at **221% of the
limit**.

For a binary bought outright the maximum loss is the stake, so open cost *is*
committed risk. The governor now publishes a loss budget and a `clip_cap`, and
both directional sleeves size against it. On a $1,000 pot with a 2% stop the
largest new stake is now $20, not $50.

### Bug 4 — the stop reset on restart, and the tuner was too slow

The daily-stop anchor is now persisted to `data/desk_day.json`, so restarting
the process no longer re-bases the stop. And a sleeve down 2% of the pot is
benched immediately at whatever sample it has, rather than waiting for eight
resolved trades — at −$34 per trade, that wait cost $270.

### Replaying the four trades through the new gates

```
sol   no  0.230  -52.70  REJECT reference_noise (2.93x < 3x)
eth   yes 0.720  +18.41  REJECT edge (+0.0366 < 0.1738, err 0.067)
btc   yes 0.170  -51.09  REJECT reference_noise (0.56x < 3x)
sol   yes 0.270  -50.61  REJECT reference_noise (1.16x < 3x)

session was -135.98; with the new gates it would be +0.00
```

All four are rejected, **including the winner**. That is the honest outcome and
worth stating plainly: the ETH trade had a genuine edge of +0.037 sitting
inside a ±0.067 error bar. It won by luck. Gates that kept it would be gates
fitted to the outcome rather than to the evidence.

Even with a hypothetically perfect reference price, none of the four come back:
three are refused because the model favoured the opposite side, and ETH misses
the edge bar by 0.0006.

## 3. The uncomfortable finding: the signal does not predict

The sleeve is named for a lag. Whether that lag exists is testable, so I tested
it — 1,218 signals of >=2.5 sigma across six majors, measuring the forward
return **signed by the direction of the triggering move**:

| horizon | mean forward return | t-stat | P(continuation) |
|---|---|---|---|
| 3 min | −0.109 sigma | −1.25 | 42.6% |
| 5 min | +0.014 sigma | +0.13 | 46.7% |

**There is no continuation.** A sharp one-minute move does not predict the next
three to five minutes in either direction. Spot behaves as a martingale, which
is also why the driftless model is the right structural form — and why the
model being well calibrated does not by itself produce profit.

A related check, since the first instinct was to blame volatility scaling: the
variance ratio of 15-minute to 1-minute returns is **0.96**, so scaling sigma
by sqrt(tau) is accurate to about 2%. That hypothesis was wrong and the data
said so.

So the four fixes above stop the sleeve losing. They do not make it win. On the
current evidence the directional sleeve has **no demonstrated edge**, and after
these gates it will trade rarely or not at all — which is the correct behaviour
for a strategy whose signal does not predict. A sleeve that sits flat is
telling you something true about the market.

What would change that: a genuinely predictive signal (order-book imbalance on
the venue itself, or cross-venue lead-lag with measured significance), or a
reference price precise enough that small dislocations become measurable. The
lock sleeves remain the structurally sound play, and they correctly sit flat
too — Kalshi 15m books quote `sum_asks` of 1.01–1.07, so there is no lock to
take.

---

## 4. Venue reachability is checked, and rechecked

The desk probes every venue at startup and again every three minutes, so a
network change is picked up without a restart. A sleeve whose venue stops
answering is disabled with the reason on screen; when the venue comes back the
sleeve is re-enabled and the allocator immediately re-splits the pot. Nothing
needs restarting when you move between networks.

When a hostname is redirected at the DNS layer the probe says so in plain
words. On a network where Polymarket is filtered you will see:

```
Polymarket gamma  resolves to block.acma.gov.au — requests are not reaching the venue
```

rather than a bare `SSL: CERTIFICATE_VERIFY_FAILED`, which is what a
block page's own certificate looks like from inside httpx and reads like a bug
in the bot. On a network where Polymarket resolves normally the Polymarket
sleeves simply run.

The venue strip in the header shows the current state and round-trip time for
each venue at a glance.

---

## 5. Two venues, four sleeves

| Sleeve | Venue | Kind | Reference price |
|---|---|---|---|
| **Spot-Lag** | Polymarket | directional, vol-aware | Binance (Polymarket's own basis) |
| **Kalshi Lag** | Kalshi 15m | directional, vol-aware | CF-style USD composite |
| **Poly Lock** | Polymarket | pair-complete, no direction | — |
| **Kalshi Lock** | Kalshi 15m | pair-complete, no direction | — |

```bash
python -m stablebot desk --auto                       # all four
python -m stablebot desk --auto --sleeves lag         # both directional
python -m stablebot desk --auto --sleeves kalshi_lag  # Kalshi directional only
python -m stablebot desk --auto --sleeves locks       # both pair-complete
```

### Why Kalshi got its own directional sleeve

Kalshi runs the same 15-minute crypto binaries and is structurally *better* to
model, because it publishes an explicit strike on every contract:

```
ticker        KXBTC15M-26SEP082200-00
title         BTC price up in next 15 mins?
yes_sub_title Target Price: $78,835.32
strike_type   greater_or_equal
close_time    2026-09-09T02:00:00Z
```

So the reference level is read straight off the contract instead of inferred
from a window-open candle, and the fair value is the same normal CDF:

```
fair_yes = Phi( log(spot / strike) / (sigma * sqrt(minutes left)) )
```

Both sides are then compared against their own ask with the fee included, and
whichever shows the larger edge is the candidate. The books are genuinely
liquid — a live BTC 15m book had $10,070 resting at 0.54 — and the fee schedule
is the same quadratic curve already implemented.

It trades. From a live run:

```
ETH   z=+2.61  strike 2,496.17  YES @0.720 vs fair 0.776  edge +0.0422  69.3 shares  ARMED
XRP   z=+2.82  strike 1.42      edge +0.0335 < 0.0400                                WATCH
BTC   z=+2.20  strike 78,835.32 z=+2.20 of 2.5 needed (sigma=0.0359%/min)            COLD
```

### Settlement basis: CF Benchmarks, not Binance

Kalshi settles these on **CF Benchmarks**, whose real-time indices are a
composite of USD spot books — Coinbase, Kraken, Bitstamp, Gemini, itBit, LMAX —
and which notably **exclude Binance**. Pricing a CF-settled contract off a
Binance USDT book carries a persistent basis. Measured live on BTC:

```
coinbase 78,869.01   kraken 78,856.60   bitstamp 78,854.53   gemini 78,869.62
BRTI-style median    78,862.80
Binance USDT         78,869.99      basis +7.19  (+0.9 bp)
```

0.9 bp is about 6% of a 15-minute BTC sigma, and it widens whenever USDT itself
moves. So `desk/reference.py` takes the **median of the CF constituent venues
that answer**, for both the model input and the settlement price. The median is
deliberate: it makes one venue printing a bad tick, or one API going stale,
unable to move the reference. Venue dispersion is reported alongside the price —
live it was 2.6 bp on BTC and 22.6 bp on BNB, which is worth knowing before
sizing a BNB trade.

All six coins have all four constituent venues available for both live spot and
1-minute historical closes, so settlement is resolved on the composite too.

**On using the real index.** The CF Benchmarks API needs credentials: `/assets`
returns 401 and every index ticker returns `Unknown id` unauthenticated, so I
could not confirm the endpoint shape. `CFBenchmarksSource` is wired and tried
first when `CF_BENCHMARKS_API_KEY` and `CF_BENCHMARKS_INDEX_<COIN>` are set, and
falls through to the composite on any failure — but it is **unverified**, and
the composite is what actually runs today. The composite is a methodology match,
not a reproduction: CF volume-weights over an averaging window with their own
outlier rules, where this is a median of last trades.

To keep the idle path cheap, the composite is only fetched once a signal has
fired. The always-on fair shown in the book comes from the single Binance tick
and is indicative; the number a trade is actually priced against is the
composite, and the ledger records both plus the method used.

---

## 6. The screen

```
 STABLEBOT DESK  PAPER  ● RUNNING              EQUITY $2,000.00  session +12.40 (+0.62%)
 uptime 01:12:03  autopilot ON  cycles 412     day +8.10 (+0.40%)  dd -0.12%  cash $1,987
 venues binance ● 41ms   poly ● BLOCKED   kalshi ● 88ms
```

| Panel | What it is for |
|---|---|
| **Opportunity book** | Every window being watched, best edge first. Live model fair for each coin whether or not a signal has fired. |
| **Open positions** | What is at risk right now, with time to settlement. |
| **Tape** | Fills and resolutions as they happen. |
| **Sleeves** | Per-strategy allocation, clip, trade count, hit rate against its own break-even, realised PnL, cumulative sparkline. |
| **Risk** | Distance to the daily stop and the drawdown halt, current size multiplier, equity curve. |
| **Why no trade** | Which gate is blocking entries, ranked. The panel that answers "it never does anything". |
| **Log** | Venue probes, benching decisions, auto-tune changes. |

Book states: `ARMED` would enter now · `WATCH` quoted, edge under the bar ·
`HELD` already in this window · `COLD` no signal or no two-sided quote ·
`ERR` the venue did not answer.

### Keys

| key | action |
|---|---|
| `q` | quit — open paper positions stay in the ledger and resolve next run |
| `space` | pause: stops new entries, still resolves open positions |
| `a` | toggle autopilot (off = current sizing frozen, no benching, no tuning) |
| `1`–`9` | focus one sleeve across book, positions and tape |
| `0` | show everything again |
| `h` / `c` | raise / clear the halt file |
| `r` | force a rebalance now |
| `?` | help |

---

## 7. What runs it while you are not watching

### Risk governor — three independent brakes

1. **Daily stop.** Equity down more than `--daily-stop` (default from
   `config.yaml`, 2%) against the UTC-day open: entries stop. Re-bases at the
   day roll.
2. **Drawdown halt.** Equity down more than `--max-drawdown` (default 10%)
   from the session peak: entries stop.
3. **Halt file.** `data/desk_halt` exists: entries stop everywhere. Press `h`,
   or `touch data/desk_halt` from any other terminal.

Between fine and stopped there is a **throttle band**. Once the day is halfway
to the stop, position size scales down linearly to a quarter rather than
trading stopping outright, so an ordinary losing streak shrinks risk instead of
ending the session.

Halting never abandons open positions — they still resolve. Only new entries
are gated.

### Allocator — capital follows results

Every 60 seconds each sleeve is scored on expectancy per dollar risked over its
last 50 resolved trades, and the budget is split in proportion. A sleeve with
no track record gets a fixed exploration slice rather than being trusted or
ignored.

- Negative expectancy over at least 8 resolved trades: **benched**, with a
  re-probe 45 minutes later. A sleeve that stops working sits out; it is never
  permanently written off.
- No sleeve exceeds 60% of the budget or drops below 5% while enabled.
- A sleeve disabled because its venue is unreachable stays disabled — the
  allocator does not revive it.

### Auto-tuning — the entry bar moves itself

A binary bought at `p` needs to win more than `p` of the time to break even.
Every five minutes each sleeve compares its realised hit rate against its own
average entry price:

- Hit rate more than 2 points **below** break-even: raise `min_edge`.
- More than 6 points **above**: relax it back toward the floor.
- In between: leave it alone.

Separately, a sleeve that has taken zero trades after 200 evaluations, where
the gate counter says the *signal* threshold is what is blocking, walks
`z_entry` down toward 1.5 sigma. That is the specific failure you hit, now
self-correcting instead of silent.

Two guards keep that arm honest, because loosening an entry gate is the one
tuning move that can quietly make a losing sleeve lose faster:

- **Zero trades means zero ever, not zero since the restart.** Realised PnL is
  persisted per pot, but the trade counters used to live only in memory, so a
  restart made a sleeve that had already traded and lost look brand new and
  idle. Each sleeve now replays its own ledger at startup and logs what it
  found (`replayed 6 prior fills / 6 resolved`).
- **A sleeve that is down on its pot is never loosened.** Buying more of a
  signal that has already been paid for and found wanting is the wrong
  direction to tune. Recover to flat first, or raise the bar by hand.

Everything the tuner does is written to the log panel.

---

## 6b. Running it somewhere else

`--headless` drops the Rich Live view and the raw-mode key reader, because a
service manager has neither a tty nor a keyboard. Desk notes go to stdout for
journald. `--serve [HOST:]PORT` publishes `DeskState` as JSON at `/state`, and
`--remote URL` draws that snapshot with the same renderer the local desk uses —
one dashboard implementation, not two that drift. A test asserts the contract:
a round trip through the wire must produce a byte-identical screen.

    # on the box
    stablebot desk --headless --serve 127.0.0.1:8787
    # on your laptop
    ssh -N -L 8787:127.0.0.1:8787 you@desk-host
    stablebot desk --remote http://127.0.0.1:8787

The snapshot carries positions, equity and P&L, so the endpoint binds loopback
and refuses any other interface unless `STABLEBOT_DESK_TOKEN` is set. The viewer
is read-only.

See DEPLOY.md for the measured case for us-east-2 and what colocation does not
fix.

---

## 6c. Benching, and getting a sleeve back

The allocator benches a sleeve that is bleeding without waiting for a full
sample. The threshold is `AllocatorCfg.loss_bench_frac`, **6% of the pot**.

It measures the drop **since the sleeve last came back**, not since the session
opened. That distinction is the whole fix: session realized PnL never recovers,
so a lifetime threshold pins a sleeve off permanently — it gets re-probed after
45 minutes, is still past the line on the same stale number, and is benched
again on the very next rebalance. The baseline is reset when the sleeve is
re-probed, when the operator resumes it, and at startup — the tuner should
remember yesterday, the bench should not.

**`e` resumes a benched sleeve.** With a sleeve focused (`1`-`9`) it toggles
that one; with the focus on `all` it wakes every benched sleeve. Resuming moves
the loss baseline to what the sleeve has already lost, otherwise the next
rebalance would bench it straight back on the number the operator just
overrode. An operator-disabled sleeve carries `operator:` rather than `auto:`
in its reason, and the allocator leaves it alone.

---

## 7a. Clip size and the daily stop are one decision

A binary bought outright loses the whole stake, so the clip is the unit the
daily stop is spent in. The only number that matters is the ratio:

    headroom = daily budget / clip = losing trades before the desk halts

The shipped defaults were a $50 clip against a 2% stop on a $3,000 pot — a
$60 budget, **1.2 clips**. One total loss ended the day, and `capacity =
budget_remaining - open_cost` then refused to open a second clip at all. A desk
that halts after one loss cannot gather the sample its own calibration report
needs (n>=30 before the buckets mean anything).

Current defaults: **clip $15, daily stop 16%** — a $480 budget, **32 clips**.
That is sized to reach the n>=30 the calibration report needs inside a single
day rather than over a week.

The stop moved with the **drawdown halt, now 25%**. The two have to move
together: a 16% daily stop under a 10% halt is dead code, because the halt
measures from the session peak and trips first on any day that opens below the
high. The halt must stay meaningfully above the daily stop for the pair to do
different jobs — this one catches a bad day, that one catches a bad run.

Raising the stop rather than cutting the clip buys the same trade count with
five times the damage per mistake, which is why the clip came down first.

Two tests hold the shape: `test_the_daily_stop_absorbs_a_losing_run_not_one_clip`
requires at least 4 clips of headroom, and
`test_the_daily_stop_stays_clear_of_the_drawdown_halt` requires the halt to be
at least `MIN_TIER_RATIO` (1.5x) the daily stop. The ratio was 2.0 for the old
4%/10% pair and was relaxed deliberately for 16%/25%, which is a tighter
separation: one very bad day now leaves 9 points before the session halt rather
than 6. Change either number and the pair tells you whether the other agrees.

None of this creates edge. It governs how fast the desk finds out.

---

## 7b. Calibration — is the model sharper than the price?

`stablebot calibration [--sleeve kalshi_lag] [--buckets 5]`

A fair value is a probability claim, so P&L over a handful of trades does not
test it. What tests it is whether the claimed probabilities match observed
frequencies — and whether they match them better than the ask, because the ask
is the market's own probability claim. The report scores three forecasters by
Brier score (mean squared error, lower better):

| forecaster | claim |
| --- | --- |
| model | `fair` from the fill record, complemented for a NO position |
| market | `entry_p`, the price actually paid |
| base rate | always predicting the overall win frequency |

`skill = 1 - Brier(model)/Brier(market)`. Above 0 the model beat the price;
below 0 the quote was the better forecast and the sleeve is paying fees to
express a view no sharper than what it lifted.

The bucket table breaks the same data down by claimed probability, so a model
that is confident in the wrong direction shows up as a positive `gap`. Because
every entry gate requires the model to claim *more* than the ask, `claimed`
sitting above `realized` is the specific failure to watch for.

Below 30 resolved trades the report says so. Read the direction, not the number.

---

## 7c. The stall watchdog

An enabled sleeve whose scan loop stops turning looks exactly like a quiet one:
the book freezes, no trade fires, and the dashboard reads as a calm market. A
benched sleeve labels itself (`off — venue unreachable: …`); a stalled one used
to say nothing at all.

The desk now measures the gap since each sleeve last *completed* a cycle and
calls it out after `max(90s, 4 x interval)` — floored so one slow scan is not an
accusation. A stalled sleeve shows `STALLED 612s` in reverse video on its block
and logs the silence, the interval and its last error. Recovery logs too.

A disabled sleeve is never called stalled: it is off on purpose and already
carries its reason.

---

## 8. Honest limits

- **Paper PnL is not cash.** Fills are assumed at the quoted ask with a modelled
  fee. Real books are thinner and slower, and the second leg of a lock does not
  always fill.
- **The fair value model is simple.** Driftless, constant volatility inside the
  window, no jump or microstructure term. It is a large improvement on a fixed
  constant, not a priced volatility surface.
- **Calibration is not edge.** A well-calibrated model finds *candidate*
  dislocations. Whether they survive fees, latency and adverse selection is
  something only a live-quote backtest or a funded trial answers.
- **The composite is not the index.** Kalshi settles on CF Benchmarks' published
  rate; paper resolves on a median of CF's constituent venues. Close to it, and
  much closer than Binance alone, but near a strike a few bp decides outcomes.
- **The directional sleeves have no demonstrated edge.** The signal test found
  no continuation (t = +0.13 at 5 min). The gates make them safe and honest,
  not profitable.
- **The allocator reallocates, it does not create.** If no sleeve has positive
  expectancy, it will correctly hand out smaller and smaller stakes and bench
  everything. That is the system working.
- **The lock sleeves are honest about there being no lock.** Kalshi 15m books
  currently quote `sum_asks` around 1.00–1.03, so the edge is negative and
  nothing fires. A lock sleeve that never fires is telling you the truth about
  the market.

---

## 9. Files

| Path | What |
|---|---|
| `src/stablebot/desk/signal.py` | Vol-aware fair, EWMA volatility, gate counters |
| `src/stablebot/desk/spot_lag_vol.py` | Vol-aware entry logic over the existing paper engine |
| `src/stablebot/desk/kalshi_lag.py` | Vol-aware directional sleeve on Kalshi 15m binaries |
| `src/stablebot/desk/reference.py` | CF-style composite reference price; CF Benchmarks seam |
| `src/stablebot/desk/sleeves.py` | Sleeve adapters for all four sleeves |
| `src/stablebot/desk/allocator.py` | Capital allocation, benching, auto-tuning |
| `src/stablebot/desk/risk.py` | Daily stop, drawdown halt, halt file, throttle band |
| `src/stablebot/desk/health.py` | Venue reachability and DNS-block detection |
| `src/stablebot/desk/render.py` | The screen |
| `src/stablebot/desk/app.py` | Orchestrator |
| `src/stablebot/desk/menu.py` | Launcher with auto-start |
| `scripts/vol_signal_study.py` | Model scoring against real outcomes |
| `tests/test_desk.py` | 53 tests over the desk core |
| `tests/test_desk_kalshi_lag.py` | 38 tests, including replays of the four real losing trades |

Ledgers and sessions are unchanged and still in `data/`. The vol-aware engine
writes the same `spot_lag` record shape as the original, with `model`, `z` and
`sigma_1m` added, so old and new ledgers replay together.
