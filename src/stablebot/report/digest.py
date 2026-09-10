from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from stablebot.market.book import iso
from stablebot.paper.ledger import Ledger, window_start
from stablebot.signals.trends import TrendRollup
from stablebot.strategy.stable_spread import ScanResult

console = Console()


def print_scan(result: ScanResult, x_note: str | None = None, trend: TrendRollup | None = None) -> None:
    console.rule("[bold]stablebot scan")
    if x_note:
        console.print(f"[yellow]{x_note}[/yellow]")
    if result.errors:
        for e in result.errors:
            console.print(f"[red]venue error[/red] {e}")
    console.print(f"quotes: {len(result.quotes)}  opps: {len(result.opportunities)}  depegs: {len(result.depegs)}")

    qt = Table(title="Quotes (mid)")
    qt.add_column("venue")
    qt.add_column("pair")
    qt.add_column("bid", justify="right")
    qt.add_column("ask", justify="right")
    qt.add_column("mid", justify="right")
    for q in sorted(result.quotes, key=lambda x: (x.pair, x.venue)):
        mid = q.mid
        qt.add_row(
            q.venue,
            q.pair,
            f"{q.bid:.6f}" if q.bid else "-",
            f"{q.ask:.6f}" if q.ask else "-",
            f"{mid:.6f}" if mid else "-",
        )
    console.print(qt)

    ot = Table(title="Opportunities (net after taker fees)")
    ot.add_column("kind")
    ot.add_column("pair")
    ot.add_column("buy")
    ot.add_column("sell")
    ot.add_column("gross bps", justify="right")
    ot.add_column("fees bps", justify="right")
    ot.add_column("net bps", justify="right")
    if not result.opportunities:
        ot.add_row("—", "none after fees + min_edge", "—", "—", "—", "—", "—")
    for o in result.opportunities[:25]:
        ot.add_row(
            o.kind, o.pair, o.buy_venue, o.sell_venue,
            f"{o.gross_bps:.2f}", f"{o.fee_bps:.2f}", f"{o.net_bps:.2f}",
        )
    console.print(ot)

    dt = Table(title="Depeg alerts")
    dt.add_column("venue")
    dt.add_column("pair")
    dt.add_column("mid", justify="right")
    dt.add_column("dev bps", justify="right")
    if not result.depegs:
        dt.add_row("—", "none", "—", "—")
    for a in result.depegs[:25]:
        dt.add_row(a.venue, a.pair, f"{a.mid:.6f}", f"{a.deviation_bps:+.2f}")
    console.print(dt)

    if trend:
        console.print(
            f"X {trend.window} fear={trend.fear_score:.2f} posts={trend.n_posts} "
            f"hint={trend.size_hint()} terms={trend.top_terms[:5]}"
        )
    console.print(
        "[dim]Research / paper-trading only. Retail stablecoin arb is usually fee-negative "
        "after taker fees, withdrawal fees, and latency.[/dim]"
    )


