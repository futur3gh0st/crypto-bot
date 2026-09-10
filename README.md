# stablebot

> **New: `python -m stablebot desk`** — a full-screen trading desk that runs the
> paper sleeves unattended (menu, live book, allocator, risk governor, venue
> health). It also fixes the spot-lag entry gates, which could not be satisfied
> as shipped. See **[DESK.md](DESK.md)**.


Paper-trading research bot that watches stablecoin pairs on public CEX tickers, flags **fee-aware** cross-venue / cross-pair spreads and depegs, optionally overlays official X API v2 recent-search sentiment, and paper-fills locally. **This is not financial advice and it does not place live orders.** Retail stablecoin arb is usually fee-negative after taker fees, withdrawal/gas, and latency — the bot is built to show that honestly.

## What it does

Three paper sleeves (default backtest runs all of them):

1. **Funding harvest** — market-neutral long spot / short USD-M perp (or the cheaper side) when the trailing 3 funding prints share a sign and `|avg| >= 3 bp`. Hold and collect subsequent prints; exit on a sign flip or `|avg| < 0.5 bp`. No re-entry on the same symbol within 24h of an exit. If nothing in the universe clears 3 bp, sit flat. Default fee assumption is **maker** (`funding_use_maker: true`, 2 bp spot / 2 bp perp, configurable); taker path remains available. Sized at 50% of equity per name, 1x, sleeve cap 60%. **Source is OKX public funding** (`/api/v5/public/funding-rate-history`). Binance USD-M (`fapi.binance.com`) returns HTTP 451 from this host; there is no vision futures mirror.
2. **Depeg fade / re-peg (acute only)** — the pair must have been inside **±15 bps** of peg at some point in the prior 24h, **then** print cheaper than **-35 bps** vs USDT/USD for **2 consecutive hourly closes**. This skips chronic discounts (TUSD sitting at −38 bps for weeks). Exit at **-10 bps**, a **-150 bps** melt stop, or a **72h** time stop. One position per asset; after any exit the discount must first recover above -35 bps before a new entry (no restacking a stuck peg). USDT/USDC are only faded vs USD, and only if the dislocation is real.
3. **Closed cross-venue arb** — unchanged. Same-pair only after retail taker fees. Usually **zero trades**.
4. **Idle cash** — earns 0 unless a live public USDC supply APY can be fetched (DefiLlama Aave v3 Ethereum, fallback Aave dump). Never a hardcoded rate. Applied only to unallocated cash and labeled with source + exact APY.

Also:

- Scan Binance (via `data-api.binance.vision`), Coinbase, Kraken, and Bybit **public** book tickers (no keys).
- `scan` also prints live OKX funding and current depeg-fade candidates.
- `run`: loop + paper ledger in `data/ledger.sqlite`. High X fear cuts **depeg-fade** size only; the funding hedge stays on.
- `backtest`: joint hourly book. Funding uses real OKX prints (no lookahead). Depeg/arb: signal on bar t close, fill at bar t+1 open.
- X overlay is **optional** and **live-only**. Backtests are price-only (`X overlay not applied historically`).
- Daily loss circuit: if day PnL ≤ **-2%**, flatten depeg-fade and stop new risk that day. Open funding hedges remain.

## Honesty

This is not a money printer. Entry requires **3 bp per 8h**; quieter prints sit flat (a 0-trade month at $0.00 is a success versus fee-churn). At **3 bp per 8h** a **$1,000** notional collects about **$0.90/day** before fees (`1000 × 0.0003 × 3`). The book puts **50%** of equity on the funding sleeve. Default maker 2+2 bp + 1 bp half-spread is about **12 bps** round-trip. Last-30d OKX `|mean|` funding on majors is often well below 3 bp, so many windows will be flat. Depeg-fade is acute-only and **can lose** (the -150 bp stop exists because of Terra-style blowups). Closed CEX arb was already flat after retail taker fees. **Basis (spot vs mark) is approximated as 0** and labeled in every report; do not treat funding PnL as locked-in carry. No live orders, no wash trades, no invented fills.

## What it does not do

- No live exchange orders (even if `LIVE=1`).
- No wash trading, spoofing, or any manipulation.
- No promise of profit. A flat or negative backtest is the expected base case.
- Does not scrape X/Nitter. Official API only, and only with `X_BEARER_TOKEN`.

## Setup

```bash
cd /workspace/crypto-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# or: pip install -r requirements.txt && export PYTHONPATH=src
cp .env.example .env   # optional
```

## Commands

