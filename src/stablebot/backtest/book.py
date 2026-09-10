from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from stablebot.backtest.engine import (
    _fill_opp_at,
    _max_dd,
)
from stablebot.backtest.funding_hist import FundingBook
from stablebot.backtest.history import HistoryBook, bars_to_quotes
from stablebot.config import AppConfig
from stablebot.market.depeg import DepegAlert, find_depegs
from stablebot.market.funding import FundingPrint, cost_usd
from stablebot.market.spreads import find_opportunities
from stablebot.paper.engine import simulate_fill
from stablebot.market.idle_yield import IdleYield, idle_cash_pnl
from stablebot.strategy.depeg_fade import (
    DepegPosition,
    acute_entry,
    depeg_pnl,
    deviation_bps,
    exit_reason,
    should_rearm,
)
from stablebot.strategy.funding_harvest import FundingState, one_way_bps
from stablebot.strategy.stable_spread import is_fillable, opp_key


@dataclass
class BookDayRow:
    date: str
    starting_equity: float
    funding_pnl: float
    depeg_pnl: float
    arb_pnl: float
    total_pnl: float
    ending_equity: float
    trades: int
    max_drawdown: float
    fees_paid: float
    funding_collected: float
    depeg_alerts: int = 0
    idle_pnl: float = 0.0


@dataclass
class BookTrade:
    ts: datetime
    strategy: str
    kind: str
    pair: str
    pnl: float
    fees_paid: float
    notional: float
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class BookResult:
    start: datetime
    end: datetime
    starting_balance: float
    ending_equity: float
    total_return: float
    total_pnl: float
    fees_paid: float
    funding_collected: float
    n_trades: int
    n_wins: int
    win_rate: float
    win_days: int
    lose_days: int
    max_drawdown: float
    depeg_wins: int
    depeg_losses: int
    strategies: list[str]
    idle_pnl: float = 0.0
    idle_note: str = "idle cash yield skipped (no live public rate)"
    days: list[BookDayRow] = field(default_factory=list)
    trades: list[BookTrade] = field(default_factory=list)
    depegs: list[DepegAlert] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    x_note: str = "X overlay not applied historically"
    universe: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "starting_balance": self.starting_balance,
            "ending_equity": self.ending_equity,
            "total_return": self.total_return,
            "total_pnl": self.total_pnl,
            "fees_paid": self.fees_paid,
            "funding_collected": self.funding_collected,
            "n_trades": self.n_trades,
            "n_wins": self.n_wins,
            "win_rate": self.win_rate,
            "win_days": self.win_days,
            "lose_days": self.lose_days,
            "max_drawdown": self.max_drawdown,
            "depeg_wins": self.depeg_wins,
            "depeg_losses": self.depeg_losses,
            "idle_pnl": self.idle_pnl,
            "idle_note": self.idle_note,
            "strategies": self.strategies,
            "universe": self.universe,
            "x_note": self.x_note,
            "skipped": self.skipped,
            "notes": self.notes,
            "days": [d.__dict__ for d in self.days],
            "trades": [
                {
                    "ts": t.ts.isoformat(),
                    "strategy": t.strategy,
                    "kind": t.kind,
                    "pair": t.pair,
                    "pnl": t.pnl,
                    "fees_paid": t.fees_paid,
                    "notional": t.notional,
                    **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in t.extra.items()},
                }
                for t in self.trades
            ],
            "depegs": [
                {
                    "ts": a.ts.isoformat(),
                    "venue": a.venue,
                    "pair": a.pair,
                    "mid": a.mid,
                    "deviation_bps": a.deviation_bps,
                }
                for a in self.depegs
            ],
        }


