from __future__ import annotations

from pathlib import Path

from stablebot.backtest.book import BookResult


def save_book_chart(result: BookResult, path: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dates = [d.date for d in result.days]
    equity = [d.ending_equity for d in result.days]
    fund = [d.funding_pnl for d in result.days]
    depeg = [d.depeg_pnl for d in result.days]
    arb = [d.arb_pnl for d in result.days]
    idle = [getattr(d, "idle_pnl", 0.0) for d in result.days]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7.5), sharex=True, gridspec_kw={"height_ratios": [2, 1.4]})
    ax1.plot(dates, equity, color="#1f4e79", linewidth=2.0, label="equity")
    ax1.axhline(result.starting_balance, color="#888", linestyle="--", linewidth=1, label="start")
    ax1.set_ylabel("Equity (USD)")
    ax1.set_title(
        f"Book backtest  {result.start.date()} → {result.end.date()}  "
        f"start ${result.starting_balance:,.0f}  end ${result.ending_equity:,.2f}"
    )
    ax1.legend(loc="best")
    ax1.grid(True, alpha=0.3)

    import numpy as np

    x = np.arange(len(dates))
    # stacked bars that tolerate mixed signs: plot each series from 0
    width = 0.8
    ax2.bar(x, fund, width, label="funding", color="#2a9d8f")
    ax2.bar(x, depeg, width, label="depeg-fade", color="#e9c46a")
    ax2.bar(x, arb, width, label="arb", color="#e76f51")
    ax2.bar(x, idle, width, label="idle yield", color="#6c757d")
    ax2.axhline(0.0, color="#333", linewidth=0.8)
    ax2.set_ylabel("Daily PnL")
    ax2.set_xticks(x[:: max(1, len(x) // 10)])
    ax2.set_xticklabels([dates[i] for i in range(0, len(dates), max(1, len(dates) // 10))], rotation=30, ha="right")
    ax2.legend(loc="best")
    ax2.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
