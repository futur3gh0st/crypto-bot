"""poly-scan / poly-run: live table + paper fills. No live Polymarket orders."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from stablebot.config import AppConfig, data_dir
from stablebot.poly.client import scan_windows
from stablebot.poly.markets import ScanRow, parse_coins, parse_minutes_list
from stablebot.poly.paper import PolyLedger, PolyPaper

console = Console(width=200)

FEE_ZERO_LABEL = "fee assumed 0 — check live schedule"


def fee_note(taker_fee_bps: float) -> str:
    if taker_fee_bps == 0:
        return (
            f"{FEE_ZERO_LABEL} (official crypto taker is C x 0.07 x p x (1-p) "
            "as of 2026-08 docs; not applied; no rebate invented)"
        )
    return (
        f"flat taker_fee_bps={taker_fee_bps:g} (pair-level haircut). "
        "Official crypto curve C x 0.07 x p x (1-p) not applied; no rebate invented."
    )


def _fmt(px: float | None, digits: int = 3) -> str:
    if px is None:
        return "—"
    return f"{px:.{digits}f}"


def _crossed(r: ScanRow) -> bool:
    if r.up_bid is not None and r.up_ask is not None and r.up_bid > r.up_ask:
        return True
    if r.down_bid is not None and r.down_ask is not None and r.down_bid > r.down_ask:
        return True
    return False


def print_scan_table(rows: list[ScanRow], taker_fee_bps: float) -> None:
    console.rule("[bold]polymarket crypto Up/Down (paper)")
    console.print(f"[dim]{fee_note(taker_fee_bps)}[/dim]")
    table = Table(title="current + next window", expand=False)
    table.add_column("coin", no_wrap=True)
    table.add_column("tf", no_wrap=True)
    table.add_column("win", no_wrap=True)
    table.add_column("slug", no_wrap=True)
    table.add_column("left m", justify="right", no_wrap=True)
    table.add_column("Up bid/ask", justify="right", no_wrap=True)
    table.add_column("Dn bid/ask", justify="right", no_wrap=True)
    table.add_column("sum_asks", justify="right", no_wrap=True)
    table.add_column("lock_edge", justify="right", no_wrap=True)
    table.add_column("fair_up", justify="right", no_wrap=True)
    table.add_column("disloc_bps", justify="right", no_wrap=True)
    locks = 0
    for r in rows:
        lock = r.lock_edge
        style = "bold green" if lock is not None and lock > 0 else ""
        if lock is not None and lock > 0:
            locks += 1
        table.add_row(
            r.coin,
            f"{r.minutes}m",
            r.which,
            r.slug,
            f"{r.minutes_left:.1f}",
            f"{_fmt(r.up_bid)}/{_fmt(r.up_ask)}",
            f"{_fmt(r.down_bid)}/{_fmt(r.down_ask)}",
            _fmt(r.sum_asks, 3),
            _fmt(lock, 4),
            _fmt(r.fair_up, 3),
            _fmt(r.dislocation_bps, 1),
            style=style,
        )
        if r.error:
            console.print(f"[red]{r.slug}[/red] {r.error}")
    console.print(table)
    console.print("[dim]exact rows:[/dim]")
    for r in rows:
        console.print(
            f"  {r.slug}  {r.which}  left={r.minutes_left:.2f}m  "
            f"Up {_fmt(r.up_bid)}/{_fmt(r.up_ask)}  "
            f"Dn {_fmt(r.down_bid)}/{_fmt(r.down_ask)}  "
            f"sum_asks={_fmt(r.sum_asks, 4)}  lock_edge={_fmt(r.lock_edge, 4)}  "
            f"fair_up={_fmt(r.fair_up, 4)}  disloc_bps={_fmt(r.dislocation_bps, 1)}  "
            f"spot={_fmt(r.spot, 4)}  open={_fmt(r.open_px, 4)}"
            f"{('  CROSSED' if _crossed(r) else '')}"
        )
    if locks == 0:
        console.print("[yellow]no lock[/yellow]  (no row with lock_edge > 0)")
    else:
        console.print(f"[green]{locks} row(s) with lock_edge > 0[/green]")
    console.print(
        "[dim]Paper only. Live books on 2026-08-14 ~22:51Z were sum_asks=1.01 "
        "(no lock). Tweet $81k PnL is unverified. Crude fair is spot vs window-open, "
        "not a priced vol model.[/dim]"
    )


async def run_scan(
    cfg: AppConfig,
    coins: list[str] | None = None,
    windows: list[int] | None = None,
    now_ts: float | None = None,
) -> list[ScanRow]:
    poly = cfg.poly
    coins = coins or list(poly.coins)
    windows = windows or list(poly.windows)
    now_ts = time.time() if now_ts is None else now_ts
    return await scan_windows(
        coins,
        windows,
        now_ts,
        taker_fee_bps=poly.taker_fee_bps,
        fair_scale=poly.fair_scale,
    )


async def cmd_poly_scan(
    cfg: AppConfig,
    coins_raw: str | None,
    windows_raw: str | None,
) -> None:
    coins = parse_coins(coins_raw, cfg.poly.coins)
    windows = parse_minutes_list(windows_raw, cfg.poly.windows)
    rows = await run_scan(cfg, coins, windows)
    print_scan_table(rows, cfg.poly.taker_fee_bps)


async def cmd_poly_run(
    cfg: AppConfig,
    interval: int,
    fade: bool,
    coins_raw: str | None = None,
    windows_raw: str | None = None,
    live: bool = False,
    live_dry_run: bool = False,
) -> None:
    if fade and (live or live_dry_run):
        raise SystemExit("--fade is forbidden with --live / --live-dry-run")
    if live and live_dry_run:
        raise SystemExit("--live and --live-dry-run are mutually exclusive")
    coins = parse_coins(coins_raw, cfg.poly.coins)
    windows = parse_minutes_list(windows_raw, cfg.poly.windows)
    ledger = PolyLedger()
    live_mode = live or live_dry_run
    if live_mode:
        from stablebot.poly.live import PolyLive, ClobLive, require_live_ready

        require_live_ready(cli_live=True, fade=fade)
        engine = PolyLive(cfg.poly, ledger, ClobLive(), dry_run=live_dry_run)
        label = "LIVE-DRY-RUN (no POST)" if live_dry_run else "LIVE"
        console.print(
            f"poly {label} loop every {interval}s  fade=off  "
            f"ledger={ledger.path}  data={data_dir()}"
        )
        console.print(
            "[red]Live path selected. Halt file data/poly_halt stops sending. "
            "Fade is off.[/red]"
        )
    else:
        engine = PolyPaper(cfg.poly, ledger, fade=fade)
        console.print(
            f"poly paper loop every {interval}s  fade={'on' if fade else 'off'}  "
            f"ledger={ledger.path}  data={data_dir()}"
        )
        console.print("[yellow]No live Polymarket orders. Paper fills only.[/yellow]")
    console.print(f"[dim]{fee_note(cfg.poly.taker_fee_bps)}[/dim]")
    while True:
        rows = await run_scan(cfg, coins, windows)
        print_scan_table(rows, cfg.poly.taker_fee_bps)
        if live_mode:
            from stablebot.poly.live import halt_present

            if halt_present():
                console.print(
                    "[red]halt file present — scan only, no live orders[/red]"
                )
                fills = []
            else:
                fills = engine.step(rows, datetime.now(timezone.utc))
        else:
            fills = engine.step(rows, datetime.now(timezone.utc))
        shown = 0
        for f in fills:
            if f.skipped and f.kind == "pair_complete" and "already" in f.reason:
                continue
            flag = "SKIP" if f.skipped else "FILL"
            console.print(
                f"{flag} {f.kind} {f.slug} shares={f.shares:g} "
                f"pnl={f.pnl:+.4f} {f.reason}"
            )
            shown += 1
        if shown == 0:
            console.print(
                f"{datetime.now(timezone.utc).isoformat()}  "
                f"rows={len(rows)} paper fills=0"
            )
        await asyncio.sleep(max(5, interval))
