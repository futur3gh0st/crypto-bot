from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from rich.console import Console

from stablebot.config import AppConfig, EnvSettings, data_dir, env_settings, load_config
from stablebot.exchanges.base import new_client
from stablebot.paper.engine import PaperEngine
from stablebot.paper.ledger import Ledger
from stablebot.report.digest import print_digest, print_scan
from stablebot.signals.trends import annotate, rollup
from stablebot.signals.x_client import from_env as x_from_env
from stablebot.strategy.risk import decide
from stablebot.strategy.stable_spread import run_scan

console = Console()


def _check_live(settings: EnvSettings) -> None:
    if settings.live:
        console.print(
            "[red]LIVE=1 is set, but this version is paper-only. "
            "No exchange orders will be placed.[/red]"
        )


async def cmd_scan(cfg: AppConfig, settings: EnvSettings) -> None:
    _check_live(settings)
    result = await run_scan(cfg)
    x = x_from_env(cfg, settings)
    note = x.disabled_message()
    trend = None
    if x.enabled:
        try:
            async with new_client() as http:
                posts = annotate(await x.recent_search(http))
            trend = rollup(posts, "hourly")
        except Exception as exc:  # noqa: BLE001
            note = f"X overlay failed: {exc}"
    from stablebot.report.digest import print_book_scan
    from stablebot.strategy.live_book import LiveBookSignals, collect_live_signals

    print_scan(result, x_note=note, trend=trend)
    try:
        live = await collect_live_signals(cfg, result.quotes)
    except Exception as exc:  # noqa: BLE001
        live = LiveBookSignals(notes=[f"live book signals failed: {exc}"])
    print_book_scan(live, trend=trend, fear_cut=cfg.risk.fear_cut_threshold)


async def cmd_run(cfg: AppConfig, settings: EnvSettings, interval: int) -> None:
    _check_live(settings)
    ledger = Ledger()
    engine = PaperEngine(cfg, ledger)
    x = x_from_env(cfg, settings)
    note = x.disabled_message()
    if note:
        console.print(f"[yellow]{note}[/yellow]")
        ledger.note("x_disabled", "1")
    console.print(f"paper loop every {interval}s  data={data_dir()}")
    while True:
        result = await run_scan(cfg)
        ledger.record_quotes(result.quotes)
        ledger.record_spreads(result.opportunities)
        ledger.record_depegs(result.depegs)
        trend = None
        if x.enabled:
            try:
                async with new_client() as http:
                    posts = annotate(await x.recent_search(http))
                ledger.record_x_posts(posts)
                trend = rollup(posts, "hourly")
            except Exception as exc:  # noqa: BLE001
                console.print(f"[yellow]X overlay failed: {exc}[/yellow]")
        decision = decide(cfg, trend)
        if result.errors:
            for e in result.errors:
                console.print(f"[red]{e}[/red]")
        if not result.opportunities:
            console.print(
                f"{datetime.now(timezone.utc).isoformat()}  "
                f"quotes={len(result.quotes)} opps=0 depegs={len(result.depegs)} ({decision.reason})"
            )
        engine.sync_open(result.opportunities)
        for opp in result.opportunities[:3]:
            fill = engine.maybe_fill(opp, decision)
            flag = "SKIP" if fill.skipped else "FILL"
            console.print(
                f"{flag} {opp.kind} {opp.pair} net={opp.net_bps:.2f}bps "
                f"pnl={fill.pnl:+.4f} {fill.reason}"
            )
        from stablebot.paper.book import BookPaperEngine
        from stablebot.strategy.live_book import collect_live_signals

        try:
            live = await collect_live_signals(cfg, result.quotes)
            book_eng = BookPaperEngine(cfg, ledger)
            for bf in book_eng.step(live, decision):
                flag = "SKIP" if bf.skipped else "FILL"
                console.print(
                    f"{flag} {bf.strategy} {bf.pair} notional={bf.notional:.2f} "
                    f"pnl={bf.pnl:+.4f} {bf.reason}"
                )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]book live step failed: {exc}[/yellow]")
        await asyncio.sleep(max(5, interval))


def cmd_digest(window: str) -> None:
    print_digest(Ledger(), window)


async def cmd_backtest(
    cfg: AppConfig,
    days: int | None,
    start: datetime | None,
    end: datetime | None,
    balance: float,
    fixture: bool = False,
    strategies: str | None = None,
) -> None:
    from datetime import timedelta

    from stablebot.backtest.book import parse_strategies, run_book_backtest
    from stablebot.backtest.engine import daterange
    from stablebot.backtest.funding_hist import FundingBook, fetch_funding_book
    from stablebot.backtest.history import default_fixture_path, fetch_history, load_fixture
    from stablebot.backtest.report import print_book, save_book

    strats = parse_strategies(strategies)
    start_dt, end_dt = daterange(days, start, end)
    fetch_start = start_dt - timedelta(days=3)
    book = None
    if fixture:
        book = load_fixture(default_fixture_path())
        console.print("[yellow]using bundled fixture (no network)[/yellow]")
    else:
        try:
            book = await fetch_history(cfg, fetch_start, end_dt)
            if not book.bars:
                raise RuntimeError("no historical bars returned")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]history fetch failed ({exc}); falling back to fixture[/yellow]")
            book = load_fixture(default_fixture_path())
            book.notes.append(f"network fallback: {exc}")
    funding = FundingBook(notes=["funding not fetched (fixture or disabled)"])
    if "funding" in strats and not fixture:
        try:
            funding = await fetch_funding_book(cfg, start_dt, end_dt)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]funding fetch failed ({exc}); funding sleeve skipped[/yellow]")
            funding.notes.append(f"funding fetch failed: {exc}")
            funding.skipped.append(str(exc))
    idle = None
    if cfg.idle_yield.enabled:
        try:
            from stablebot.market.idle_yield import fetch_idle_yield

            idle = await fetch_idle_yield()
            if idle is None:
                console.print("[yellow]idle yield: no live public USDC APY; unallocated cash earns 0[/yellow]")
            else:
                console.print(f"[dim]{idle.label()}[/dim]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]idle yield fetch failed ({exc}); unallocated cash earns 0[/yellow]")
            idle = None
    result = run_book_backtest(cfg, book, funding, start_dt, end_dt, balance, strats, idle_yield=idle)
    if not result.days and book.bars:
        times = sorted(b.open_time for b in book.bars)
        result = run_book_backtest(
            cfg, book, funding, times[0], times[-1] + timedelta(hours=1), balance, strats, idle_yield=idle
        )
        result.notes.append("requested window had no bars; replayed fixture timestamps instead")
    print_book(result)
    save_book(result)
