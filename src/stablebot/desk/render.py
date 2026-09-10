"""The trading desk view. Dense, single screen, refreshes in place."""

from __future__ import annotations


from rich.align import Align
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from stablebot.desk.state import DeskState, SleeveStat

SPARK = "▁▂▃▄▅▆▇█"
BAR_FULL = "█"
BAR_EMPTY = "░"

STATE_STYLE = {
    "ARMED": "bold green",
    "WATCH": "yellow",
    "HELD": "cyan",
    "COLD": "dim",
    "ERR": "red",
}

STATUS_STYLE = {
    "armed": "bold green",
    "scanning": "cyan",
    "idle": "dim",
    "cooling": "yellow",
    "error": "bold red",
    "off": "dim red",
}

RISK_STYLE = {
    "RUNNING": "bold green",
    "THROTTLED": "bold yellow",
    "HALTED": "bold red",
    "PAUSED": "bold magenta",
}


def money(v: float, width: int = 0) -> str:
    s = f"${v:,.2f}"
    return s.rjust(width) if width else s


def signed(v: float, digits: int = 2, pct: bool = False) -> Text:
    style = "green" if v > 0 else ("red" if v < 0 else "dim")
    body = f"{v*100:+.{digits}f}%" if pct else f"{v:+,.{digits}f}"
    return Text(body, style=style)


def sparkline(values: list[float], width: int = 24) -> str:
    if not values:
        return "—"
    vals = values[-width:]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-12:
        return SPARK[3] * len(vals)
    span = hi - lo
    return "".join(SPARK[min(7, int((v - lo) / span * 7.999))] for v in vals)


def meter(fraction: float, width: int = 14, style: str = "cyan") -> Text:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    t = Text()
    t.append(BAR_FULL * filled, style=style)
    t.append(BAR_EMPTY * (width - filled), style="dim")
    return t


def _dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _mins(v: float | None) -> str:
    if v is None:
        return "—"
    if v < 0:
        return "exp"
    return f"{v:.1f}"


def _px(v: float | None, digits: int = 3) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


# ---------------------------------------------------------------------------
# panels
# ---------------------------------------------------------------------------


def header(state: DeskState) -> RenderableType:
    mode_style = "bold white on dark_red" if state.mode == "LIVE" else "bold black on green"
    risk_mode = state.risk.mode
    left = Text()
    left.append(" STABLEBOT DESK ", style="bold white on blue")
    left.append(f" {state.mode} ", style=mode_style)
    left.append("  ")
    left.append(f"● {risk_mode}", style=RISK_STYLE.get(risk_mode, "white"))
    if state.risk.reason:
        left.append(f"  {state.risk.reason}", style="dim")

    line2 = Text()
    line2.append(f" uptime {_dur(state.uptime)}", style="dim")
    line2.append("   autopilot ", style="dim")
    line2.append("ON" if state.autopilot else "OFF", style="bold green" if state.autopilot else "bold red")
    cycles = sum(s.cycles for s in state.sleeves.values())
    line2.append(f"   cycles {cycles}", style="dim")
    hit = state.hit_rate
    line2.append(
        f"   hit {hit*100:.0f}% ({state.total_wins}/{state.total_wins + state.total_losses})"
        if hit is not None
        else "   hit —",
        style="dim",
    )
    line2.append(f"   open {len(state.positions)}", style="dim")

    right = Text()
    right.append("EQUITY ", style="dim")
    right.append(money(state.equity), style="bold white")
    right.append("   session ")
    right.append_text(signed(state.session_pnl))
    right.append(" (")
    right.append_text(signed(state.session_pnl_pct, 2, pct=True))
    right.append(")")

    right2 = Text()
    right2.append("day ", style="dim")
    right2.append_text(signed(state.day_pnl))
    right2.append(" (")
    right2.append_text(signed(state.day_pnl_pct, 2, pct=True))
    right2.append(")   dd ", style="dim")
    right2.append_text(signed(state.drawdown, 2, pct=True))
    right2.append(f"   cash {money(state.cash)}", style="dim")

    venues = Text(" venues ", style="dim")
    if not state.venues:
        venues.append("probing…", style="dim")
    for h in state.venues.values():
        venues.append(f"{h.venue} ", style="dim")
        if h.ok:
            dot = "●" if h.latency_ms is None else f"● {h.latency_ms:.0f}ms"
            venues.append(dot, style="green")
        elif h.blocked:
            venues.append("● BLOCKED", style="bold red")
        else:
            venues.append(f"● {h.status}", style="red")
        venues.append("   ", style="dim")

    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(justify="right")
    grid.add_row(left, right)
    grid.add_row(line2, right2)
    grid.add_row(venues, Text(""))
    return Padding(grid, (0, 1), style="on grey11")


