"""Terminal report for model calibration. See calibration.py for the maths."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text

from stablebot.desk.calibration import MIN_MEANINGFUL, Report, build_report, load_outcomes


def _ledger_for(sleeve: str):
    if sleeve == "kalshi_lag":
        from stablebot.desk.kalshi_lag import ledger_path

        return ledger_path(), "kalshi_lag", "ticker"
    from stablebot.poly.spot_lag import ledger_path

    return ledger_path(), "spot_lag", "slug"


def _verdict(r: Report) -> Text:
    skill = r.skill_vs_market
    t = Text()
    if skill > 0.05:
        t.append("model beat the price", style="bold green")
    elif skill < -0.05:
        t.append("the price was the better forecast", style="bold red")
    else:
        t.append("model matched the price", style="bold yellow")
    t.append(f"   skill {skill:+.3f}", style="dim")
    return t


def render(r: Report, sleeve: str, console: Console) -> None:
    console.print()
    console.print(Text(f"Calibration — {sleeve}", style="bold"))
    console.print(
        Text(
            "A fair value is a probability claim. The test is whether it matches "
            "reality better than the ask you lifted.",
            style="dim",
        )
    )
    console.print()

    if r.n == 0:
        console.print(Text("No resolved trades in the ledger yet.", style="yellow"))
        console.print(
            Text(
                "Nothing to score until positions settle — the report reads fills "
                "joined to resolves.",
                style="dim",
            )
        )
        return

    score = Table(box=None, pad_edge=False)
    score.add_column("", style="dim")
    score.add_column("Brier", justify="right")
    score.add_column("", style="dim")
    score.add_row("model", f"{r.brier_model:.4f}", "the fair-value model's claim")
    score.add_row("market", f"{r.brier_market:.4f}", "the price paid (entry_p)")
    score.add_row("base rate", f"{r.brier_base:.4f}", f"always predicting {r.base_rate:.0%}")
    console.print(score)
    console.print()
    console.print(_verdict(r))
    console.print()

    buckets = Table(title=None, header_style="bold")
    buckets.add_column("predicted", justify="center")
    buckets.add_column("n", justify="right")
    buckets.add_column("claimed", justify="right")
    buckets.add_column("realized", justify="right")
    buckets.add_column("gap", justify="right")
    for b in r.buckets:
        gap_style = "green" if abs(b.gap) < 0.10 else "red"
        buckets.add_row(
            f"{b.lo:.0%}-{b.hi:.0%}",
            str(b.n),
            f"{b.mean_p:.0%}",
            f"{b.realized:.0%}",
            Text(f"{b.gap:+.0%}", style=gap_style),
        )
    console.print(buckets)

    console.print()
    if not r.meaningful:
        console.print(
            Text(
                f"n={r.n}. Below {MIN_MEANINGFUL} resolved trades these buckets are noise — "
                "read the direction, not the number.",
                style="bold yellow",
            )
        )
    console.print(
        Text(
            "Every entry gate requires the model to claim more than the ask, so "
            "'claimed' sitting above 'realized' is the failure mode to watch for.",
            style="dim",
        )
    )
    console.print()


def run_calibration(sleeve: str = "kalshi_lag", buckets: int = 5) -> int:
    path, fill_kind, key = _ledger_for(sleeve)
    outcomes = load_outcomes(path, fill_kind=fill_kind, key=key)
    render(build_report(outcomes, buckets), sleeve, Console())
    return 0
