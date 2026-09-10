"""Draw a desk that is running somewhere else.

The daemon on the VPS owns the sleeves and the money. This owns a screen: it
polls /state, rebuilds a DeskState from the snapshot and hands it to the same
renderer the local desk uses, so there is one dashboard implementation rather
than two that drift.

Read-only by design. Keys that would pause, halt or rebalance are not wired up,
because a viewer that could halt a desk over an unauthenticated loopback socket
is a foot-gun, and the operator can always reach the daemon over SSH.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
from rich.console import Console
from rich.live import Live
from rich.text import Text

from stablebot.desk import render
from stablebot.desk.server import TOKEN_ENV
from stablebot.desk.state import DeskState
from stablebot.desk.wire import state_from_dict

POLL_SECONDS = 1.0
REFRESH_HZ = 4.0


def _status(url: str, note: str, style: str) -> Text:
    t = Text()
    t.append("  remote desk  ", style="bold")
    t.append(url, style="cyan")
    t.append("\n\n  ")
    t.append(note, style=style)
    t.append("\n\n  ctrl-c to leave. This view is read-only.", style="dim")
    return t


async def run_viewer(url: str, console: Console | None = None) -> None:
    console = console or Console()
    base = url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    headers = {}
    token = os.environ.get(TOKEN_ENV)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    state: DeskState | None = None
    note, style = "connecting…", "yellow"

    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), headers=headers) as http:
        with Live(
            _status(base, note, style),
            console=console,
            refresh_per_second=REFRESH_HZ,
            screen=True,
            transient=False,
        ) as live:
            try:
                while True:
                    try:
                        r = await http.get(f"{base}/state")
                        if r.status_code == 401:
                            note, style = (
                                f"401 — set {TOKEN_ENV} to the daemon's token",
                                "bold red",
                            )
                        else:
                            r.raise_for_status()
                            state = state_from_dict(r.json())
                            note, style = "", ""
                    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                        note = f"{type(exc).__name__}: {exc}"
                        style = "bold red"

                    if state is not None and not note:
                        live.update(render.build(state, console.size.height or 40))
                    elif state is not None:
                        # Keep the last good screen up, but say it is stale.
                        live.update(render.build(state, console.size.height or 40))
                    else:
                        live.update(_status(base, note, style))
                    await asyncio.sleep(POLL_SECONDS)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
    if note:
        console.print(f"[dim]last from {base}: {note}[/dim]")