def book_panel(state: DeskState, height: int) -> RenderableType:
    t = Table(expand=True, box=None, pad_edge=False, padding=(0, 1))
    t.add_column("SYM", no_wrap=True, width=7)
    t.add_column("VEN", no_wrap=True, width=6, style="dim")
    t.add_column("WIN", no_wrap=True, width=8, style="dim")
    t.add_column("EXP", justify="right", no_wrap=True, width=5)
    t.add_column("ASK", justify="right", no_wrap=True, width=6)
    t.add_column("FAIR", justify="right", no_wrap=True, width=6)
    t.add_column("EDGE", justify="right", no_wrap=True, width=8)
    t.add_column("STATE", no_wrap=True, width=6)
    t.add_column("NOTE", overflow="ellipsis")

    rows = state.book
    if state.focus != "all":
        rows = [r for r in rows if r.sleeve == state.focus]
    shown = rows[: max(1, height)]
    for r in shown:
        style = STATE_STYLE.get(r.state, "")
        edge_txt = Text("—", style="dim")
        if r.edge is not None:
            edge_txt = Text(
                f"{r.edge:+.4f}",
                style="bold green" if r.edge > 0 else ("red" if r.edge < 0 else "dim"),
            )
        exp = _mins(r.expires_min)
        exp_style = "bold red" if (r.expires_min is not None and r.expires_min < 2) else ""
        t.add_row(
            Text(r.symbol, style=style),
            r.venue,
            r.window,
            Text(exp, style=exp_style),
            _px(r.ask),
            _px(r.fair),
            edge_txt,
            Text(r.state, style=style),
            Text(r.detail, style="dim"),
        )
    if not shown:
        t.add_row(Text("waiting for first scan…", style="dim"), "", "", "", "", "", "", "", "")
    armed = sum(1 for r in rows if r.state == "ARMED")
    title = f"[b]OPPORTUNITY BOOK[/b]  [dim]{len(rows)} rows[/dim]"
    if armed:
        title += f"  [bold green]{armed} ARMED[/bold green]"
    return Panel(t, title=title, title_align="left", border_style="blue")


def positions_panel(state: DeskState, height: int) -> RenderableType:
    t = Table(expand=True, box=None, pad_edge=False, padding=(0, 1))
    t.add_column("SLEEVE", no_wrap=True, width=11, style="dim")
    t.add_column("SYMBOL", no_wrap=True, width=12)
    t.add_column("QTY", justify="right", no_wrap=True, width=8)
    t.add_column("ENTRY", justify="right", no_wrap=True, width=6)
    t.add_column("COST", justify="right", no_wrap=True, width=9)
    t.add_column("TTL", justify="right", no_wrap=True, width=6)
    t.add_column("UPNL", justify="right", no_wrap=True, width=9)

    rows = state.positions
    if state.focus != "all":
        rows = [p for p in rows if p.sleeve == state.focus]
    for p in rows[: max(1, height)]:
        ttl = "—"
        ttl_style = ""
        if p.expires_in is not None:
            ttl = f"{p.expires_in:.0f}s"
            ttl_style = "bold red" if p.expires_in < 30 else ""
        u = p.unrealized
        upnl = Text("—", style="dim") if u is None else signed(u)
        t.add_row(
            p.sleeve,
            Text(p.symbol, style="bold"),
            f"{p.qty:,.2f}",
            f"{p.entry:.3f}",
            money(p.cost),
            Text(ttl, style=ttl_style),
            upnl,
        )
    if not rows:
        t.add_row(Text("flat", style="dim"), "", "", "", "", "", "")
    total = sum(p.cost for p in rows)
    return Panel(
        t,
        title=f"[b]OPEN POSITIONS[/b]  [dim]{len(rows)} @ {money(total)} at risk[/dim]",
        title_align="left",
        border_style="cyan",
    )


