"""The desk orchestrator: runs every sleeve concurrently and draws the screen.

One process, one event loop. Each sleeve keeps its own clock, the allocator
re-sizes them from their own realised results, and the risk governor can cut
size or stop entries without anyone at the keyboard. Quitting is safe at any
moment — open paper positions live in the JSONL ledgers and are replayed and
resolved the next time the desk starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from typing import Sequence

from rich.console import Console
from rich.live import Live
from rich.panel import Panel

from stablebot.config import AppConfig
from stablebot.desk import render
from stablebot.desk.allocator import Allocator, AllocatorCfg
from stablebot.desk.keys import KeyReader, raw_mode
from stablebot.desk.risk import RiskGovernor, clear_halt, halt_present, raise_halt
from stablebot.desk.sleeves import Sleeve
from stablebot.desk.state import DeskState

REFRESH_HZ = 4.0
# Re-probe venues on this cadence so a network change is picked up without a restart.
VENUE_RECHECK_SECONDS = 180.0
# A sleeve is called stalled after this many of its own intervals pass with no
# completed cycle — floored, so a fast sleeve is not accused during one slow scan.
STALE_INTERVALS = 4.0
STALE_FLOOR_SECONDS = 90.0


class DeskApp:
    def __init__(
        self,
        cfg: AppConfig,
        sleeves: Sequence[Sleeve],
        pot: float | None = None,
        autopilot: bool = True,
        console: Console | None = None,
        allocator: Allocator | None = None,
        governor: RiskGovernor | None = None,
    ):
        self.cfg = cfg
        self.sleeves = list(sleeves)
        self.console = console or Console()
        self.state = DeskState()
        self.state.autopilot = autopilot
        self.allocator = allocator or Allocator(AllocatorCfg(pot=pot or 0.0))
        self.governor = governor or RiskGovernor(cfg)
        self._pot_override = pot
        self.keys = KeyReader()
        self._help = False

    # ---- equity roll-up ------------------------------------------------

    def _refresh_equity(self) -> None:
        """Sum each distinct pot's stake once, then add what each sleeve did to it."""
        st = self.state
        pots: dict[str, float] = {}
        for s in self.sleeves:
            pots[s.pot_id] = max(pots.get(s.pot_id, 0.0), s.pot_start)
        starting = sum(pots.values())
        equity = starting + sum(s.pot_pnl for s in self.sleeves)
        open_cost = sum(s.open_cost for s in self.sleeves)
        if st.starting_equity <= 0:
            st.starting_equity = starting or equity
        st.mark_equity(equity, equity - open_cost, open_cost)

    # ---- key handling --------------------------------------------------

    def _handle_key(self, ch: str) -> None:
        st = self.state
        if self._help:
            self._help = False
            return
        if ch in {"q", "Q", "\x03", "\x04"}:
            st.quit = True
            st.note("info", "quit requested")
        elif ch == " ":
            st.paused = not st.paused
            st.flash("PAUSED — no new entries" if st.paused else "resumed")
            st.note("info", "paused" if st.paused else "resumed")
        elif ch in {"a", "A"}:
            st.autopilot = not st.autopilot
            st.flash(f"autopilot {'ON' if st.autopilot else 'OFF'}")
            st.note("info", f"autopilot {'on' if st.autopilot else 'off'}")
        elif ch in {"h", "H"}:
            p = raise_halt("desk keypress")
            st.flash(f"HALT raised: {p}")
            st.note("warn", f"halt raised ({p})")
        elif ch in {"c", "C"}:
            if clear_halt():
                st.flash("halt cleared")
                st.note("info", "halt cleared")
            else:
                st.flash("no halt file to clear")
        elif ch in {"r", "R"}:
            self.allocator.rebalance(st, force=True)
            self.allocator.tune(st, force=True)
            st.flash("rebalanced")
        elif ch in {"e", "E"}:
            self._resume_sleeves()
        elif ch == "0":
            st.focus = "all"
        elif ch.isdigit():
            idx = int(ch) - 1
            names = list(st.sleeves)
            if 0 <= idx < len(names):
                st.focus = "all" if st.focus == names[idx] else names[idx]
        elif ch == "?":
            self._help = True

    def _resume_sleeves(self) -> None:
        """Put a benched sleeve back to work. The operator overrides the allocator.

        Acts on the focused sleeve, or on every auto-benched sleeve when the
        focus is "all". The loss baseline is moved to what the sleeve has
        already lost, otherwise the next rebalance benches it straight back on
        the same number the operator just overrode. A sleeve the operator
        disabled by hand is left alone by the allocator.
        """
        st = self.state
        targets = (
            [st.sleeves[st.focus]]
            if st.focus in st.sleeves
            else [s for s in st.sleeves.values() if not s.enabled]
        )
        if not targets:
            st.flash("nothing to resume")
            return
        woken = []
        for stat in targets:
            if stat.enabled and st.focus in st.sleeves:
                stat.enabled = False
                stat.disabled_reason = "operator: switched off"
                stat.status = "off"
                st.flash(f"{stat.label} off")
                st.note("info", f"{stat.label} switched off by operator")
                return
            stat.enabled = True
            stat.disabled_reason = ""
            stat.status = "idle"
            stat.bench_baseline = stat.realized
            stat.probe_at = 0.0
            woken.append(stat.label)
        st.flash(f"resumed {', '.join(woken)}")
        for label in woken:
            st.note("info", f"{label} resumed by operator — loss baseline reset")
        self.allocator.rebalance(st, force=True)

    async def _key_loop(self) -> None:
        while not self.state.quit:
            ch = await self.keys.get(timeout=0.15)
            if ch:
                self._handle_key(ch)

    # ---- venue health --------------------------------------------------

    async def _check_venues(self, first: bool = True) -> None:
        """Probe venues and follow the result.

        Re-run periodically, not just at startup: a venue that is unreachable on
        one network is often fine on another, and the desk should pick the
        sleeve back up by itself rather than needing a restart.
        """
        from stablebot.desk.health import probe_all, sleeve_blockers

        st = self.state
        if first:
            st.note("info", "probing venues…")
        try:
            health = await probe_all()
        except Exception as exc:  # noqa: BLE001
            st.note("warn", f"venue probe failed: {exc}")
            return
        was = {k: v.ok for k, v in st.venues.items()}
        st.venues = health
        for h in health.values():
            changed = was.get(h.venue) is not None and was[h.venue] != h.ok
            if first or changed:
                if h.ok:
                    st.note("info", f"{h.label}: reachable ({h.detail})")
                elif h.blocked:
                    st.note("error", f"{h.label}: {h.detail}")
                else:
                    st.note("warn", f"{h.label}: {h.status} — {h.detail}")

        changed_enabled = False
        for s in self.sleeves:
            stat = s.stat(st)
            blockers = sleeve_blockers(s.name, health)
            venue_disabled = stat.disabled_reason.startswith("venue unreachable")
            if blockers:
                names = ", ".join(b.label for b in blockers)
                reason = f"venue unreachable: {names}"
                if stat.enabled or stat.disabled_reason != reason:
                    if stat.enabled:
                        st.note("error", f"{stat.label} disabled — {names} not reachable")
                    stat.enabled = False
                    stat.disabled_reason = reason
                    stat.status = "off"
                    changed_enabled = True
            elif venue_disabled:
                stat.enabled = True
                stat.disabled_reason = ""
                stat.status = "idle"
                st.note("info", f"{stat.label} re-enabled — its venues are reachable again")
                changed_enabled = True

        if changed_enabled:
            # Who is trading just changed, so the split is stale — redo it now
            # rather than leaving a disabled sleeve holding an allocation.
            self.allocator.rebalance(st, force=True)

        if first and all(not s.stat(st).enabled for s in self.sleeves):
            st.note("error", "every sleeve is blocked by venue reachability — nothing can trade")

    # ---- watchdog ------------------------------------------------------

    def _check_watchdog(self) -> None:
        """Say so when an enabled sleeve stops completing cycles.

        A sleeve that is benched already labels itself. The dangerous state is
        the one that still calls itself enabled while its scan loop has quietly
        stopped turning: the book freezes, no trade fires, and the desk looks
        merely quiet. Silence and idleness are indistinguishable on a scan table
        full of dashes, so the desk measures the gap and names it.
        """
        st = self.state
        now = time.monotonic()
        for s in self.sleeves:
            stat = s.stat(st)
            if not stat.enabled:
                # Off on purpose; disabled_reason already carries the why.
                stat.stale = False
                stat.stale_for = 0.0
                continue
            limit = max(STALE_FLOOR_SECONDS, s.interval * STALE_INTERVALS)
            silent = now - (stat.last_cycle_mono or st.started_mono)
            was = stat.stale
            stat.stale = silent > limit
            stat.stale_for = silent if stat.stale else 0.0
            if stat.stale and not was:
                ran = "has never completed one" if not stat.last_cycle_mono else f"none in {silent:.0f}s"
                st.note(
                    "error",
                    f"{stat.label}: scan loop stalled — {ran} "
                    f"(interval {s.interval:.0f}s, last error: {stat.last_error or 'none'})",
                )
            elif was and not stat.stale:
                st.note("info", f"{stat.label}: cycling again")

    # ---- supervisor ----------------------------------------------------

    async def _supervisor(self) -> None:
        """Risk, allocation and venue health, on a slower clock than the render."""
        st = self.state
        last_probe = time.monotonic()
        while not st.quit:
            self._refresh_equity()
            self._check_watchdog()
            self.governor.evaluate(st)
            if st.autopilot:
                self.allocator.rebalance(st)
                self.allocator.tune(st)
            if time.monotonic() - last_probe >= VENUE_RECHECK_SECONDS:
                last_probe = time.monotonic()
                await self._check_venues(first=False)
            await asyncio.sleep(1.0)

    async def _render_loop(self, live: Live) -> None:
        st = self.state
        period = 1.0 / REFRESH_HZ
        while not st.quit:
            height = self.console.size.height or 40
            if self._help:
                live.update(Panel(render.HELP, title="[b]HELP[/b]", border_style="cyan"))
            else:
                live.update(render.build(st, height))
            await asyncio.sleep(period)

    # ---- entry point ---------------------------------------------------

    async def _bootstrap(self) -> None:
        """Everything both the TUI desk and the headless daemon need first."""
        st = self.state
        for s in self.sleeves:
            s.stat(st)
        self._refresh_equity()
        # The sleeves own separate paper pots (spot-lag has its own session file;
        # the two lock venues share one). Unless the operator named a budget,
        # size against what is actually there rather than a guess.
        if self._pot_override is None:
            self.allocator.cfg.pot = st.starting_equity
        seen: dict[str, float] = {}
        for s in self.sleeves:
            seen[s.pot_id] = max(seen.get(s.pot_id, 0.0), s.pot_start)
        pots = ", ".join(f"{k} ${v:,.0f}" for k, v in seen.items() if v)
        if pots:
            st.note("info", f"paper pots — {pots}")
        self.governor.bind(st)
        self.allocator.rebalance(st, force=True)
        st.note(
            "info",
            f"desk up — {len(self.sleeves)} sleeves, allocator budget "
            f"${self.allocator.cfg.pot:,.0f}",
        )
        if halt_present():
            st.note("warn", "halt file present at startup — entries are blocked")
        await self._check_venues()

    # ---- headless daemon -----------------------------------------------

    async def run_headless(self, serve: str | None = None) -> DeskState:
        """Run the desk with no terminal, for a VPS under systemd.

        The Rich Live view needs a tty and raw-mode keys need a keyboard;
        neither exists under a service manager. Same sleeves, same supervisor,
        same risk — the screen is replaced by a log on stdout for journald, and
        optionally a JSON snapshot a remote terminal can draw.
        """
        from stablebot.desk.server import serve_state
        from stablebot.desk.wire import state_to_dict

        st = self.state
        await self._bootstrap()

        server = None
        if serve:
            host, _, port = serve.rpartition(":")
            server = await serve_state(
                lambda: state_to_dict(st), host or "127.0.0.1", int(port)
            )
            st.note("info", f"state endpoint on {host or '127.0.0.1'}:{port}/state")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, lambda: setattr(st, "quit", True))

        tasks = [asyncio.create_task(sl.run(st), name=f"sleeve:{sl.name}") for sl in self.sleeves]
        tasks.append(asyncio.create_task(self._supervisor(), name="supervisor"))
        tasks.append(asyncio.create_task(self._log_pump(), name="log"))
        try:
            while not st.quit:
                await asyncio.sleep(0.2)
        except (KeyboardInterrupt, asyncio.CancelledError):
            st.quit = True
        finally:
            st.quit = True
            if server is not None:
                server.close()
                with contextlib.suppress(Exception):
                    await server.wait_closed()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for sl in self.sleeves:
                with contextlib.suppress(Exception):
                    await sl.teardown()
        return st

    async def _log_pump(self) -> None:
        """Print new desk notes to stdout so journald keeps the history."""
        st = self.state
        last: tuple | None = None
        while not st.quit:
            entries = list(st.log)
            if last is not None and last in entries:
                fresh = entries[entries.index(last) + 1:]
            else:
                fresh = entries
            for ts, level, msg in fresh:
                print(f"{ts:%H:%M:%S} {level:<5} {msg}", flush=True)
            if entries:
                last = entries[-1]
            await asyncio.sleep(0.5)

    # ---- entry point ---------------------------------------------------

    async def run(self) -> DeskState:
        st = self.state
        await self._bootstrap()

        with raw_mode():
            self.keys.start()
            tasks = [asyncio.create_task(s.run(st), name=f"sleeve:{s.name}") for s in self.sleeves]
            tasks.append(asyncio.create_task(self._supervisor(), name="supervisor"))
            tasks.append(asyncio.create_task(self._key_loop(), name="keys"))
            try:
                with Live(
                    render.build(st, self.console.size.height or 40),
                    console=self.console,
                    refresh_per_second=REFRESH_HZ,
                    screen=True,
                    transient=False,
                ) as live:
                    tasks.append(asyncio.create_task(self._render_loop(live), name="render"))
                    while not st.quit:
                        await asyncio.sleep(0.2)
            except (KeyboardInterrupt, asyncio.CancelledError):
                st.quit = True
            finally:
                st.quit = True
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.keys.stop()
                for s in self.sleeves:
                    with contextlib.suppress(Exception):
                        await s.teardown()
        return st

    # ---- closing summary -----------------------------------------------

    def print_summary(self) -> None:
        st = self.state
        c = self.console
        c.print()
        c.rule("[bold]desk session summary")
        c.print(
            f"ran {render._dur(st.uptime)}   equity {render.money(st.equity)}   "
            f"session PnL {st.session_pnl:+,.2f} ({st.session_pnl_pct*100:+.2f}%)   "
            f"peak {render.money(st.peak_equity)}"
        )
        for s in st.sleeves.values():
            wr = s.win_rate
            wr_s = f"{wr*100:.0f}%" if wr is not None else "—"
            exp = s.expectancy
            exp_s = f"{exp:+.3f}/trade" if exp is not None else "—"
            c.print(
                f"  {s.label:<12} {s.trades:>4} trades  {s.wins}W/{s.losses}L/{s.scratches}S  "
                f"hit {wr_s:>5}  expectancy {exp_s:>12}  realised {s.realized:+,.2f}"
                + (f"   [dim]{s.disabled_reason}[/dim]" if not s.enabled else "")
            )
        for name, gates in st.gates.items():
            top = ", ".join(f"{k}={v}" for k, v in gates.top(5))
            if top:
                c.print(f"  [dim]{name} gates: {top}[/dim]")
        c.print(
            "\n[dim]Paper only. Paper PnL is not cash — fills are assumed at the quoted "
            "ask with a modelled fee, and no live order was placed.[/dim]"
        )


def build_default_sleeves(
    cfg: AppConfig,
    which: Sequence[str] | None = None,
    balance: float = 1000.0,
    legacy_spot_lag: bool = False,
) -> list[Sleeve]:
    """The standard desk: spot-lag plus both lock venues."""
    from stablebot.desk.sleeves import (
        KalshiLagSleeve,
        KalshiSleeve,
        PairCompleteSleeve,
        SpotLagSleeve,
    )

    want = set(which or {"spot_lag", "kalshi_lag", "poly_lock", "kalshi_lock"})
    out: list[Sleeve] = []
    if "spot_lag" in want:
        out.append(SpotLagSleeve(cfg, balance=balance, legacy=legacy_spot_lag))
    if "kalshi_lag" in want:
        out.append(KalshiLagSleeve(cfg, balance=balance))
    if "poly_lock" in want:
        out.append(PairCompleteSleeve(cfg))
    if "kalshi_lock" in want:
        out.append(KalshiSleeve(cfg))
    return out