def parse_strategies(raw: str | Iterable[str] | None) -> list[str]:
    if raw is None:
        return ["funding", "depeg", "arb"]
    if isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    else:
        parts = [str(p).strip().lower() for p in raw if str(p).strip()]
    alias = {"depeg_fade": "depeg", "fade": "depeg", "spread": "arb", "cross": "arb"}
    out: list[str] = []
    for p in parts:
        p = alias.get(p, p)
        if p not in {"funding", "depeg", "arb"}:
            raise ValueError(f"unknown strategy: {p}")
        if p not in out:
            out.append(p)
    return out or ["funding", "depeg", "arb"]


def _pick_depeg_series(
    book: HistoryBook,
    cfg: AppConfig,
    end: datetime,
) -> tuple[dict[str, list], list[str]]:
    """One hourly series per asset. Prefer binance BASE/USDT. Drop stale."""
    fade = cfg.depeg_fade
    wanted = {a.upper() for a in fade.candidates}
    extras = {a.upper() for a in fade.fiat_only_extras}
    quotes_ok = {q.upper() for q in fade.quotes}
    by_key: dict[tuple[str, str, str], list] = {}
    for b in book.bars:
        if b.base.upper() not in wanted and not (
            b.base.upper() in extras and b.quote.upper() == "USD"
        ):
            continue
        if b.quote.upper() not in quotes_ok:
            continue
        if b.base.upper() in extras and b.quote.upper() != "USD":
            continue
        by_key.setdefault((b.base.upper(), b.quote.upper(), b.venue), []).append(b)
    skipped: list[str] = []
    chosen: dict[str, list] = {}
    venue_pref = ("binance", "coinbase", "kraken", "bybit")
    assets = sorted({k[0] for k in by_key})
    for asset in assets:
        options = [(k, rows) for k, rows in by_key.items() if k[0] == asset]
        options.sort(
            key=lambda kv: (
                venue_pref.index(kv[0][2]) if kv[0][2] in venue_pref else 99,
                0 if kv[0][1] == "USDT" else 1,
                -len(kv[1]),
            )
        )
        picked = None
        for key, rows in options:
            rows = sorted(rows, key=lambda x: x.open_time)
            if not rows:
                continue
            last = rows[-1].open_time
            if last < end - timedelta(days=2):
                skipped.append(
                    f"depeg {key[2]} {key[0]}/{key[1]}: stale last bar {last.isoformat()}"
                )
                continue
            picked = rows
            chosen[asset] = picked
            break
        if picked is None and asset in wanted:
            skipped.append(f"depeg {asset}: no usable hourly series")
    return chosen, skipped


