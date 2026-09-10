from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from stablebot.config import AppConfig
from stablebot.market.depeg import DepegAlert, find_depegs
from stablebot.market.spreads import SpreadOpportunity, find_opportunities
from stablebot.paper.engine import simulate_fill
from stablebot.strategy.stable_spread import is_fillable, opp_key
from stablebot.backtest.history import Bar, HistoryBook, bars_to_quotes


@dataclass
class BacktestTrade:
    signal_ts: datetime
    fill_ts: datetime
    kind: str
    pair: str
    buy_venue: str
    sell_venue: str
    signal_net_bps: float
    fill_net_bps: float
    notional: float
    pnl: float
    fees_paid: float
    equity_after: float


@dataclass
class DayRow:
    date: str
    starting_equity: float
    trades: int
    pnl: float
    fees_paid: float
    ending_equity: float
    max_drawdown: float
    depegs: int


@dataclass
class BacktestResult:
    start: datetime
    end: datetime
    starting_balance: float
    ending_equity: float
    total_return: float
    total_pnl: float
    fees_paid: float
    n_trades: int
    n_wins: int
    win_rate: float
    max_drawdown: float
    days: list[DayRow] = field(default_factory=list)
    trades: list[BacktestTrade] = field(default_factory=list)
    depegs: list[DepegAlert] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    x_note: str = "X overlay not applied historically"

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "starting_balance": self.starting_balance,
            "ending_equity": self.ending_equity,
            "total_return": self.total_return,
            "total_pnl": self.total_pnl,
            "fees_paid": self.fees_paid,
            "n_trades": self.n_trades,
            "n_wins": self.n_wins,
            "win_rate": self.win_rate,
            "max_drawdown": self.max_drawdown,
            "x_note": self.x_note,
            "skipped": self.skipped,
            "notes": self.notes,
            "days": [d.__dict__ for d in self.days],
            "trades": [
                {
                    "signal_ts": t.signal_ts.isoformat(),
                    "fill_ts": t.fill_ts.isoformat(),
                    "kind": t.kind,
                    "pair": t.pair,
                    "buy_venue": t.buy_venue,
                    "sell_venue": t.sell_venue,
                    "signal_net_bps": t.signal_net_bps,
                    "fill_net_bps": t.fill_net_bps,
                    "notional": t.notional,
                    "pnl": t.pnl,
                    "fees_paid": t.fees_paid,
                    "equity_after": t.equity_after,
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


def _max_dd(equity_path: list[float]) -> float:
    if not equity_path:
        return 0.0
    peak = equity_path[0]
    dd = 0.0
    for x in equity_path:
        peak = max(peak, x)
        if peak > 0:
            dd = max(dd, (peak - x) / peak)
    return dd


def _fill_opp_at(
    signal: SpreadOpportunity,
    next_quotes: list,
    cfg: AppConfig,
) -> SpreadOpportunity | None:
    """Reprice the same legs on the next bar (no lookahead)."""
    buy = None
    sell = None
    for q in next_quotes:
        if (
            q.venue == signal.buy_venue
            and q.base == signal.buy_base
            and q.quote == signal.buy_quote
        ):
            buy = q
        if (
            q.venue == signal.sell_venue
            and q.base == signal.sell_base
            and q.quote == signal.sell_quote
        ):
            sell = q
    if buy is None or sell is None or buy.ask is None or sell.bid is None:
        return None
    from stablebot.market.spreads import fee_drag_bps, gross_edge_bps, net_edge_bps

    bf, sf = cfg.fee_bps(buy.venue), cfg.fee_bps(sell.venue)
    net = net_edge_bps(buy.ask, sell.bid, bf, sf)
    return SpreadOpportunity(
        kind=signal.kind,
        pair=signal.pair,
        buy_venue=buy.venue,
        sell_venue=sell.venue,
        buy_base=buy.base,
        buy_quote=buy.quote,
        sell_base=sell.base,
        sell_quote=sell.quote,
        buy_px=buy.ask,
        sell_px=sell.bid,
        buy_fee_bps=bf,
        sell_fee_bps=sf,
        gross_bps=gross_edge_bps(buy.ask, sell.bid),
        fee_bps=fee_drag_bps(bf, sf),
        net_bps=net,
        ts=buy.ts,
    )


def run_backtest(
    cfg: AppConfig,
    book: HistoryBook,
    start: datetime,
    end: datetime,
    balance: float,
) -> BacktestResult:
    """Walk-forward hourly: signal on bar t close, fill at bar t+1 open."""
    by_t = book.by_time()
    times = sorted(t for t in by_t if start <= t < end)
    equity = float(balance)
    peak = equity
    max_dd = 0.0
    trades: list[BacktestTrade] = []
    depegs: list[DepegAlert] = []
    day_start_eq: dict[str, float] = {}
    day_eq_path: dict[str, list[float]] = {}
    day_pnl: dict[str, float] = {}
    day_fees: dict[str, float] = {}
    day_trades: dict[str, int] = {}
    day_depegs: dict[str, int] = {}
    day_end: dict[str, float] = {}

    notes = list(book.notes)
    notes.append("X overlay not applied historically (recent-search only; price-only backtest).")
    notes.append("Signals use bar-t close; fills use bar-t+1 open. No lookahead.")
    notes.append(
        "Paper fills are cross-venue same-pair only by default. "
        "Cross-pair discounts (e.g. TUSD vs USDC) are signals, not harvested every bar."
    )

    max_frac = cfg.backtest.max_fraction
    base_notional = min(cfg.strategy.paper_notional_usd, balance)
    open_keys: set[tuple] = set()

    for i, t in enumerate(times[:-1]):
        nxt = times[i + 1]
        # Only step one hour if the next stamp is the next hour (gaps = skip fill)
        if nxt - t > timedelta(hours=1, minutes=5):
            continue
        sig_quotes = bars_to_quotes(by_t[t], cfg, use_open=False)
        fill_quotes = bars_to_quotes(by_t[nxt], cfg, use_open=True)
        opps = find_opportunities(sig_quotes, cfg)
        alerts = find_depegs(sig_quotes, cfg)
        depegs.extend(alerts)
        day = t.date().isoformat()
        day_start_eq.setdefault(day, equity)
        day_eq_path.setdefault(day, [equity])
        day_pnl.setdefault(day, 0.0)
        day_fees.setdefault(day, 0.0)
        day_trades.setdefault(day, 0)
        day_depegs[day] = day_depegs.get(day, 0) + len(alerts)

        fillable = [o for o in opps if is_fillable(o, cfg)]
        live = {opp_key(o) for o in fillable}
        open_keys &= live
        signal = next((o for o in fillable if opp_key(o) not in open_keys), None)
        if signal is None:
            day_eq_path[day].append(equity)
            day_end[day] = equity
            continue
        filled = _fill_opp_at(signal, fill_quotes, cfg)
        if filled is None:
            day_eq_path[day].append(equity)
            day_end[day] = equity
            continue
        notional = min(base_notional, equity * max_frac)
        if notional < 10:
            day_eq_path[day].append(equity)
            day_end[day] = equity
            continue
        pnl, fees = simulate_fill(filled, notional)
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
        day_pnl[day] += pnl
        day_fees[day] += fees
        day_trades[day] += 1
        day_eq_path[day].append(equity)
        day_end[day] = equity
        trades.append(
            BacktestTrade(
                signal_ts=t,
                fill_ts=nxt,
                kind=signal.kind,
                pair=signal.pair,
                buy_venue=signal.buy_venue,
                sell_venue=signal.sell_venue,
                signal_net_bps=signal.net_bps,
                fill_net_bps=filled.net_bps,
                notional=notional,
                pnl=pnl,
                fees_paid=fees,
                equity_after=equity,
            )
        )
        open_keys.add(opp_key(signal))

    # close last day if the final bar had no next
    if times:
        last_day = times[-1].date().isoformat()
        day_start_eq.setdefault(last_day, equity)
        day_eq_path.setdefault(last_day, [equity])
        day_end.setdefault(last_day, equity)
        day_pnl.setdefault(last_day, 0.0)
        day_fees.setdefault(last_day, 0.0)
        day_trades.setdefault(last_day, 0)
        day_depegs.setdefault(last_day, 0)

    days: list[DayRow] = []
    for day in sorted(day_start_eq):
        path = day_eq_path.get(day, [day_start_eq[day]])
        days.append(
            DayRow(
                date=day,
                starting_equity=day_start_eq[day],
                trades=day_trades.get(day, 0),
                pnl=day_pnl.get(day, 0.0),
                fees_paid=day_fees.get(day, 0.0),
                ending_equity=day_end.get(day, path[-1]),
                max_drawdown=_max_dd(path),
                depegs=day_depegs.get(day, 0),
            )
        )

    n_wins = sum(1 for t in trades if t.pnl > 0)
    total_pnl = equity - balance
    return BacktestResult(
        start=start,
        end=end,
        starting_balance=balance,
        ending_equity=equity,
        total_return=(equity / balance - 1.0) if balance else 0.0,
        total_pnl=total_pnl,
        fees_paid=sum(t.fees_paid for t in trades),
        n_trades=len(trades),
        n_wins=n_wins,
        win_rate=(n_wins / len(trades)) if trades else 0.0,
        max_drawdown=max_dd,
        days=days,
        trades=trades,
        depegs=depegs,
        skipped=list(book.skipped),
        notes=notes,
    )


def daterange(days: int | None, start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    if start and end:
        return as_start(start), as_start(end)
    if days is None:
        days = 7
    end_dt = now
    start_dt = end_dt - timedelta(days=days)
    return start_dt, end_dt


def as_start(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