```bash
# one-shot public scan (no keys)
PYTHONPATH=src python -m stablebot scan

# paper loop
PYTHONPATH=src python -m stablebot run --interval 60

# digest of logged scans / fills
PYTHONPATH=src python -m stablebot digest --window hourly
PYTHONPATH=src python -m stablebot digest --window daily

# backtests (public klines; falls back to tests/fixtures if network fails)
PYTHONPATH=src python -m stablebot backtest --days 7 --balance 1000
PYTHONPATH=src python -m stablebot backtest --days 30 --balance 1000
PYTHONPATH=src python -m stablebot backtest --from 2026-07-14 --to 2026-08-14 --balance 500

# demo presets
PYTHONPATH=src python -m stablebot backtest --days 7 --balance 100
PYTHONPATH=src python -m stablebot backtest --days 7 --balance 1000
PYTHONPATH=src python -m stablebot backtest --days 7 --balance 5000
PYTHONPATH=src python -m stablebot backtest --days 30 --balance 1000
PYTHONPATH=src python -m stablebot backtest --days 7 --balance 1000 --fixture
PYTHONPATH=src python -m stablebot backtest --days 7 --balance 1000 --strategies funding,depeg,arb

# Polymarket crypto Up/Down (paper is the default — no live orders)
PYTHONPATH=src python -m stablebot poly-scan
PYTHONPATH=src python -m stablebot poly-scan --windows 5,15 --coins btc,eth,sol
PYTHONPATH=src python -m stablebot poly-run --interval 10
PYTHONPATH=src python -m stablebot poly-run --interval 10 --fade
PYTHONPATH=src python -m stablebot poly-live-check

# Polymarket Up/Down paper backtest (public history; no live orders)
PYTHONPATH=src python -m stablebot poly-backtest --days 7 --balance 1000
PYTHONPATH=src python -m stablebot poly-backtest --days 7 --balance 1000 --fade

# Kalshi 15m YES/NO (paper only — no live orders, no keys)
PYTHONPATH=src python -m stablebot kalshi-scan
PYTHONPATH=src python -m stablebot kalshi-scan --series KXBTC15M,KXETH15M
PYTHONPATH=src python -m stablebot kalshi-run --interval 15
```

Reports land in `data/backtests/` with a `book_` prefix (JSON + daily CSV + PNG equity/PnL chart). Polymarket sleeve reports use a `poly_` prefix. History last/mid is not an ask — lock fills on mids may be unfillable.

If you used `pip install -e .`, drop `PYTHONPATH=src`.

## Env vars

| var | required | purpose |
|---|---|---|
| *(none)* | | Price scan, paper run, backtest |
| `X_BEARER_TOKEN` | for X overlay | Official X API v2 recent search |
| `LIVE` | unused | CEX dummy; does **not** enable Polymarket live |
| `POLY_LIVE` | no (keep `0`) | Must be exactly `1` **and** other gates to arm poly live |
| `POLY_PK` or `PK` | for live only | Wallet private key (never commit) |
| `CLOB_API_KEY` / `CLOB_SECRET` / `CLOB_PASS_PHRASE` | for live only | CLOB L2 creds (never commit) |
| `POLY_FUNDER` / `POLY_SIGNATURE_TYPE` / `POLY_BUILDER_CODE` | optional | Proxy/Safe/deposit + builder |
| `STABLEBOT_ROOT` / `STABLEBOT_CONFIG` | no | Override paths |

## Fees (defaults in `config.yaml`)

These are **retail-ish taker** defaults, not VIP:

- Binance / Bybit: 10 bps
- Kraken: 26 bps
- Coinbase Exchange: 60 bps (small-account default)

Edit `config.yaml` if your schedule is better. Withdrawal fees and latency are **not** modeled — real fills would be worse.

## X / Twitter

When `X_BEARER_TOKEN` is set, `scan` / `run` call `GET /2/tweets/search/recent` (api.x.com, fallback api.twitter.com). Tweets are keyword-classified (`peg-stress`, `regulatory`, `bullish-mint`, `noise`) and scored by likes/retweets/followers. High fear cuts size or skips paper fills. Recent search is a **paid** X API product; free/basic tiers may 403. Without a token the bot prints `X disabled (no bearer token)` once and keeps going. Backtests do not call X.


## Polymarket crypto Up/Down (paper)

Short-window BTC/ETH/SOL/XRP Up or Down (5m and 15m). **Paper is the default.** The live CLOB path is off until several independent gates all pass.

Two strategies, labeled separately:

1. **Pair-complete (locked).** Buy Up **and** Down when `ask_up + ask_down < 1 - fee`. Locked profit per share pair is `1 - sum_asks - fee`. This is the only risk-free-ish edge. `poly-run` paper-fills when `lock_edge > min_lock` (default 0.5¢) and both asks are quoted. Ledger: `data/poly_ledger.jsonl`.
2. **Dislocation / inventory (directional, off by default).** `--fade` buys a side that is ≥ 8¢ cheap vs a **crude** fair (clipped-linear of spot vs window-open; 0.50 at open). Completing the other side later is the goal. This is **not** a lock.

**Honesty.** A live check at ~2026-08-14 22:51Z showed `sum_asks = 1.01` on BTC 5m/15m current and next windows — **no lock**. The tweet claiming an $81k Polymarket bot is **unverified**. Do not treat paper PnL as cash.

