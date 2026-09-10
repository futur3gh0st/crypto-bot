# Crypto bot — run (paper) + fund for real

Not financial advice. Paper PnL is **not** cash. Live can lose money (one-leg fills, fees, thin books, latency).

## What’s in this zip

| Path | What |
|------|------|
| `src/stablebot/` | Bot code (pair-complete, spot_lag, Kalshi paper, live gates) |
| `scripts/` | Backtests / sleeve hunt helpers |
| `config.yaml` | Defaults |
| `.env.example` | Env template (copy to `.env`; never commit secrets) |
| `data/spot_lag_HOW_TO_RUN.md` | Spot-lag paper knobs |
| `tests/` | Unit tests |

**Not included:** `.venv`, live keys, your paper session/ledger history, big backtest caches.

---

## 1) One-time setup (paper or live)

Needs **Python 3.11+**.

```bash
cd crypto-bot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -U pip
pip install -e .
cp .env.example .env
# leave POLY_LIVE=0 and keys empty for paper
```

Optional live CLOB client (only if you later arm live pair-complete):

```bash
pip install -e '.[poly-live]'
```

---

## 2) Run paper (recommended first)

### A) Pair-complete (the sleeve that’s been printing on the $1k pot)

```bash
cd crypto-bot
export STABLEBOT_ROOT="$(pwd)"
PYTHONUNBUFFERED=1 .venv/bin/python -m stablebot poly-run \
  --interval 15 \
  --windows 5,15 \
  --coins btc,eth,sol,xrp,doge,bnb,hype
```

Session / ledger (created on first fill):

- `data/poly_session.json`
- `data/poly_ledger.jsonl`

One-shot scan:

```bash
.venv/bin/python -m stablebot poly-scan --windows 5,15 --coins btc,eth,sol,xrp,doge,bnb,hype
```

### B) Spot-lag (separate $1k paper pot; **no live CLOB path**)

```bash
PYTHONUNBUFFERED=1 .venv/bin/python -m stablebot spot-lag-run \
  --interval 15 \
  --coins btc,eth,sol,xrp,doge,bnb \
  --windows 5 \
  --catchup 0.7 --slip 0.02 --min-edge 0.04 --threshold 0.003 \
  --balance 1000
```

`--live` is **refused** for spot-lag. Details: `data/spot_lag_HOW_TO_RUN.md`.

### C) Kalshi 15m paper (optional; shares the poly session scoreboard)

```bash
.venv/bin/python -m stablebot kalshi-run --interval 15
```

---

## 3) Fund for real (Polymarket live — pair-complete only)

Live only applies to **`poly-run --live`**. Spot-lag and Kalshi stay paper-only in this build.

### What you need

1. **Polymarket account** (polymarket.com) that can trade crypto Up/Down.
2. **Wallet** on **Polygon** that Polymarket uses for trading (EOA or their proxy/Safe — match `POLY_SIGNATURE_TYPE`).
3. **USDC on Polygon** → deposit / convert so the trading wallet shows **pUSD / collateral** balance on Polymarket (exact UI label can change; the bot checks the CLOB balance API).
4. **CLOB L2 API credentials** derived from that wallet (API key / secret / passphrase). Official flow is via Polymarket’s CLOB docs / `py-clob-client-v2` derive helpers — do this on **your machine**, never paste the private key into chat.
5. Enough **MATIC/POL** for gas if your wallet type needs on-chain approvals (proxy setups often handle this differently).

### Suggested starting size

Live defaults in this bot are conservative:

- `live_max_shares` ≈ **20** per lock
- `live_daily_notional` ≈ **$200**/day UTC
- balance buffer ≈ **10%**

Practical starting cash: about **$50–$250** pUSD to exercise the path, not “all-in.” Scale only after dry-run looks sane.

### Put secrets in `.env` (local only)

```bash
cp .env.example .env
# edit .env — never commit, never paste into chat
```

Minimum for live:

```
POLY_LIVE=0                 # keep 0 until every other gate is ready
POLY_PK=0x...               # trading wallet private key (or use PK=)
CLOB_API_KEY=...
CLOB_SECRET=...
CLOB_PASS_PHRASE=...
# Optional if you use Polymarket proxy/Safe:
# POLY_FUNDER=0x...
# POLY_SIGNATURE_TYPE=0     # 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, 3=POLY_1271
# POLY_BUILDER_CODE=...
```

### Arming checklist (all required)

1. `pip install -e '.[poly-live]'`
2. Fill `.env` keys (still with `POLY_LIVE=0`)
3. Check without sending orders:

```bash
.venv/bin/python -m stablebot poly-live-check
```

   It prints address / funder / confirm+halt status / balance if available — **never** prints key values.

4. Dry-run the live path (logs would-be FOKs, no POST):

```bash
.venv/bin/python -m stablebot poly-run --interval 15 --live-dry-run \
  --windows 5,15 --coins btc,eth,sol,xrp,doge,bnb,hype
```

5. When you accept risk, create **exactly**:

```bash
mkdir -p data
printf 'I_ACCEPT_LIVE_POLY_ORDERS' > data/poly_live_confirm.txt
# ensure data/poly_halt does NOT exist
```

6. Set `POLY_LIVE=1` in `.env`.

7. Start live:

```bash
.venv/bin/python -m stablebot poly-run --interval 15 --live \
  --windows 5,15 --coins btc,eth,sol,xrp,doge,bnb,hype
```

If any gate fails, the process **exits** (it will not silently paper).

### Emergency stop

```bash
touch data/poly_halt    # stops new live sends
# and/or set POLY_LIVE=0 and kill the process
```

### Live behavior (honest)

- Buys **both** Up and Down when lock edge clears fees + depth gates (FOK, thinner side first).
- If the second leg fails, it tries to **unwind** the first leg immediately.
- Fade / spot-lag are **not** on the live path.
- Paper equity on my computer is unrelated to your wallet — funding live does not copy paper PnL.

---

## 4) Security

- Never commit `.env`, `POLY_PK`, or CLOB secrets.
- Never paste a private key or API secret into chat / email / Slack.
- Prefer a **dedicated** trading wallet with only what you’re willing to lose.
- Start with `--live-dry-run` and tiny daily notional.

---

## 5) Quick command cheat sheet

| Goal | Command |
|------|---------|
| Paper pair-complete | `python -m stablebot poly-run --interval 15 --windows 5,15 --coins btc,eth,sol,xrp,doge,bnb,hype` |
| Paper spot-lag | `python -m stablebot spot-lag-run --interval 15 --coins btc,eth,sol,xrp,doge,bnb --windows 5 --catchup 0.7 --slip 0.02 --min-edge 0.04 --threshold 0.003 --balance 1000` |
| Live credentials check | `python -m stablebot poly-live-check` |
| Live dry-run | `python -m stablebot poly-run --live-dry-run ...` |
| Live (all gates) | `POLY_LIVE=1` + confirm file + `poly-run --live ...` |

Full detail also lives in `README.md`.
