"""`stablebot desk` — menu, then whatever the menu picked, then back to the menu."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from rich.console import Console

from stablebot.config import AppConfig, EnvSettings
from stablebot.desk.allocator import Allocator, AllocatorCfg
from stablebot.desk.app import DeskApp, build_default_sleeves
from stablebot.desk.risk import RiskGovernor

console = Console()

SLEEVE_SETS = {
    "desk:all": ("spot_lag", "kalshi_lag", "poly_lock", "kalshi_lock"),
    "desk:lag": ("spot_lag", "kalshi_lag"),
    "desk:spot_lag": ("spot_lag",),
    "desk:kalshi_lag": ("kalshi_lag",),
    "desk:locks": ("poly_lock", "kalshi_lock"),
}


async def run_desk(
    cfg: AppConfig,
    which: tuple[str, ...],
    pot: float | None,
    autopilot: bool,
    legacy: bool,
    daily_stop: float | None,
    max_dd: float,
    headless: bool = False,
    serve: str | None = None,
) -> None:
    sleeves = build_default_sleeves(cfg, which, balance=pot or 1000.0, legacy_spot_lag=legacy)
    app = DeskApp(
        cfg,
        sleeves,
        pot=pot,
        autopilot=autopilot,
        console=console,
        allocator=Allocator(AllocatorCfg(pot=pot or 0.0)) if pot else None,
        governor=RiskGovernor(cfg, daily_stop_pct=daily_stop, max_drawdown_pct=max_dd),
    )
    if headless:
        await app.run_headless(serve=serve)
    else:
        await app.run(serve=serve)
    app.print_summary()


def _project_root() -> Path:
    from stablebot.config import find_project_root

    return find_project_root()


def _run_script(name: str, *extra: str) -> None:
    script = _project_root() / "scripts" / name
    if not script.exists():
        console.print(f"[red]missing {script}[/red]")
        return
    subprocess.run([sys.executable, str(script), *extra], check=False)


def dispatch(action: str, cfg: AppConfig, settings: EnvSettings, args) -> bool:
    """Run one menu action. Returns True to show the menu again."""
    if action == "quit":
        return False

    if action in SLEEVE_SETS:
        asyncio.run(
            run_desk(
                cfg,
                SLEEVE_SETS[action],
                pot=args.pot,
                autopilot=not args.no_autopilot,
                legacy=args.legacy_signal,
                daily_stop=args.daily_stop,
                max_dd=args.max_drawdown,
                headless=getattr(args, "headless", False),
                serve=getattr(args, "serve", None),
            )
        )
        return not args.auto

    if action == "study":
        _run_script("vol_signal_study.py", "--window", "5", "--pulls", "4")
        console.input("\n[dim]enter to return to the menu[/dim] ")
        return True

    if action == "backtest":
        from stablebot.main import cmd_backtest

        asyncio.run(cmd_backtest(cfg, days=7, start=None, end=None, balance=args.pot))
        console.input("\n[dim]enter to return to the menu[/dim] ")
        return True

    if action == "digest":
        from stablebot.main import cmd_digest

        cmd_digest("hourly")
        console.input("\n[dim]enter to return to the menu[/dim] ")
        return True

    if action == "livecheck":
        from stablebot.poly.live import cmd_poly_live_check

        try:
            cmd_poly_live_check()
        except SystemExit as exc:
            console.print(f"[yellow]{exc}[/yellow]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
        console.input("\n[dim]enter to return to the menu[/dim] ")
        return True

    console.print(f"[yellow]unknown action {action}[/yellow]")
    return True


def cmd_desk(cfg: AppConfig, settings: EnvSettings, args) -> None:
    from stablebot.desk.menu import run_menu_sync

    if getattr(args, "remote", None):
        from stablebot.desk.viewer import run_viewer

        asyncio.run(run_viewer(args.remote, console))
        return

    if getattr(args, "headless", False):
        # A service manager has no tty, so there is no menu to show and nothing
        # to press a key on. Headless always means start now.
        args.auto = True

    if args.auto:
        action = f"desk:{args.sleeves}" if args.sleeves != "all" else "desk:all"
        if action not in SLEEVE_SETS:
            raise SystemExit(
                "--sleeves must be one of: "
                + ", ".join(sorted(k.split(":", 1)[1] for k in SLEEVE_SETS))
            )
        dispatch(action, cfg, settings, args)
        return

    again = True
    while again:
        action = run_menu_sync(cfg, settings, console)
        again = dispatch(action, cfg, settings, args)
    console.print("[dim]desk closed. Paper ledgers are in data/.[/dim]")
