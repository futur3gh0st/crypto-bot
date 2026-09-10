"""The launcher. Arrow keys or numbers, and it starts itself if you walk away."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from stablebot.config import AppConfig, EnvSettings, data_dir
from stablebot.desk.keys import KeyReader, raw_mode
from stablebot.desk.render import meter

BANNER = r"""
 ___ _        _    _     ___      _
/ __| |_ __ _| |__| |___| _ ) ___| |_
\__ \  _/ _` | '_ \ / -_) _ \/ _ \  _|
|___/\__\__,_|_.__/_\___|___/\___/\__|
"""

AUTOSTART_SECONDS = 20.0


@dataclass
class MenuItem:
    key: str
    title: str
    blurb: str
    action: str
    accent: str = "cyan"


ITEMS: list[MenuItem] = [
    MenuItem(
        "1",
        "Auto trading desk",
        "All sleeves, autopilot on. Allocates, sizes, benches and stops by itself.",
        "desk:all",
        "green",
    ),
    MenuItem(
        "2",
        "Desk — directional only",
        "Vol-aware spot-lag on both Kalshi 15m and Polymarket Up/Down.",
        "desk:lag",
    ),
    MenuItem(
        "3",
        "Desk — locks only",
        "Pair-complete on Polymarket and Kalshi. Locked edge, no direction.",
        "desk:locks",
    ),
    MenuItem(
        "4",
        "Signal study",
        "Score the fair-value model against real outcomes. Evidence, not vibes.",
        "study",
        "magenta",
    ),
    MenuItem(
        "5",
        "Backtest",
        "Walk-forward replay of the CEX book sleeves.",
        "backtest",
        "magenta",
    ),
    MenuItem(
        "6",
        "Scoreboard",
        "Ledger digest — what the paper pots have actually done.",
        "digest",
        "magenta",
    ),
    MenuItem(
        "7",
        "Live gate check",
        "Show which Polymarket live gates are open. Places no orders.",
        "livecheck",
        "yellow",
    ),
    MenuItem("q", "Quit", "", "quit", "red"),
]


def _status_line(cfg: AppConfig, settings: EnvSettings) -> RenderableType:
    from stablebot.desk.risk import halt_present

    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()

    live_bits = Text()
    if settings.poly_live:
        live_bits.append("POLY_LIVE=1", style="bold red")
    else:
        live_bits.append("POLY_LIVE=0", style="green")
    live_bits.append("  ")
    confirm = (data_dir() / "poly_live_confirm.txt").exists()
    live_bits.append(
        "confirm file present" if confirm else "no confirm file",
        style="yellow" if confirm else "dim",
    )
    live_bits.append("  ")
    live_bits.append(
        "HALTED" if halt_present() else "no halt",
        style="bold red" if halt_present() else "dim",
    )

    t.add_row("mode", Text("PAPER — nothing here can place an order", style="bold green"))
    t.add_row("live gates", live_bits)
    t.add_row("data", Text(str(data_dir()), style="dim"))
    t.add_row(
        "coins",
        Text(", ".join(cfg.poly.coins) + f"   windows {cfg.poly.windows}", style="dim"),
    )
    return t


def render_menu(selected: int, countdown: float | None, cfg: AppConfig, settings: EnvSettings) -> RenderableType:
    head = Text(BANNER, style="bold cyan")
    sub = Text("paper trading desk — stablecoin spreads, prediction-market locks, spot lag", style="dim")

    body = Table.grid(padding=(0, 1), expand=True)
    body.add_column(width=4, no_wrap=True)
    body.add_column(width=24, no_wrap=True)
    body.add_column(overflow="fold")

    for i, item in enumerate(ITEMS):
        picked = i == selected
        marker = Text("  ▶ " if picked else "    ", style="bold yellow")
        title = Text(
            f"[{item.key}] {item.title}",
            style=("bold " + item.accent) if picked else item.accent,
        )
        blurb = Text(item.blurb, style="white" if picked else "dim")
        body.add_row(marker, title, blurb)

    foot_lines: list[RenderableType] = [Text("")]
    if countdown is not None and countdown > 0:
        bar = Text("  starting the auto desk in ")
        bar.append(f"{countdown:.0f}s", style="bold yellow")
        bar.append("   ")
        bar.append_text(meter(1.0 - countdown / AUTOSTART_SECONDS, 24, "yellow"))
        foot_lines.append(bar)
        foot_lines.append(Text("  press any key to stop the countdown and choose", style="dim"))
    else:
        foot_lines.append(
            Text("  ↑/↓ or number to pick · enter to run · q to quit", style="dim")
        )

    return Panel(
        Group(
            Align.center(head),
            Align.center(sub),
            Text(""),
            _status_line(cfg, settings),
            Text(""),
            body,
            *foot_lines,
        ),
        border_style="blue",
        padding=(1, 2),
    )


async def run_menu(cfg: AppConfig, settings: EnvSettings, console: Console) -> str:
    """Draw the menu until something is picked. Returns an action string."""
    keys = KeyReader()
    selected = 0
    started = time.monotonic()
    autostart = keys.enabled  # only auto-run when a human could have stopped it
    chosen: str | None = None

    with raw_mode():
        keys.start()
        with Live(console=console, refresh_per_second=8, screen=True) as live:
            while chosen is None:
                countdown = None
                if autostart:
                    left = AUTOSTART_SECONDS - (time.monotonic() - started)
                    countdown = max(0.0, left)
                    if left <= 0:
                        chosen = "desk:all"
                        break
                live.update(render_menu(selected, countdown, cfg, settings))
                ch = await keys.get(timeout=0.12)
                if ch is None:
                    continue
                if autostart:
                    autostart = False          # a keypress means someone is here
                    continue
                if ch in {"\x1b"}:
                    # arrow keys arrive as ESC [ A/B
                    nxt = await keys.get(timeout=0.05)
                    nxt2 = await keys.get(timeout=0.05)
                    if nxt == "[" and nxt2 == "A":
                        selected = (selected - 1) % len(ITEMS)
                    elif nxt == "[" and nxt2 == "B":
                        selected = (selected + 1) % len(ITEMS)
                    continue
                if ch in {"\r", "\n"}:
                    chosen = ITEMS[selected].action
                    break
                if ch in {"k"}:
                    selected = (selected - 1) % len(ITEMS)
                    continue
                if ch in {"j"}:
                    selected = (selected + 1) % len(ITEMS)
                    continue
                if ch in {"\x03", "\x04"}:
                    chosen = "quit"
                    break
                for i, item in enumerate(ITEMS):
                    if ch.lower() == item.key:
                        selected = i
                        chosen = item.action
                        break
        keys.stop()

    if chosen is None:
        chosen = "quit"
    return chosen


def run_menu_sync(cfg: AppConfig, settings: EnvSettings, console: Console | None = None) -> str:
    console = console or Console()
    if not KeyReader().enabled:
        # Not a terminal (piped, CI, nohup): go straight to the thing that runs
        # itself rather than blocking forever on input nobody can give.
        console.print("[dim]no tty — starting the auto desk directly[/dim]")
        return "desk:all"
    return asyncio.run(run_menu(cfg, settings, console))