def tape_panel(state: DeskState, height: int) -> RenderableType:
    t = Table(expand=True, box=None, pad_edge=False, padding=(0, 1), show_header=False)
    t.add_column(no_wrap=True, width=8, style="dim")     # time
    t.add_column(no_wrap=True, width=6)                  # kind
    t.add_column(no_wrap=True, width=11, style="dim")    # sleeve
    t.add_column(no_wrap=True, width=10)                 # symbol
    t.add_column(no_wrap=True, width=5)                  # side
    t.add_column(justify="right", no_wrap=True, width=8) # qty
    t.add_column(justify="right", no_wrap=True, width=7) # price
    t.add_column(justify="right", no_wrap=True, width=9) # pnl
    t.add_column(overflow="ellipsis", style="dim")       # detail

    events = list(state.tape)
    if state.focus != "all":
        want = state.sleeves.get(state.focus)
        if want is not None:
            events = [e for e in events if e.sleeve == want.label]
    for e in reversed(events[-max(1, height):]):
        kind_style = {
            "fill": "bold cyan",
            "lock": "bold green",
            "resolve": "bold white",
            "skip": "dim",
            "info": "dim",
        }.get(e.kind, "")
        pnl_txt = Text("—", style="dim") if e.pnl is None else signed(e.pnl, 3)
        side_style = "green" if e.side == "UP" else ("red" if e.side == "DOWN" else "dim")
        t.add_row(
            e.ts.strftime("%H:%M:%S"),
            Text(e.kind.upper(), style=kind_style),
            e.sleeve,
            Text(e.symbol, style="bold"),
            Text(e.side, style=side_style),
            f"{e.qty:,.2f}",
            _px(e.price),
            pnl_txt,
            e.detail,
        )
    if not events:
        t.add_row("", Text("—", style="dim"), "", Text("no fills yet", style="dim"), "", "", "", "", "")
    return Panel(
        t,
        title=f"[b]TAPE[/b]  [dim]{len(state.tape)} events[/dim]",
        title_align="left",
        border_style="magenta",
    )


def _sleeve_block(s: SleeveStat, focused: bool) -> RenderableType:
    head = Text()
    head.append("▶ " if focused else "  ", style="bold yellow")
    head.append(s.label, style="bold" if s.enabled else "dim")
    head.append("  ")
    head.append(s.status, style=STATUS_STYLE.get(s.status, "dim"))
    if s.stale:
        # Loud on purpose: an enabled sleeve that is not cycling looks exactly
        # like a quiet one, and that ambiguity has cost a session before.
        head.append(f"  STALLED {s.stale_for:.0f}s", style="bold white on red")
    elif s.next_cycle_in > 0.5 and s.enabled:
        head.append(f"  {s.next_cycle_in:.0f}s", style="dim")

    l2 = Text("   ", style="")
    if s.enabled:
        l2.append(f"alloc {money(s.allocation)} ", style="dim")
        l2.append(f"({s.alloc_frac*100:.0f}%) ", style="dim")
        l2.append(f"clip {money(s.clip)}", style="dim")
    else:
        l2.append(s.disabled_reason or "disabled", style="dim red")

    l3 = Text("   ")
    l3.append(f"{s.trades} tr  ", style="dim")
    wr = s.win_rate
    if wr is not None:
        be = s.breakeven_hit_rate
        wr_style = "green" if (be is None or wr >= be) else "red"
        l3.append(f"{wr*100:.0f}% hit", style=wr_style)
        if be is not None:
            l3.append(f" /be {be*100:.0f}%", style="dim")
    else:
        l3.append("— hit", style="dim")
    l3.append("  ")
    l3.append_text(signed(s.realized))

    l4 = Text("   ")
    l4.append(sparkline(_cumulative(list(s.recent))), style="cyan")
    exp = s.expectancy
    if exp is not None:
        l4.append(f"  exp {exp:+.3f}/tr", style="green" if exp > 0 else "red")

    lines = [head, l2, l3] if not s.enabled else [head, l2, l3, l4]
    if s.tuning and s.enabled:
        lines.append(Text(f"   {s.tuning}", style="dim italic"))
    if s.last_error:
        lines.append(Text(f"   ! {s.last_error[:44]}", style="red"))
    return Group(*lines)


def _cumulative(vals: list[float]) -> list[float]:
    out: list[float] = []
    run = 0.0
    for v in vals:
        run += v
        out.append(run)
    return out


def sleeves_panel(state: DeskState) -> RenderableType:
    blocks: list[RenderableType] = []
    for i, s in enumerate(state.sleeves.values(), start=1):
        focused = state.focus == s.name
        blocks.append(Text(f" [{i}]", style="dim"))
        blocks.append(_sleeve_block(s, focused))
        blocks.append(Text(""))
    if not blocks:
        blocks = [Text("no sleeves", style="dim")]
    return Panel(
        Group(*blocks),
        title="[b]SLEEVES[/b]  [dim]allocator-managed[/dim]",
        title_align="left",
        border_style="green",
    )


