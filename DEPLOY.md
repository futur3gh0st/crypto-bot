# Running the desk on a VPS

The desk is a persistent asyncio loop with in-memory state — seeded volatility,
open positions, gate counters — that writes paper sessions and ledgers to
`data/`. That shape rules out serverless: Vercel and Lambda cap execution time,
start cold (adding the latency this move is meant to remove), re-seed volatility
on every invocation, and give you an ephemeral filesystem. What it wants is a
small always-on VM.

## Why us-east-2

Measured, not assumed. Hostnames resolved against AWS's published ranges:

| host | used for | region |
| --- | --- | --- |
| `external-api.kalshi.com` | **orders and quotes** | **us-east-2 (Ohio)** |
| `api.elections.kalshi.com` | — | GLOBAL (CloudFront edge) |
| `data-api.binance.vision` | idle-path 1m tick, vol seeding | ap-northeast-1 (Tokyo) |
| `clob.polymarket.com` | Polymarket CLOB | Azure |

RTT from a laptop in Australia, TCP connect, five samples:

```
external-api.kalshi.com    243-275 ms
data-api.binance.vision    216-274 ms
```

Inside us-east-2 the Kalshi leg is single-digit milliseconds. On the critical
path — see a move, price it, send the order — that is roughly:

```
signal -> order, 3 sequential round trips
  from Australia   750 ms
  from us-east-2    30 ms
```

Binance sitting in Tokyo does not change the answer. The *trading* fair is
priced against the CF-constituent composite (Coinbase, Kraken, Bitstamp,
Gemini), all US-hosted; Binance only feeds the cheap idle-path tick and the
volatility seed.

Pick **us-east-2**. us-east-1 is ~10 ms further and has more provider choice;
either beats a laptop by two orders of magnitude.

## What colocation does not fix

- **Scan cadence.** At `throttle_ms: 180` over ~12 calls a cycle, 2.16 s of a
  5.16 s cycle is our own rate limiter. Moving to Ohio takes the cycle to about
  2.3 s and the throttle is all of it.
- **Edge.** `stablebot calibration` currently reports skill −0.541: the price
  forecast the outcomes better than the model. Faster execution of a
  negative-edge model reaches the loss sooner. Speed is a multiplier on the sign
  of the edge, never a source of it.

The stronger reasons to move are the other two: a US host is not subject to the
ISP filtering that makes Polymarket unreachable from some networks (which
disables two of four sleeves *and* the cross-venue lock, the only
prediction-free edge in the book), and it runs whether or not a laptop is awake
and on a working network.

## Provision

Anything with 1 vCPU and 1 GB is plenty — this is one Python process on a 20 s
loop. t4g.small or Lightsail in us-east-2, or any VPS in Ashburn/Columbus.

```bash
sudo adduser --system --group --home /opt/stablebot stablebot
sudo -u stablebot git clone <your remote> /opt/stablebot
cd /opt/stablebot
sudo -u stablebot python3 -m venv .venv
sudo -u stablebot .venv/bin/pip install -r requirements.txt
sudo -u stablebot .venv/bin/pip install -e .
sudo -u stablebot cp .env.example .env   # then fill it in
```

## Run it — systemd

```bash
sudo cp deploy/stablebot-desk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stablebot-desk
journalctl -u stablebot-desk -f
```

`--headless` is what makes this work under a service manager: no Rich Live view
(there is no tty) and no raw-mode key reader (there is no keyboard). Desk notes
go to stdout, so `journalctl` holds the history. The unit sends SIGTERM on stop
and waits 30 s, which the desk handles by closing sleeves cleanly.

## Run it — Docker

```bash
docker compose up -d --build
docker compose logs -f
```

`data/` is bind-mounted. Without it every redeploy resets the paper pots to
$1,000 and throws away the ledger the calibration report reads.

## The second process: the maker forward test

`scripts/bt/maker_wx_poll.py` records Kalshi and Polymarket weather books and
prints for the pre-registered passive-quoting test (`maker_wx_fill.py` replays
them, `maker_wx_report.py` scores them). It is read-only and needs no keys.

It has to run on an always-on host, and this is measured, not assumed. Run on a
laptop from 12 to 13 September, `books.jsonl` held 144 snapshots over 28.1 h;
34 gaps longer than five minutes covered 24.4 h of that — **87% of the window
with no book**. `pmset -g log` shows the machine entering sleep at 00:10 local
and surfacing only for two-to-three-minute DarkWakes, and every poll timestamp
in that span lands inside one. The fill replay requotes only at a snapshot, so
a quote modelled as resting across a two-hour gap is filled through by every
print in between. That is an instrument fault, not adverse selection, and it
is why the first settled day is not scored.

The poller now prints `warn: clock jumped …` when its sleep overruns by more
than a poll interval, so a suspended host shows up in the log instead of in
the P&L.

Compose runs it as `maker-wx-poll` from the same image (`docker compose up -d`
starts both). Under systemd:

```bash
sudo cp deploy/stablebot-maker-wx.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stablebot-maker-wx
journalctl -u stablebot-maker-wx -f
```

`data/bt_cache/maker_wx/trades.jsonl` grows ~14 MB a day; 1 GB of disk covers
the 21-day window with room.

## Watch it from your laptop

The state feed carries positions, equity and P&L, so it binds to loopback and
`serve_state` refuses any other interface unless `STABLEBOT_DESK_TOKEN` is set.
Reach it over SSH:

```bash
ssh -N -L 8787:127.0.0.1:8787 you@desk-host      # terminal 1
stablebot desk --remote http://127.0.0.1:8787    # terminal 2
```

That draws the real dashboard — same renderer, same panels — from a snapshot
polled once a second. It is read-only: keys that pause, halt or rebalance are
deliberately not wired up, because the operator can always reach the daemon over
SSH and a viewer that can halt a desk over a loopback socket is a foot-gun.

If you would rather not run two processes, `ssh -t desk-host tmux attach` gives
you the full interactive desk with no extra machinery.

## Before you fund anything

Kalshi is CFTC-regulated. Paper trading from anywhere is fine; trading live from
outside the US through a US VPS is an eligibility question worth answering
before real money is involved.
