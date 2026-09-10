# spot_lag paper sleeve — how to run

Paper only. **No wallet required.** Never posts Polymarket CLOB orders.
Separate from pair-complete (`poly_session.json` / `poly_run`).

## Params (hunt winner)

| knobs | value |
|-------|-------|
| catchup | 0.70 |
| slip | 0.02 |
| min_edge | 0.04 |
| threshold | 0.3% (0.003) |
| window | 5m |
| coins | btc,eth,sol,xrp,doge,bnb |
| sizing | clip = min(5% equity, $50); shares = clip / entry_p |
| fees | 0.07 × p × (1−p) charged into paper equity at fill |
| session start | $1000 in `data/spot_lag_session.json` if missing |

## One-time setup

```bash
cd /workspace/crypto-bot
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .
```

## One-shot scan (smoke)

```bash
cd /workspace/crypto-bot
STABLEBOT_ROOT=/workspace/crypto-bot PYTHONUNBUFFERED=1 \
  .venv/bin/python -m stablebot spot-lag-scan \
  --coins btc,eth,sol,xrp,doge,bnb --windows 5
```

## Continuous paper loop

```bash
cd /workspace/crypto-bot
STABLEBOT_ROOT=/workspace/crypto-bot PYTHONUNBUFFERED=1 \
  .venv/bin/python -m stablebot spot-lag-run \
  --interval 15 \
  --coins btc,eth,sol,xrp,doge,bnb \
  --windows 5 \
  --catchup 0.7 --slip 0.02 --min-edge 0.04 --threshold 0.003 \
  --balance 1000 \
  >> data/spot_lag_run.log 2>&1 &
```

Optional knobs: `--catchup`, `--slip`, `--min-edge`, `--threshold`, `--balance`
(seeds session only when `data/spot_lag_session.json` is missing).

**`--live` is refused.** This sleeve is paper-only for now.

## Files

| path | role |
|------|------|
| `data/spot_lag_session.json` | paper equity / cash (separate from `poly_session.json`) |
| `data/spot_lag_ledger.jsonl` | fills `kind=spot_lag` + resolves `kind=spot_lag_resolve` |
| `data/spot_lag_run.log` | loop stdout/stderr |

## Ledger shapes

**Fill** (`kind: spot_lag`):

```json
{"ts":"...","kind":"spot_lag","slug":"btc-updown-5m-...","coin":"btc","direction":"UP",
 "move_pct":0.004,"fair_side":0.72,"entry_model":0.674,"live_ask":0.68,
 "fill_p":0.68,"entry_source":"live_ask","edge":0.04,"shares":73.5,"cost":50.0,
 "fee":...,"live":false,"note":"paper spot_lag fill; no live CLOB order"}
```

**Resolve** (`kind: spot_lag_resolve`):

```json
{"ts":"...","kind":"spot_lag_resolve","slug":"...","resolved":"UP","won":true,
 "scratched":false,"pnl":...,"fee":...,"pnl_after_fee":...,"equity":...,"live":false}
```

Resolution uses Binance window open vs close. FLAT scratches (refund stake; fee already paid at fill).

## Leave pair-complete alone

Do **not** stop `poly-run` / `kalshi-run` for this sleeve. spot_lag has its own session + ledger.

## Live wallet

Out of scope here. Parent agent will document the secure live path separately.