def risk_panel(state: DeskState) -> RenderableType:
    r = state.risk
    lines: list[RenderableType] = []

    mode = Text(" ")
    mode.append(r.mode, style=RISK_STYLE.get(r.mode, "white"))
    if r.reason:
        mode.append(f"  {r.reason}", style="dim")
    lines.append(mode)

    used = 0.0
    if r.daily_stop_pct > 0:
        used = max(0.0, -state.day_pnl_pct / r.daily_stop_pct)
    row = Text(" day stop  ")
    row.append(f"{-r.daily_stop_pct*100:.2f}%  ", style="dim")
    lines.append(row)
    bar = Text("  ")
    bar.append_text(meter(used, 18, "red" if used > 0.5 else "yellow"))
    bar.append(f"  {used*100:.0f}% used", style="dim")
    lines.append(bar)

    dd_used = 0.0
    if r.max_drawdown_pct > 0:
        dd_used = max(0.0, -state.drawdown / r.max_drawdown_pct)
    row = Text(" drawdown  ")
    row.append(f"{state.drawdown*100:+.2f}% / {-r.max_drawdown_pct*100:.2f}%", style="dim")
    lines.append(row)
    bar = Text("  ")
    bar.append_text(meter(dd_used, 18, "red" if dd_used > 0.5 else "cyan"))
    lines.append(bar)

    lines.append(Text(""))
    row = Text(" loss budget ")
    row.append(f"{money(r.budget_remaining)}", style="bold" if r.budget_remaining <= 0 else "")
    row.append(f" of {money(r.budget_total)}", style="dim")
    lines.append(row)
    row = Text(" max new stake ")
    cap = max(0.0, r.capacity * r.throttle_mult)
    row.append(money(cap), style="bold red" if cap <= 0 else "bold green")
    lines.append(row)

    row = Text(" size mult ")
    row.append(f"x{r.throttle_mult:.2f}", style="bold" if r.throttle_mult < 1 else "dim")
    row.append(f"   peak {money(r.peak_equity)}", style="dim")
    lines.append(row)

    eq = [v for _, v in state.curve]
    if len(eq) > 1:
        lines.append(Text(""))
        lines.append(Text(" equity", style="dim"))
        spark = Text(" ")
        spark.append(sparkline(eq, 26), style="green" if state.session_pnl >= 0 else "red")
        lines.append(spark)

    return Panel(
        Group(*lines),
        title="[b]RISK[/b]",
        title_align="left",
        border_style="red" if r.mode in {"HALTED", "THROTTLED"} else "grey50",
    )



GATE_LABEL = {
    "no_vol": "no volatility estimate",
    "no_signal": "move too small (z)",
    "window_timing": "wrong point in window",
    "already_open": "already in that window",
    "max_concurrent": "at position limit",
    "no_quote": "venue quoted nothing",
    "reference_noise": "strike inside ref noise",
    "wrong_side": "would fade the model",
    "edge": "edge below the bar",
    "sizing": "clip / cash too small",
    "risk": "risk gate closed",
    "fired": "ENTERED",
}


def gates_panel(state: DeskState) -> RenderableType:
    """Why entries are not happening. The single most useful panel when idle."""
    lines: list[RenderableType] = []
    if not state.gates:
        lines.append(Text(" no gate data yet", style="dim"))
    for name, gc in state.gates.items():
        sleeve = state.sleeves.get(name)
        head = Text(" ")
        head.append(sleeve.label if sleeve else name, style="bold")
        total = gc.total
        head.append(f"  {total} checks", style="dim")
        lines.append(head)
        top = gc.top(5)
        if not top:
            lines.append(Text("   nothing evaluated yet", style="dim"))
        for gate, n in top:
            frac = n / total if total else 0.0
            row = Text("   ")
            style = "green" if gate == "fired" else ("yellow" if frac > 0.5 else "dim")
            row.append(f"{GATE_LABEL.get(gate, gate):<24}", style=style)
            row.append_text(meter(frac, 8, "green" if gate == "fired" else "yellow"))
            row.append(f" {n:>5}", style="dim")
            lines.append(row)
        binding = gc.binding()
        if binding and gc.counts.get("fired", 0) == 0 and total > 20:
            hint = gc.last_detail.get(binding[0], "")
            if hint:
                lines.append(Text(f"   last: {hint[:44]}", style="dim italic"))
    return Panel(
        Group(*lines),
        title="[b]WHY NO TRADE[/b]",
        title_align="left",
        border_style="yellow",
    )