def print_digest(ledger: Ledger, window: str) -> None:
    start = window_start(window)
    now = datetime.now(timezone.utc)
    console.rule(f"[bold]digest {window}[/bold]  {iso(start)} → {iso(now)}")

    spreads = ledger.spreads_since(start)
    fills = ledger.fills_since(start)
    depegs = ledger.depegs_since(start)
    xrows = ledger.x_since(start)
    pnl = ledger.pnl_since(start)

    st = Table(title="Spreads seen")
    st.add_column("ts")
    st.add_column("kind")
    st.add_column("pair")
    st.add_column("net bps", justify="right")
    if not spreads:
        st.add_row("—", "none logged", "—", "—")
    for r in spreads[:20]:
        st.add_row(str(r["ts"])[:19], r["kind"], r["pair"], f"{r['net_bps']:.2f}")
    console.print(st)

    ft = Table(title="Paper fills")
    ft.add_column("ts")
    ft.add_column("pair")
    ft.add_column("notional", justify="right")
    ft.add_column("net bps", justify="right")
    ft.add_column("pnl", justify="right")
    if not fills:
        ft.add_row("—", "none", "—", "—", "—")
    for f in fills[:30]:
        ft.add_row(f.ts[:19], f.pair, f"{f.notional:.2f}", f"{f.net_bps:.2f}", f"{f.pnl:.4f}")
    console.print(ft)
    console.print(f"paper PnL ({window}): [bold]{pnl:+.4f}[/bold]  fills={len(fills)}")

    dtab = Table(title="Depeg events")
    dtab.add_column("ts")
    dtab.add_column("venue")
    dtab.add_column("pair")
    dtab.add_column("dev bps", justify="right")
    if not depegs:
        dtab.add_row("—", "—", "none", "—")
    for r in depegs[:20]:
        dtab.add_row(str(r["ts"])[:19], r["venue"], r["pair"], f"{r['deviation_bps']:+.2f}")
    console.print(dtab)

    terms: dict[str, int] = {}
    labels: dict[str, int] = {}
    for r in xrows:
        labels[r["label"]] = labels.get(r["label"], 0) + 1
        for tok in str(r["text"] or "").lower().split():
            t = "".join(ch for ch in tok if ch.isalnum())
            if len(t) >= 3:
                terms[t] = terms.get(t, 0) + 1
    top = sorted(terms.items(), key=lambda kv: kv[1], reverse=True)[:8]
    console.print(f"X posts: {len(xrows)}  labels={labels}  top terms={top or 'n/a'}")
    if ledger.get_note("x_disabled"):
        console.print("[yellow]X disabled (no bearer token)[/yellow]")


def print_book_scan(live, trend=None, fear_cut: float = 0.60) -> None:
    ft = Table(title="Funding harvest (OKX live)")
    ft.add_column("inst")
    ft.add_column("last rate", justify="right")
    ft.add_column("bps", justify="right")
    ft.add_column("trail3 avg bps", justify="right")
    ft.add_column("next")
    ft.add_column("signal")
    if not live.funding:
        ft.add_row("—", "none", "—", "—", "—", "—")
    for r in live.funding:
        if r.error:
            ft.add_row(r.inst_id, "err", "—", "—", "—", str(r.error)[:40])
            continue
        rate = f"{r.rate:.6f}" if r.rate is not None else "—"
        bps = f"{r.rate*10000:+.2f}" if r.rate is not None else "—"
        avg = f"{r.trail_avg*10000:+.2f}" if r.trail_avg is not None else "n<3"
        nxt = r.next_funding_time.strftime("%H:%MZ") if r.next_funding_time else "—"
        sig = "ENTER" if r.enter else "flat"
        ft.add_row(r.inst_id, rate, bps, avg, nxt, sig)
    console.print(ft)

    dt = Table(title="Depeg-fade candidates")
    dt.add_column("asset")
    dt.add_column("pair")
    dt.add_column("venue")
    dt.add_column("mid", justify="right")
    dt.add_column("dev bps", justify="right")
    dt.add_column("signal")
    dt.add_column("note")
    if not live.depegs:
        dt.add_row("—", "—", "—", "—", "—", "none", "")
    for r in live.depegs:
        mid = f"{r.mid:.6f}" if r.mid is not None else "—"
        dev = f"{r.dev_bps:+.2f}" if r.dev_bps is not None else "—"
        sig = "ENTER" if r.enter else "flat"
        dt.add_row(r.asset, r.pair, r.venue, mid, dev, sig, r.note)
    console.print(dt)
    for n in live.notes:
        console.print(f"[dim]{n}[/dim]")
    if trend and trend.fear_score >= fear_cut:
        console.print(
            f"[yellow]X fear {trend.fear_score:.2f} ≥ {fear_cut:.2f}: "
            "depeg-fade size would be cut on run; funding harvest stays on (hedged).[/yellow]"
        )
