from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from stablebot.backtest.engine import BacktestResult
from stablebot.config import data_dir

console = Console()


def print_backtest(result: BacktestResult) -> None:
    span_days = max(1, (result.end - result.start).total_seconds() / 86400)
    console.rule(
        f"[bold]backtest[/bold]  {result.start.date()} → {result.end.date()}  "
        f"({span_days:.0f}d)  start ${result.starting_balance:,.2f}"
    )
    console.print(f"[yellow]{result.x_note}[/yellow]")
    for n in result.notes:
        console.print(f"[dim]{n}[/dim]")
    if result.skipped:
        console.print(f"[yellow]skipped {len(result.skipped)} pair/venue fetches[/yellow]")
        for s in result.skipped[:12]:
            console.print(f"  [dim]- {s}[/dim]")
        if len(result.skipped) > 12:
            console.print(f"  [dim]… {len(result.skipped) - 12} more[/dim]")

    daily = Table(title="Day-by-day")
    daily.add_column("date", no_wrap=True, min_width=10)
    daily.add_column("start eq", justify="right")
    daily.add_column("trades", justify="right")
    daily.add_column("pnl", justify="right")
    daily.add_column("fees", justify="right")
    daily.add_column("end eq", justify="right")
    daily.add_column("day max DD", justify="right")
    daily.add_column("depegs", justify="right")
    if not result.days:
        daily.add_row("—", "—", "0", "—", "—", "—", "—", "—")
    for d in result.days:
        daily.add_row(
            d.date,
            f"{d.starting_equity:,.2f}",
            str(d.trades),
            f"{d.pnl:+,.4f}",
            f"{d.fees_paid:,.4f}",
            f"{d.ending_equity:,.2f}",
            f"{d.max_drawdown*100:.2f}%",
            str(d.depegs),
        )
    console.print(daily)

    sm = Table(title="Summary")
    sm.add_column("metric")
    sm.add_column("value", justify="right")
    sm.add_row("starting balance", f"${result.starting_balance:,.2f}")
    sm.add_row("ending equity", f"${result.ending_equity:,.2f}")
    sm.add_row("total PnL", f"${result.total_pnl:+,.4f}")
    sm.add_row("total return", f"{result.total_return*100:+.3f}%")
    sm.add_row("trades", str(result.n_trades))
    sm.add_row("wins", str(result.n_wins))
    sm.add_row("win rate", f"{result.win_rate*100:.1f}%")
    sm.add_row("fees paid", f"${result.fees_paid:,.4f}")
    sm.add_row("max drawdown", f"{result.max_drawdown*100:.2f}%")
    sm.add_row("depeg alerts", str(len(result.depegs)))
    console.print(sm)
    console.print(
        "[dim]Paper-only. Fees + 1bp assumed half-spread on OHLC. "
        "Retail stablecoin arb is usually fee-negative; a flat/negative result is the honest base case.[/dim]"
    )


def save_backtest(result: BacktestResult) -> tuple[Path, Path]:
    out_dir = data_dir() / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"{result.start.date()}_{result.end.date()}_{int(result.starting_balance)}_{stamp}"
    json_path = out_dir / f"bt_{tag}.json"
    csv_path = out_dir / f"bt_{tag}_daily.csv"
    json_path.write_text(json.dumps(result.to_dict(), indent=2))
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "date", "starting_equity", "trades", "pnl", "fees_paid",
                "ending_equity", "max_drawdown", "depegs",
            ]
        )
        for d in result.days:
            w.writerow(
                [
                    d.date, f"{d.starting_equity:.6f}", d.trades, f"{d.pnl:.6f}",
                    f"{d.fees_paid:.6f}", f"{d.ending_equity:.6f}",
                    f"{d.max_drawdown:.6f}", d.depegs,
                ]
            )
    console.print(f"wrote {json_path}")
    console.print(f"wrote {csv_path}")
    return json_path, csv_path