def log_panel(state: DeskState, height: int) -> RenderableType:
    styles = {"error": "red", "warn": "yellow", "info": "dim"}
    lines: list[Text] = []
    for ts, level, msg in list(state.log)[-max(1, height):]:
        t = Text()
        t.append(ts.strftime("%H:%M:%S "), style="dim")
        t.append(msg, style=styles.get(level, ""))
        lines.append(t)
    if not lines:
        lines = [Text("—", style="dim")]
    return Panel(Group(*lines), title="[b]LOG[/b]", title_align="left", border_style="grey50")


def footer(state: DeskState) -> RenderableType:
    flash = state.flash_active()
    if flash:
        return Align.center(Text(flash, style="bold black on yellow"))
    keys = [
        ("q", "quit"),
        ("space", "pause" if not state.paused else "resume"),
        ("a", "autopilot"),
        ("1-9", "focus sleeve"),
        ("0", "all"),
        ("h", "halt"),
        ("c", "clear halt"),
        ("e", "resume sleeve"),
        ("r", "rebalance"),
        ("?", "help"),
    ]
    t = Text(" ")
    for k, v in keys:
        t.append(f"[{k}]", style="bold cyan")
        t.append(f" {v}  ", style="dim")
    return Padding(t, (0, 0), style="on grey11")


HELP = """
[bold]Stablebot trading desk[/bold]

The desk runs every enabled sleeve on its own clock, in one process. You do not
pick trades — the allocator hands capital to whatever is currently paying and
benches whatever is not, and the risk governor cuts size or stops entries
before a bad day becomes a bad week.

[bold]Keys[/bold]
  q / ctrl-c   quit (open paper positions stay in the ledger and resolve next run)
  space        pause — stops new entries, still resolves open positions
  a            toggle autopilot (off = sleeves keep their current sizing, no
               benching and no auto-tuning)
  1..9         focus one sleeve in the book / positions / tape
  0            show all sleeves again
  h            raise the halt file (data/desk_halt) — stops entries everywhere
  c            clear the halt file
  e            resume a benched sleeve — the focused one, or every benched
               sleeve when the focus is [all]. Resets its loss baseline so the
               allocator does not bench it again on what it already lost.
               On a focused, running sleeve this switches it off instead.
  r            force an allocator rebalance now
  ?            this help

[bold]Reading the book[/bold]
  ARMED   the sleeve would enter this row right now
  WATCH   quoted and live, edge below the entry bar
  HELD    already in this window
  COLD    no signal / no two-sided quote
  ERR     the venue did not answer

[bold]Honest caveats[/bold]
  Everything here is paper. Paper PnL is not cash. Fills are assumed at the
  quoted ask with a modelled fee; real books are thinner and slower, and a
  live Polymarket path exists only for pair-complete and stays behind its own
  gates. Nothing in the desk can place a live order.

[dim]press any key to return[/dim]
"""


def build(state: DeskState, height: int = 40) -> Layout:
    """Assemble the whole screen for one refresh."""
    root = Layout()
    root.split_column(
        Layout(name="header", size=4),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )
    root["body"].split_row(
        Layout(name="left", ratio=2),
        Layout(name="right", minimum_size=36, ratio=1),
    )

    body_h = max(12, height - 5)
    book_h = max(5, int(body_h * 0.40))
    pos_h = max(3, int(body_h * 0.22))
    tape_h = max(4, body_h - book_h - pos_h - 6)

    root["left"].split_column(
        Layout(book_panel(state, book_h - 2), name="book", ratio=40),
        Layout(positions_panel(state, pos_h - 2), name="pos", ratio=22),
        Layout(tape_panel(state, tape_h), name="tape", ratio=38),
    )

    n_sleeves = max(1, len(state.sleeves))
    sleeve_h = min(max(8, body_h - 30), 6 * n_sleeves + 2)
    gate_h = 0
    if state.gates:
        gate_h = min(12, 2 + sum(2 + len(g.top(5)) for g in state.gates.values()))
    right_cols = [
        Layout(sleeves_panel(state), name="sleeves", size=sleeve_h),
        Layout(risk_panel(state), name="risk", size=17),
    ]
    if gate_h:
        right_cols.append(Layout(gates_panel(state), name="gates", size=gate_h))
    log_h = max(3, body_h - sleeve_h - 17 - gate_h)
    right_cols.append(Layout(log_panel(state, log_h - 2), name="log"))
    root["right"].split_column(*right_cols)

    root["header"].update(header(state))
    root["footer"].update(footer(state))
    return root
