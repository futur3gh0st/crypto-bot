"""Build REPORT.md from sleeve hunt leaderboard JSONs."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_report(out_dir: Path, iteration2_ran: bool) -> Path:
    out_dir = Path(out_dir)
    p30 = out_dir / "leaderboard_30d.json"
    p90 = out_dir / "leaderboard_90d.json"
    r30 = json.loads(p30.read_text())["leaderboard"] if p30.is_file() else []
    r90 = json.loads(p90.read_text())["leaderboard"] if p90.is_file() else []
    honest30 = [r for r in r30 if not r.get("fantasy")]
    best_honest = honest30[0] if honest30 else (r30[0] if r30 else None)
    best_any = r30[0] if r30 else None
    hits = [r for r in honest30 if r.get("hits_5x")]
    fantasy_hits = [r for r in r30 if r.get("fantasy") and r.get("hits_5x")]

    L: list[str] = []
    L.append("# Sleeve Hunt Report")
    L.append("")
    L.append("Generated: %s (UTC)" % _iso(datetime.now(tz=timezone.utc)))
    L.append("Mode: paper research only.")
    L.append("")
    L.append("## Target")
    L.append("Goal: $1000 -> $5000 in 30 days. Fee curve 0.07*p*(1-p) charged.")
    L.append("")
    L.append("## Ideas source: MrFadiAi/Polymarket-bot (Cyril target)")
    L.append("- Classic arb (YES+NO under 1) -> pair_lock mid replay (MID!=ASK flagged)")
    L.append("- DipArb panic drop then hedge -> dip_arb 10/15/20 pct on 1m bars")
    L.append("- Risk caps via risk_frac/Kelly sizing")
    L.append("- Copy-trade wallets: skipped (no wallet feed)")
    L.append("- External bot package: not installed; ideas only")
    L.append("")
    L.append("## Dan1ro0 desk mapping")
    L.append("- SPOTTER: added (dan_desk / spot_lag)")
    L.append("- PRIOR: crude_fair_up sketch (not vol-aware)")
    L.append("- EDGE: min_edge + fee curve")
    L.append("- KELLY: kelly_frac * edge, capped at 5 pct equity")
    L.append("- TAKER: modeled catch-up entry")
    L.append("- Async pair-complete + CLOSER: added")
    L.append("- Six separate agents / true ask tape: skipped")
    L.append("")
    L.append("## Honest 5x answer")
    if hits:
        L.append("YES on paper under modeled fills: " + ", ".join(h["name"] for h in hits))
        L.append("Caveat: catch-up entry is still modeled, not proven book fills.")
    elif fantasy_hits:
        L.append("ONLY-WITH-FANTASY-FILLS (optimistic catch-up). Live will not match.")
        L.append("Fantasy names: " + ", ".join(h["name"] for h in fantasy_hits))
    else:
        L.append("NO — no honest variant reached $5000 final equity on 30d.")
        if best_honest:
            L.append(
                "Best honest 30d: %s -> $%.2f (%d trades, maxDD %.1f%%)."
                % (
                    best_honest["name"],
                    best_honest["final_equity"],
                    best_honest["n_trades"],
                    100.0 * best_honest["max_drawdown"],
                )
            )
    L.append("")
    L.append("## Leaderboard 30d")
    for i, r in enumerate(r30, 1):
        L.append(
            "%2d. %s | %s | eq=$%.2f | n=%d | WR=%.1f%% | DD=%.1f%% | 5x=%s | fantasy=%s"
            % (
                i,
                r["name"],
                r["family"],
                r["final_equity"],
                r["n_trades"],
                100.0 * r["win_rate"],
                100.0 * r["max_drawdown"],
                r.get("hits_5x"),
                r.get("fantasy"),
            )
        )
    L.append("")
    L.append("## Leaderboard 90d")
    if not r90:
        L.append("(none)")
    for i, r in enumerate(r90, 1):
        L.append(
            "%2d. %s | %s | eq=$%.2f | n=%d | WR=%.1f%% | DD=%.1f%% | fantasy=%s"
            % (
                i,
                r["name"],
                r["family"],
                r["final_equity"],
                r["n_trades"],
                100.0 * r["win_rate"],
                100.0 * r["max_drawdown"],
                r.get("fantasy"),
            )
        )
    L.append("")
    L.append("## Recommendation")
    if best_honest:
        L.append("Paper-run next: %s / %s" % (best_honest["family"], best_honest["name"]))
        L.append("30d equity: $%.2f" % best_honest["final_equity"])
        L.append("Daily CSV: %s" % best_honest.get("daily_path", ""))
        L.append("Params: %s" % json.dumps(best_honest.get("params", {})))
        L.append("Flags: %s" % "; ".join(best_honest.get("honesty_flags") or []))
        if best_honest["final_equity"] < 5000:
            L.append("Does NOT clear $5k/30d under honest assumptions.")
    else:
        L.append("No usable sleeve.")
    if best_any and best_any.get("fantasy"):
        L.append(
            "Top raw was FANTASY %s @ $%.2f — not live EV."
            % (best_any["name"], best_any["final_equity"])
        )
    L.append("Iteration-2 widen: %s" % ("RAN" if iteration2_ran else "NOT RUN"))
    L.append("")
    L.append("## Day-by-day (best honest)")
    dp = best_honest.get("daily_path") if best_honest else None
    if dp and Path(dp).is_file():
        L.append("File: " + str(dp))
        rows = Path(dp).read_text().strip().splitlines()
        L.append("```")
        L.extend(rows[:8])
        if len(rows) > 16:
            L.append("...")
            L.extend(rows[-8:])
        else:
            L.extend(rows[8:])
        L.append("```")
    L.append("")
    L.append("## Caveats")
    L.append("- Mid history != ask; live pair locks rare when ask-sum >= 1.01")
    L.append("- Catch-up entry is a model; high WR != proven CLOB lag")
    L.append("- Poly cache for pair_lock may miss recent windows")
    out = out_dir / "REPORT.md"
    out.write_text("\n".join(L) + "\n")
    return out
