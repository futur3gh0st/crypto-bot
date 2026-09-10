"""kalshi-scan / kalshi-run: live table + paper fills. No live Kalshi orders. No fade."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
from rich.console import Console
from rich.table import Table

from stablebot.config import AppConfig, data_dir
from stablebot.exchanges.base import USER_AGENT
from stablebot.kalshi.client import KalshiClient, ScanRow, parse_series, scan_series
from stablebot.kalshi.paper import KalshiLedger, KalshiPaper
from stablebot.kalshi.session import recompute_shared_session

console = Console(width=200)

FEE_NOTE = (
    "paper fee = 0.07*p*(1-p) per side (unrounded per-contract, like poly). "
    "Official July 2026 Kalshi schedule is round_up(0.07*C*P*(1-P)); "
    "fee is applied before accepting a lock. No rebate. No live orders."
)


def _fmt(px: float | None, digits: int = 3) -> str:
    if px is None:
        return "—"
    return f"{px:.{digits}f}"


def print_scan_table(rows: list[ScanRow], drop_notes: list[str] | None = None) -> None:
    console.rule("[bold]kalshi 15m YES/NO (paper pair-complete)")
    console.print(f"[dim]{FEE_NOTE}[/dim]")
    table = Table(title="open 15m books (asks from opposite bids)", expand=False)
    table.add_column("series", no_wrap=True)
    table.add_column("ticker", no_wrap=True)
    table.add_column("left m", justify="right", no_wrap=True)
    table.add_column("YES bid/ask", justify="right", no_wrap=True)
    table.add_column("NO bid/ask", justify="right", no_wrap=True)
    table.add_column("sum_asks", justify="right", no_wrap=True)
    table.add_column("curve_fee", justify="right", no_wrap=True)
    table.add_column("lock_edge", justify="right", no_wrap=True)
    locks = 0
    for r in rows:
        lock = r.lock_edge
        style = "bold green" if lock is not None and lock > 0 else ""
        if lock is not None and lock > 0:
            locks += 1
        left = "—" if r.minutes_left is None else f"{r.minutes_left:.1f}"
        table.add_row(
            r.series,
            r.ticker or "—",
            left,
            f"{_fmt(r.yes_bid)}/{_fmt(r.yes_ask)}",
            f"{_fmt(r.no_bid)}/{_fmt(r.no_ask)}",
            _fmt(r.sum_asks, 3),
            _fmt(r.curve_fee, 4),
            _fmt(lock, 4),
            style=style,
        )
        if r.error:
            console.print(f"[red]{r.series} {r.ticker}[/red] {r.error}")
    console.print(table)
    console.print("[dim]exact rows:[/dim]")
    for r in rows:
        console.print(
            f"  {r.series}  {r.ticker}  left={_fmt(r.minutes_left, 2)}m  "
            f"YES {_fmt(r.yes_bid)}/{_fmt(r.yes_ask)}  "
            f"NO {_fmt(r.no_bid)}/{_fmt(r.no_ask)}  "
            f"sum_asks={_fmt(r.sum_asks, 4)}  curve={_fmt(r.curve_fee, 4)}  "
            f"lock_edge={_fmt(r.lock_edge, 4)}"
        )
    if drop_notes:
        for note in drop_notes:
            console.print(f"[dim]{note}[/dim]")
    if not rows:
        console.print("[yellow]no open markets this pass (weekend metals/index skip quietly)[/yellow]")
    elif locks == 0:
        console.print("[yellow]no lock[/yellow]  (no row with lock_edge > 0 after fee)")
    else:
        console.print(f"[green]{locks} row(s) with lock_edge > 0 after fee[/green]")
    console.print(
        "[dim]Paper only. Shared $1000 pool with Polymarket (data/poly_session.json). "
        "Both YES and NO assumed filled at the derived ask. Hold to settlement. "
        "Kalshi list yes_ask/no_ask are unused — orderbook bids only.[/dim]"
    )


async def run_scan(
    cfg: AppConfig,
    series: list[str] | None = None,
    client: KalshiClient | None = None,
) -> list[ScanRow]:
    k = cfg.kalshi
    series = series or [s.upper() for s in k.series]
    return await scan_series(
        series,
        apply_curve_fee=k.apply_curve_fee,
        client=client,
        throttle_ms=k.throttle_ms,
    )


async def cmd_kalshi_scan(cfg: AppConfig, series_raw: str | None) -> None:
    series = parse_series(series_raw, cfg.kalshi.series)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(12.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    ) as http:
        client = KalshiClient(http, throttle_ms=cfg.kalshi.throttle_ms)
        rows = await run_scan(cfg, series, client=client)
        print_scan_table(rows, client.drop_notes)


async def cmd_kalshi_run(cfg: AppConfig, interval: int, series_raw: str | None = None) -> None:
    series = parse_series(series_raw, cfg.kalshi.series)
    ledger = KalshiLedger()
    engine = KalshiPaper(cfg.kalshi, ledger, update_session=True)
    console.print(
        f"kalshi paper loop every {interval}s  fade=off  live=false  "
        f"ledger={ledger.path}  data={data_dir()}"
    )
    console.print("[yellow]No live Kalshi orders. Paper fills only. Shared $1000 pool.[/yellow]")
    console.print(f"[dim]{FEE_NOTE}[/dim]")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(12.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    ) as http:
        client = KalshiClient(http, throttle_ms=cfg.kalshi.throttle_ms)
        while True:
            rows = await run_scan(cfg, series, client=client)
            print_scan_table(rows, client.drop_notes)
            fills = engine.step(rows, datetime.now(timezone.utc))
            recompute_shared_session()
            shown = 0
            for f in fills:
                if f.skipped and f.kind == "pair_complete" and "already" in f.reason:
                    continue
                flag = "SKIP" if f.skipped else "FILL"
                console.print(
                    f"{flag} {f.kind} {f.ticker} shares={f.shares:g} "
                    f"pnl={f.pnl:+.4f} {f.reason}"
                )
                shown += 1
            if shown == 0:
                console.print(
                    f"{datetime.now(timezone.utc).isoformat()}  "
                    f"rows={len(rows)} paper fills=0"
                )
            await asyncio.sleep(max(5, interval))