def print_book(result) -> None:
    from stablebot.backtest.book import BookResult

    assert isinstance(result, BookResult)
    span_days = max(1, (result.end - result.start).total_seconds() / 86400)
    console.rule(
        f"[bold]book backtest[/bold]  {result.start.date()} → {result.end.date()}  "
        f"({span_days:.0f}d)  start ${result.starting_balance:,.2f}  "
        f"strats={','.join(result.strategies)}"
    )
    console.print(f"[yellow]{result.x_note}[/yellow]")
    if result.universe:
        console.print(f"funding universe: {', '.join(result.universe)}")
    for n in result.notes:
        console.print(f"[dim]{n}[/dim]")
    if result.skipped:
        console.print(f"[yellow]skipped {len(result.skipped)} fetches/series[/yellow]")
        for s in result.skipped[:15]:
            console.print(f"  [dim]- {s}[/dim]")
        if len(result.skipped) > 15:
            console.print(f"  [dim]… {len(result.skipped) - 15} more[/dim]")

    daily = Table(title="Day-by-day")
    daily.add_column("date", no_wrap=True, min_width=10)
    daily.add_column("start eq", justify="right")
    daily.add_column("funding", justify="right")
    daily.add_column("depeg", justify="right")
    daily.add_column("arb", justify="right")
    daily.add_column("idle", justify="right")
    daily.add_column("total pnl", justify="right")
    daily.add_column("end eq", justify="right")
    daily.add_column("trades", justify="right")
    daily.add_column("day max DD", justify="right")
    if not result.days:
        daily.add_row("—", "—", "—", "—", "—", "—", "—", "—", "0", "—")
    for d in result.days:
        daily.add_row(
            d.date,
            f"{d.starting_equity:,.2f}",
            f"{d.funding_pnl:+,.4f}",
            f"{d.depeg_pnl:+,.4f}",
            f"{d.arb_pnl:+,.4f}",
            f"{getattr(d, 'idle_pnl', 0.0):+,.4f}",
            f"{d.total_pnl:+,.4f}",
            f"{d.ending_equity:,.2f}",
            str(d.trades),
            f"{d.max_drawdown*100:.2f}%",
        )
    console.print(daily)

    sm = Table(title="Month / window summary")
    sm.add_column("metric")
    sm.add_column("value", justify="right")
    sm.add_row("starting balance", f"${result.starting_balance:,.2f}")
    sm.add_row("ending equity", f"${result.ending_equity:,.2f}")
    sm.add_row("total PnL", f"${result.total_pnl:+,.4f}")
    sm.add_row("total return", f"{result.total_return*100:+.3f}%")
    sm.add_row("win days", str(result.win_days))
    sm.add_row("lose days", str(result.lose_days))
    sm.add_row("trades", str(result.n_trades))
    sm.add_row("wins (trades)", str(result.n_wins))
    sm.add_row("win rate (trades)", f"{result.win_rate*100:.1f}%")
    sm.add_row("fees paid", f"${result.fees_paid:,.4f}")
    sm.add_row("funding collected", f"${result.funding_collected:+,.4f}")
    sm.add_row("idle yield", f"${getattr(result, 'idle_pnl', 0.0):+,.4f}")
    sm.add_row("depeg wins / losses", f"{result.depeg_wins} / {result.depeg_losses}")
    sm.add_row("max drawdown", f"{result.max_drawdown*100:.2f}%")
    sm.add_row("depeg alerts (scan)", str(len(result.depegs)))
    console.print(sm)
    console.print(
        "[dim]Paper-only. Funding is OKX public history (Binance USD-M blocked here). "
        "Basis approximated as 0. Depeg-fade can lose — the stop exists because of Terra-style blowups. "
        "Closed cross-venue arb is usually flat after retail taker fees.[/dim]"
    )


def save_book(result) -> tuple[Path, Path, Path]:
    from stablebot.backtest.chart import save_book_chart

    out_dir = data_dir() / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"{result.start.date()}_{result.end.date()}_{int(result.starting_balance)}_{stamp}"
    json_path = out_dir / f"book_{tag}.json"
    csv_path = out_dir / f"book_{tag}_daily.csv"
    png_path = out_dir / f"book_{tag}.png"
    json_path.write_text(json.dumps(result.to_dict(), indent=2))
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "date", "starting_equity", "funding_pnl", "depeg_pnl", "arb_pnl",
                "idle_pnl", "total_pnl", "ending_equity", "trades", "max_drawdown",
                "fees_paid", "funding_collected", "depeg_alerts",
            ]
        )
        for d in result.days:
            w.writerow(
                [
                    d.date, f"{d.starting_equity:.6f}", f"{d.funding_pnl:.6f}",
                    f"{d.depeg_pnl:.6f}", f"{d.arb_pnl:.6f}",
                    f"{getattr(d, 'idle_pnl', 0.0):.6f}", f"{d.total_pnl:.6f}",
                    f"{d.ending_equity:.6f}", d.trades, f"{d.max_drawdown:.6f}",
                    f"{d.fees_paid:.6f}", f"{d.funding_collected:.6f}", d.depeg_alerts,
                ]
            )
    try:
        save_book_chart(result, png_path)
        console.print(f"wrote {png_path}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]chart failed: {exc}[/yellow]")
        png_path = Path()
    console.print(f"wrote {json_path}")
    console.print(f"wrote {csv_path}")
    return json_path, csv_path, png_path