**Fees.** `poly.taker_fee_bps` defaults to **0** and the scan labels `fee assumed 0 — check live schedule`. Polymarket has changed fees. Official crypto taker (2026-08 docs) is the curve `C × 0.07 × p × (1-p)`, not a flat bps, and we do **not** apply it or invent a rebate. Set `taker_fee_bps` if you want a conservative flat haircut.

Quotes come from CLOB `/price` (bid = `side=buy`, ask = `side=sell`). `/book` returned 0.01/0.99 wings and is not used on the paper path. Live/dry-run re-reads the book and takes **best ask only** (lowest ask / highest bid) so wing junk is ignored. Spot/open from `data-api.binance.vision` (not `api.binance.com` / `fapi`, which 451 here).



## Kalshi 15m YES/NO (paper)

Binary 15-minute up/down on Kalshi (`KXBTC15M`, ETH/SOL/XRP/DOGE/BNB/HYPE, gold/silver/WTI, Nasdaq `KXNDQ15M`, S&P `KXINX15M`). **Paper only. No API keys. No live orders. No fade.**

**Pair-complete.** One market, YES and NO. Paper-buy both when `yes_ask + no_ask + fees` leaves `lock_edge > min_lock` (default 0.03). Hold both to settlement (one pays $1). Same assumption as poly: both derived asks fill. `live=false` always.

Kalshi's market list `yes_ask`/`no_ask` are unused (they were None). Quotes come from `GET /markets/{ticker}/orderbook` bids only:

- `yes_ask = 1 - best_no_bid` (size = that no-bid size)
- `no_ask = 1 - best_yes_bid` (size = that yes-bid size)

**Fees.** Paper uses `0.07 * p * (1-p)` per side (unrounded per-contract, same as poly). Official July 2026 Kalshi schedule is `round_up(0.07 * C * P * (1-P))`. Fee is subtracted **before** accepting a lock. No rebate.

**Same $1000 pool as Polymarket.** `data/poly_session.json` is the shared scoreboard:

`equity = starting_equity + poly pair_complete/complete_hedge pnl + kalshi pair_complete pnl`

Kalshi writes `data/kalshi_ledger.jsonl` (never `poly_ledger.jsonl`). No second $1000. No cash-gate.

`kalshi-run` loops every 15s by default. Public host `https://external-api.kalshi.com/trade-api/v2`. One reused httpx client, 180ms throttle, backoff on 429. A series that 404s is dropped after one probe; empty open books (weekend metals/index) skip quietly.

## Live (off by default)

Live pair-complete is **not** armed by installing the bot, by `LIVE=1`, or by `--fade`. Paper stays the default command:

```bash
PYTHONPATH=src python -m stablebot poly-run --interval 10
```

A real CLOB POST requires **all** of these:

1. `poly-run --live` (not `--fade`; `--live-dry-run` walks the same path and must not POST)
2. Env `POLY_LIVE=1` (nothing else — not `LIVE=1`)
3. File `data/poly_live_confirm.txt` whose stripped contents are exactly `I_ACCEPT_LIVE_POLY_ORDERS`
4. File `data/poly_halt` must **not** exist (if it appears mid-loop, sending stops)
5. `--fade` is a hard error with `--live`
6. `py_clob_client_v2` importable (`pip install '.[poly-live]'`)
7. Env: `POLY_PK` (or `PK`), `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASS_PHRASE`. Optional: `POLY_FUNDER`, `POLY_SIGNATURE_TYPE` (`0` EOA, `1` POLY_PROXY, `2` POLY_GNOSIS_SAFE, `3` POLY_1271), `POLY_BUILDER_CODE`
8. Both token ids on the row
9. Both best-ask sizes >= `live_ask_size_mult` × shares (default 2x); both bids present and > 0; neither side crossed (bid > ask)
10. shares <= `live_max_shares` (default 20)
11. Today's UTC live filled notional <= `live_daily_notional` (default 200)
12. pUSD collateral >= shares*(ask_up+ask_down) plus `live_balance_buffer` (default 10%). If the balance API is missing, live refuses rather than guess
13. `min_lock` still holds after the official curve fee `C * 0.07 * p * (1-p)`
14. Skip if either side's unwind spread (ask − bid) >= lock after curve
15. One complete per slug
16. `--live-dry-run` logs the would-be FOK orders (thinner ask first) and does not post

If `--live` is passed and any startup gate fails, the process **refuses to start** (it does not silently paper). `poly-live-check` prints a names-only credentials checklist, address, signature type, funder, confirm/halt status, and pUSD balance/allowance if available. It never places an order and never prints key/secret/pk values.

Live now requires 2x ask depth, both bids, and skips if unwind spread >= lock after curve; the thinner book is sent first. If the first FOK fails, the second is not sent. If the second fails, it immediately FOK-sells the filled first leg (best bid, else market) and ledgers `one_leg_unwind` with `live=true`. Fade / complete_hedge are not enabled on live. This is not a claim of profit.

## Layout

See `src/stablebot/` — exchanges, market math, paper ledger, backtest engine, CLI. Unit tests in `tests/` (no network). Reports land in `data/backtests/`.