def run_book_backtest(
    cfg: AppConfig,
    spot: HistoryBook,
    funding: FundingBook | None,
    start: datetime,
    end: datetime,
    balance: float,
    strategies: list[str] | None = None,
    idle_yield: IdleYield | None = None,
) -> BookResult:
    """Joint hourly book: funding harvest + depeg fade + closed cross-venue arb."""
    strats = parse_strategies(strategies)
    notes = list(spot.notes)
    skipped = list(spot.skipped)
    if funding:
        notes.extend(funding.notes)
        skipped.extend(funding.skipped)
    notes.append("X overlay not applied historically (recent-search only; price-only backtest).")
    notes.append("No lookahead: funding enter after the 3rd confirming print (that print is not collected).")
    notes.append(
        "Funding entry requires 3 consecutive same-sign prints and trailing-3 |avg| >= 3 bp; "
        "sit flat if the universe is quieter. Anti-churn: no re-entry on the same symbol within 24h of an exit. "
        "Exit only on trailing-3 sign flip or |avg| < 0.5 bp."
    )
    notes.append(
        "Depeg fade is acute-only: pair must have been inside ±15 bps of peg in the prior 24h, "
        "THEN −35 bps for 2 hourly closes. Signal on bar-t close, fill at bar-t+1 open. "
        "One position per asset; reset required after exit. Chronic discounts (e.g. TUSD stuck at −38) do not enter."
    )
    notes.append(
        "Funding cash = notional * rate * side, minus one-way spot+perp maker (or taker) + half-spread on enter and exit. "
        "Basis (spot vs mark) approximated as 0."
    )
    idle_note = "idle cash yield skipped (no live public USDC supply APY fetched)"
    if idle_yield is not None:
        idle_note = (
            f"{idle_yield.label()}. Applied only to unallocated cash "
            "(equity minus open funding + depeg notionals). Not a hardcoded rate."
        )
    notes.append(idle_note)
    if "funding" in strats and (funding is None or not funding.prints):
        notes.append("Funding history missing — funding sleeve skipped (not invented).")

    equity = float(balance)
    peak = equity
    max_dd = 0.0
    trades: list[BookTrade] = []
    depegs: list[DepegAlert] = []

    day_start_eq: dict[str, float] = {}
    day_eq_path: dict[str, list[float]] = {}
    day_end: dict[str, float] = {}
    day_funding: dict[str, float] = {}
    day_depeg: dict[str, float] = {}
    day_arb: dict[str, float] = {}
    day_fees: dict[str, float] = {}
    day_trades: dict[str, int] = {}
    day_depegs: dict[str, int] = {}
    day_fund_coll: dict[str, float] = {}
    day_idle: dict[str, float] = {}
    day_halt: dict[str, bool] = {}

    def _day(ts: datetime) -> str:
        return ts.date().isoformat()

    def _touch(ts: datetime) -> str:
        d = _day(ts)
        day_start_eq.setdefault(d, equity)
        day_eq_path.setdefault(d, [equity])
        day_end.setdefault(d, equity)
        day_funding.setdefault(d, 0.0)
        day_depeg.setdefault(d, 0.0)
        day_arb.setdefault(d, 0.0)
        day_fees.setdefault(d, 0.0)
        day_trades.setdefault(d, 0)
        day_depegs.setdefault(d, 0)
        day_fund_coll.setdefault(d, 0.0)
        day_idle.setdefault(d, 0.0)
        day_halt.setdefault(d, False)
        return d

    def _apply(ts: datetime, sleeve: str, pnl: float, fee: float = 0.0, n_trades: int = 0) -> None:
        nonlocal equity, peak, max_dd
        d = _touch(ts)
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
        if sleeve == "funding":
            day_funding[d] += pnl
        elif sleeve == "depeg":
            day_depeg[d] += pnl
        elif sleeve == "idle":
            day_idle[d] += pnl
        else:
            day_arb[d] += pnl
        day_fees[d] += fee
        day_trades[d] += n_trades
        day_eq_path[d].append(equity)
        day_end[d] = equity
        start_eq = day_start_eq[d]
        day_pnl = (day_funding[d] + day_depeg[d] + day_arb[d] + day_idle[d])
        if start_eq > 0 and day_pnl <= -abs(cfg.risk.daily_stop_pct) * start_eq:
            day_halt[d] = True

    def halted(ts: datetime) -> bool:
        return day_halt.get(_day(ts), False)

    # ---- funding setup ----
    fcfg = cfg.funding
    ow_bps = one_way_bps(fcfg)
    fstates: dict[str, FundingState] = {}
    universe: list[str] = []
    prints_by_ts: dict[datetime, list[FundingPrint]] = {}
    if "funding" in strats and funding and funding.prints:
        universe = list(funding.universe)
        by = funding.by_inst()
        for inst in universe:
            st = FundingState(
                min_funding=fcfg.min_funding,
                exit_funding=fcfg.exit_funding,
                trail=fcfg.trail_prints,
                cooldown_hours=fcfg.cooldown_hours,
            )
            fstates[inst] = st
            for p in by.get(inst, []):
                # warmup prints before start: feed rates only (no trading)
                if p.ts < start:
                    st.rates.append(p.rate)
                else:
                    prints_by_ts.setdefault(p.ts, []).append(p)

    # ---- depeg series ----
    series, dep_skip = _pick_depeg_series(spot, cfg, end)
    skipped.extend(dep_skip)
    dcfg = cfg.depeg_fade
    closes: dict[str, list[float]] = {a: [] for a in series}
    armed: dict[str, bool] = {a: True for a in series}
    for asset, rows in series.items():
        for b in rows:
            if b.open_time < start:
                closes[asset].append(b.close)
    dpos: dict[str, DepegPosition] = {}
    pending_entry: dict[str, dict] = {}
    pending_exit: dict[str, dict] = {}

    # hourly grid from spot
    by_t = spot.by_time()
    hours = sorted(t for t in by_t if start <= t < end)
    # also include funding-only timestamps in range
    extra_ts = sorted(t for t in prints_by_ts if start <= t <= end and t not in by_t)
    timeline = sorted(set(hours) | set(extra_ts))

    open_arb: set[tuple] = set()
    max_frac = cfg.backtest.max_fraction
    base_notional = min(cfg.strategy.paper_notional_usd, balance)

    depeg_wins = 0
    depeg_losses = 0

    def funding_open_notional() -> float:
        return sum(st.position.notional for st in fstates.values() if st.position)

    def depeg_open_notional() -> float:
        return sum(p.notional for p in dpos.values())

    def flatten_depeg(ts: datetime, px_by_asset: dict[str, float], reason: str) -> None:
        nonlocal depeg_wins, depeg_losses
        for asset, pos in list(dpos.items()):
            px = px_by_asset.get(asset)
            if px is None or px <= 0:
                continue
            fee_bps = cfg.fee_bps(pos.venue) + cfg.strategy.hist_half_spread_bps
            pnl, fee = depeg_pnl(pos, px, fee_bps)
            _apply(ts, "depeg", pnl, fee, 1)
            trades.append(
                BookTrade(
                    ts=ts,
                    strategy="depeg",
                    kind=f"exit_{reason}",
                    pair=pos.pair,
                    pnl=pnl,
                    fees_paid=fee,
                    notional=pos.notional,
                    extra={"asset": asset, "exit_px": px, "entry_px": pos.entry_px},
                )
            )
            episode = pnl - pos.entry_fee
            if episode > 0:
                depeg_wins += 1
            elif episode < 0:
                depeg_losses += 1
            armed[asset] = False
            del dpos[asset]
        pending_entry.clear()
        pending_exit.clear()

    last_px: dict[str, float] = {}
    last_mark = start

    def _accrue_idle(from_ts: datetime, to_ts: datetime) -> None:
        if idle_yield is None or to_ts <= from_ts:
            return
        hours = (to_ts - from_ts).total_seconds() / 3600.0
        allocated = funding_open_notional() + depeg_open_notional()
        idle = max(0.0, equity - allocated)
        pnl = idle_cash_pnl(idle, idle_yield.apy, hours)
        if pnl:
            _apply(to_ts if to_ts < end else from_ts, "idle", pnl, 0.0, 0)

    for i, t in enumerate(timeline):
        _accrue_idle(last_mark, t)
        last_mark = t
        _touch(t)
        nxt = timeline[i + 1] if i + 1 < len(timeline) else None
        bars = by_t.get(t, [])
        quotes = bars_to_quotes(bars, cfg, use_open=False) if bars else []
        fill_quotes = []
        if nxt is not None and nxt - t <= timedelta(hours=1, minutes=5) and nxt in by_t:
            fill_quotes = bars_to_quotes(by_t[nxt], cfg, use_open=True)

        # current depeg mids for flatten / last_px
        px_now: dict[str, float] = {}
        for asset, rows in series.items():
            hit = next((b for b in rows if b.open_time == t), None)
            if hit is not None:
                px_now[asset] = hit.close
                last_px[asset] = hit.close

        # ---- funding prints at t ----
        if "funding" in strats:
            for p in prints_by_ts.get(t, []):
                st = fstates.get(p.inst_id)
                if st is None:
                    continue
                can_enter = (
                    not halted(t)
                    and st.position is None
                )
                # size after we know we want to enter — compute remaining sleeve
                used = funding_open_notional()
                sleeve_cap = equity * min(fcfg.sleeve_max, cfg.risk.funding_sleeve_max)
                room = max(0.0, sleeve_cap - used)
                want = equity * fcfg.equity_frac
                notional = min(want, room)
                if notional < 10:
                    can_enter = False
                events = st.on_print(
                    p.inst_id,
                    t,
                    p.rate,
                    can_enter=can_enter,
                    notional=notional,
                    one_way_cost_bps=ow_bps,
                )
                for ev in events:
                    if ev["kind"] == "funding_accrual":
                        _apply(t, "funding", ev["cash"], 0.0, 0)
                        day_fund_coll[_day(t)] += ev["cash"]
                    elif ev["kind"] == "funding_enter":
                        _apply(t, "funding", -ev["fee"], ev["fee"], 1)
                        trades.append(
                            BookTrade(
                                ts=t,
                                strategy="funding",
                                kind="enter",
                                pair=p.inst_id,
                                pnl=-ev["fee"],
                                fees_paid=ev["fee"],
                                notional=ev["notional"],
                                extra={"side": ev["side"], "avg": ev["avg"]},
                            )
                        )
                    elif ev["kind"] == "funding_exit":
                        _apply(t, "funding", -ev["fee"], ev["fee"], 1)
                        trades.append(
                            BookTrade(
                                ts=t,
                                strategy="funding",
                                kind="exit",
                                pair=p.inst_id,
                                pnl=-ev["fee"],
                                fees_paid=ev["fee"],
                                notional=ev["notional"],
                                extra={"side": ev["side"], "collected": ev["collected"]},
                            )
                        )

        # ---- depeg fade ----
        if "depeg" in strats and bars:
            alerts = find_depegs(quotes, cfg)
            depegs.extend(alerts)
            day_depegs[_day(t)] += len(alerts)

            # fills scheduled from previous bar
            for asset, pend in list(pending_exit.items()):
                hit = next((b for b in series.get(asset, []) if b.open_time == t), None)
                if hit is None:
                    continue
                pos = dpos.get(asset)
                if pos is None:
                    pending_exit.pop(asset, None)
                    continue
                px = hit.open
                fee_bps = cfg.fee_bps(pos.venue) + cfg.strategy.hist_half_spread_bps
                pnl, fee = depeg_pnl(pos, px, fee_bps)
                _apply(t, "depeg", pnl, fee, 1)
                trades.append(
                    BookTrade(
                        ts=t,
                        strategy="depeg",
                        kind=f"exit_{pend['reason']}",
                        pair=pos.pair,
                        pnl=pnl,
                        fees_paid=fee,
                        notional=pos.notional,
                        extra={"asset": asset, "exit_px": px, "entry_px": pos.entry_px},
                    )
                )
                episode = pnl - pos.entry_fee
                if episode > 0:
                    depeg_wins += 1
                elif episode < 0:
                    depeg_losses += 1
                armed[asset] = False
                del dpos[asset]
                pending_exit.pop(asset, None)

            for asset, pend in list(pending_entry.items()):
                if asset in dpos:
                    pending_entry.pop(asset, None)
                    continue
                hit = next((b for b in series.get(asset, []) if b.open_time == t), None)
                if hit is None:
                    continue
                if halted(t):
                    pending_entry.pop(asset, None)
                    continue
                px = hit.open
                if px <= 0:
                    pending_entry.pop(asset, None)
                    continue
                used = depeg_open_notional()
                sleeve_cap = equity * min(dcfg.sleeve_max, cfg.risk.depeg_sleeve_max)
                name_cap = equity * min(dcfg.per_name_max, cfg.risk.depeg_per_name_max)
                room = max(0.0, min(sleeve_cap - used, name_cap))
                notional = room
                if notional < 10:
                    pending_entry.pop(asset, None)
                    continue
                fee_bps = cfg.fee_bps(pend["venue"]) + cfg.strategy.hist_half_spread_bps
                units = notional / px
                fee = units * px * (fee_bps / 10_000.0)
                dpos[asset] = DepegPosition(
                    asset=asset,
                    pair=pend["pair"],
                    venue=pend["venue"],
                    entry_ts=t,
                    entry_px=px,
                    notional=notional,
                    units=units,
                    entry_fee=fee,
                )
                _apply(t, "depeg", -fee, fee, 1)
                trades.append(
                    BookTrade(
                        ts=t,
                        strategy="depeg",
                        kind="enter",
                        pair=pend["pair"],
                        pnl=-fee,
                        fees_paid=fee,
                        notional=notional,
                        extra={"asset": asset, "entry_px": px, "dev_bps": deviation_bps(px)},
                    )
                )
                pending_entry.pop(asset, None)

            # update closes + arming + schedule next-bar actions (no lookahead)
            for asset, rows in series.items():
                hit = next((b for b in rows if b.open_time == t), None)
                if hit is None:
                    continue
                closes[asset].append(hit.close)
                if asset not in dpos and not armed.get(asset, True):
                    if should_rearm(hit.close, dcfg.entry_bps):
                        armed[asset] = True
                if asset in dpos and asset not in pending_exit:
                    reason = exit_reason(
                        hit.close,
                        dpos[asset].entry_ts,
                        t,
                        dcfg.exit_bps,
                        dcfg.stop_bps,
                        dcfg.max_hold_hours,
                    )
                    if reason:
                        pending_exit[asset] = {"reason": reason}
                elif (
                    asset not in dpos
                    and asset not in pending_entry
                    and armed.get(asset, True)
                    and not halted(t)
                    and acute_entry(
                        closes[asset],
                        dcfg.consecutive_hours,
                        dcfg.entry_bps,
                        dcfg.peg_band_bps,
                        dcfg.lookback_hours,
                    )
                ):
                    pending_entry[asset] = {
                        "venue": hit.venue,
                        "pair": f"{hit.base}/{hit.quote}",
                    }

            if halted(t) and dpos:
                flatten_depeg(t, px_now or last_px, "circuit")

        # ---- closed cross-venue arb (existing rules) ----
        if "arb" in strats and quotes and fill_quotes and not halted(t):
            opps = find_opportunities(quotes, cfg)
            fillable = [o for o in opps if is_fillable(o, cfg)]
            live = {opp_key(o) for o in fillable}
            open_arb &= live
            signal = next((o for o in fillable if opp_key(o) not in open_arb), None)
            if signal is not None:
                filled = _fill_opp_at(signal, fill_quotes, cfg)
                if filled is not None:
                    notional = min(base_notional, equity * max_frac)
                    if notional >= 10:
                        pnl, fees = simulate_fill(filled, notional)
                        _apply(t, "arb", pnl, fees, 1)
                        trades.append(
                            BookTrade(
                                ts=t,
                                strategy="arb",
                                kind=signal.kind,
                                pair=signal.pair,
                                pnl=pnl,
                                fees_paid=fees,
                                notional=notional,
                                extra={
                                    "buy_venue": signal.buy_venue,
                                    "sell_venue": signal.sell_venue,
                                    "signal_net_bps": signal.net_bps,
                                    "fill_net_bps": filled.net_bps,
                                },
                            )
                        )
                        open_arb.add(opp_key(signal))

    _accrue_idle(last_mark, end)

    # flatten leftovers at window end so round-trip fees are not hidden
    last_ts = timeline[-1] if timeline else end
    for inst, st in fstates.items():
        if st.position is None:
            continue
        fee = cost_usd(st.position.notional, ow_bps)
        _apply(last_ts, "funding", -fee, fee, 1)
        trades.append(
            BookTrade(
                ts=last_ts,
                strategy="funding",
                kind="exit_eod",
                pair=inst,
                pnl=-fee,
                fees_paid=fee,
                notional=st.position.notional,
                extra={"side": st.position.side, "collected": st.position.collected},
            )
        )
        st.last_exit_ts = last_ts
        st.position = None
    if dpos:
        flatten_depeg(last_ts, last_px, "eod")
        notes.append("Open depeg-fade positions flattened at window end (exit fees charged).")
    if any(t.kind == "exit_eod" for t in trades if t.strategy == "funding"):
        notes.append("Open funding hedges flattened at window end (exit fees charged).")

    # daily rows for every calendar day in [start, end)
    days: list[BookDayRow] = []
    if start and end:
        cursor = start.date()
        last_day = (end - timedelta(seconds=1)).date()
        eq = float(balance)
        while cursor <= last_day:
            key = cursor.isoformat()
            if key not in day_start_eq:
                day_start_eq[key] = eq
                day_end[key] = eq
                day_eq_path[key] = [eq]
                day_funding[key] = 0.0
                day_depeg[key] = 0.0
                day_arb[key] = 0.0
                day_fees[key] = 0.0
                day_trades[key] = 0
                day_depegs[key] = 0
                day_fund_coll[key] = 0.0
                day_idle[key] = 0.0
            se = day_start_eq[key]
            ee = day_end.get(key, se)
            fp = day_funding.get(key, 0.0)
            dp = day_depeg.get(key, 0.0)
            ap = day_arb.get(key, 0.0)
            ip = day_idle.get(key, 0.0)
            days.append(
                BookDayRow(
                    date=key,
                    starting_equity=se,
                    funding_pnl=fp,
                    depeg_pnl=dp,
                    arb_pnl=ap,
                    total_pnl=fp + dp + ap + ip,
                    ending_equity=ee,
                    trades=day_trades.get(key, 0),
                    max_drawdown=_max_dd(day_eq_path.get(key, [se])),
                    fees_paid=day_fees.get(key, 0.0),
                    funding_collected=day_fund_coll.get(key, 0.0),
                    depeg_alerts=day_depegs.get(key, 0),
                    idle_pnl=ip,
                )
            )
            eq = ee
            cursor = cursor + timedelta(days=1)

    win_days = sum(1 for d in days if d.total_pnl > 0)
    lose_days = sum(1 for d in days if d.total_pnl < 0)
    n_wins = sum(1 for t in trades if t.pnl > 0)
    total_pnl = equity - balance
    fees_paid = sum(t.fees_paid for t in trades)
    funding_collected = sum(day_fund_coll.values())

    # honesty scale note
    fee_kind = "maker" if fcfg.use_maker else "taker"
    notes.append(
        "Honesty: entry gate is 3 bp / 8h; a 1 bp drip is ignored (sit flat). "
        f"At 3 bp / 8h on $1,000 notional the drip is about $0.90/day before fees; "
        f"this book sizes funding at {fcfg.equity_frac:.0%} of equity (sleeve cap {fcfg.sleeve_max:.0%}), "
        f"so $1,000 start → ~${balance * fcfg.equity_frac * fcfg.min_funding * 3:.2f}/day gross at a persistent 3 bp print. "
        f"Round-trip {fee_kind}+spread is ~{2 * ow_bps:.1f} bps. A 0-trade window at $0.00 (plus any labeled idle yield) "
        "is a success versus fee-churn."
    )

    return BookResult(
        start=start,
        end=end,
        starting_balance=balance,
        ending_equity=equity,
        total_return=(equity / balance - 1.0) if balance else 0.0,
        total_pnl=total_pnl,
        fees_paid=fees_paid,
        funding_collected=funding_collected,
        n_trades=len(trades),
        n_wins=n_wins,
        win_rate=(n_wins / len(trades)) if trades else 0.0,
        win_days=win_days,
        lose_days=lose_days,
        max_drawdown=max_dd,
        depeg_wins=depeg_wins,
        depeg_losses=depeg_losses,
        strategies=strats,
        idle_pnl=sum(day_idle.values()),
        idle_note=idle_note,
        days=days,
        trades=trades,
        depegs=depegs,
        skipped=skipped,
        notes=notes,
        universe=universe,
    )
