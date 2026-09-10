from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from stablebot.config import env_settings, load_config
from stablebot.main import cmd_backtest, cmd_digest, cmd_run, cmd_scan


def _parse_date(value: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date: {value} (use YYYY-MM-DD)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stablebot",
        description="Fee-aware paper bot for stablecoin cross-venue / cross-pair spreads.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    desk = sub.add_parser(
        "desk",
        help="Trading desk TUI: menu + live dashboard + unattended autopilot (paper)",
    )
    desk.add_argument(
        "--auto",
        action="store_true",
        help="Skip the menu and start the desk immediately (for nohup / tmux / launchd)",
    )
    desk.add_argument(
        "--sleeves",
        default="all",
        choices=("all", "lag", "spot_lag", "kalshi_lag", "locks"),
        help="Which sleeves the desk runs (default all): "
             "lag = both directional sleeves, locks = both pair-complete sleeves",
    )
    desk.add_argument(
        "--pot",
        type=float,
        default=None,
        help="Budget the allocator splits across sleeves "
             "(default: the sleeves' own paper pots)",
    )
    desk.add_argument(
        "--no-autopilot",
        action="store_true",
        help="Keep the dashboard but stop the allocator re-sizing and benching sleeves",
    )
    desk.add_argument(
        "--legacy-signal",
        action="store_true",
        help="Use the original fixed-percent spot-lag gates instead of the vol-aware ones",
    )
    desk.add_argument(
        "--daily-stop",
        type=float,
        default=None,
        help="Daily stop as a fraction of day-open equity (default from config risk.daily_stop_pct)",
    )
    desk.add_argument(
        "--max-drawdown",
        type=float,
        default=0.25,
        help="Halt if equity falls this far below the session peak (default 0.25)",
    )
    desk.add_argument(
        "--headless",
        action="store_true",
        help="Run with no TUI, logging to stdout. For systemd/Docker, which have no tty",
    )
    desk.add_argument(
        "--serve",
        default=None,
        metavar="[HOST:]PORT",
        help="Publish state as JSON at /state, with or without --headless "
             "(default host 127.0.0.1; binding wider needs STABLEBOT_DESK_TOKEN)",
    )
    desk.add_argument(
        "--remote",
        default=None,
        metavar="URL",
        help="Draw a desk running elsewhere, e.g. http://127.0.0.1:8787 over an SSH tunnel",
    )

    sub.add_parser("scan", help="One-shot public ticker scan")

    cal = sub.add_parser(
        "calibration",
        help="Is the fair-value model sharper than the price it paid? (reads paper ledgers)",
    )
    cal.add_argument(
        "--sleeve",
        default="kalshi_lag",
        choices=["kalshi_lag", "spot_lag"],
        help="Which ledger to score (default: kalshi_lag)",
    )
    cal.add_argument("--buckets", type=int, default=5, help="Probability buckets (default: 5)")

    run = sub.add_parser("run", help="Loop: scan + paper fills")
    run.add_argument("--interval", type=int, default=None, help="Seconds between scans (default 60)")

    dig = sub.add_parser("digest", help="Print recent spreads / paper PnL / X terms")
    dig.add_argument("--window", choices=("hourly", "daily"), default="hourly")

    bt = sub.add_parser("backtest", help="Walk-forward paper replay on hourly candles")
    bt.add_argument("--days", type=int, default=None, help="Lookback days (7 or 30 typical)")
    bt.add_argument("--from", dest="start", type=_parse_date, default=None)
    bt.add_argument("--to", dest="end", type=_parse_date, default=None)
    bt.add_argument("--balance", type=float, default=1000.0, help="Starting paper equity (default 1000)")
    bt.add_argument("--fixture", action="store_true", help="Force bundled fixture (no network)")
    bt.add_argument(
        "--strategies",
        default="funding,depeg,arb",
        help="Comma list: funding,depeg,arb (default all three)",
    )
    ps = sub.add_parser("poly-scan", help="Polymarket crypto Up/Down scan (paper, no orders)")
    ps.add_argument("--windows", default=None, help="Comma minutes, e.g. 5,15")
    ps.add_argument("--coins", default=None, help="Comma coins, e.g. btc,eth,sol")

    pr = sub.add_parser("poly-run", help="Loop poly-scan + paper pair-complete fills")
    pr.add_argument("--interval", type=int, default=10, help="Seconds between scans (default 10)")
    pr.add_argument("--fade", action="store_true", help="Enable directional dislocation paper (off by default)")
    pr.add_argument("--windows", default=None, help="Comma minutes, e.g. 5,15")
    pr.add_argument("--coins", default=None, help="Comma coins, e.g. btc,eth,sol")
    live_g = pr.add_mutually_exclusive_group()
    live_g.add_argument(
        "--live",
        action="store_true",
        help="Arm CLOB V2 pair-complete (still requires POLY_LIVE=1 + confirm file + other gates)",
    )
    live_g.add_argument(
        "--live-dry-run",
        action="store_true",
        help="Walk live gates and log would-be FOK orders; never POST",
    )

    sub.add_parser(
        "poly-live-check",
        help="Validate Polymarket live env/files/client (no orders)",
    )

    pb = sub.add_parser("poly-backtest", help="Paper replay of Polymarket Up/Down (no live orders)")
    pb.add_argument("--days", type=int, default=7, help="Lookback days (7 required; 30 if cheap)")
    pb.add_argument("--balance", type=float, default=1000.0, help="Starting paper equity (default 1000)")
    pb.add_argument("--fade", action="store_true", help="End-equity uses lock+fade book (both books always computed)")
    pb.add_argument("--windows", default="15", help="Minutes, default 15 (5m is 3x API calls)")
    pb.add_argument("--coins", default="btc,eth,sol", help="Comma coins (xrp skipped)")
    pb.add_argument("--shares", type=float, default=20.0, help="Shares per complete/fade (default 20)")

    ks = sub.add_parser("kalshi-scan", help="Kalshi 15m YES/NO scan (paper, no orders)")
    ks.add_argument("--series", default=None, help="Comma series tickers, e.g. KXBTC15M,KXETH15M")

    kr = sub.add_parser("kalshi-run", help="Loop kalshi-scan + paper pair-complete fills")
    kr.add_argument("--interval", type=int, default=15, help="Seconds between scans (default 15)")
    kr.add_argument("--series", default=None, help="Comma series tickers, e.g. KXBTC15M,KXETH15M")


    sls = sub.add_parser(
        "spot-lag-scan",
        help="One-shot spot_lag paper scan (Binance move → Poly catch-up; no live orders)",
    )
    sls.add_argument("--coins", default="btc,eth,sol,xrp,doge,bnb", help="Comma coins")
    sls.add_argument("--windows", default="5", help="Comma minutes (default 5)")
    sls.add_argument("--catchup", type=float, default=0.70)
    sls.add_argument("--slip", type=float, default=0.02)
    sls.add_argument("--min-edge", type=float, default=0.04, dest="min_edge")
    sls.add_argument("--threshold", type=float, default=0.003)
    sls.add_argument("--balance", type=float, default=1000.0, help="Seed session if missing")
    sls.add_argument(
        "--live",
        action="store_true",
        help=argparse.SUPPRESS,  # refused — paper only
    )

    slr = sub.add_parser(
        "spot-lag-run",
        help="Loop spot_lag paper fills (no live CLOB orders, ever)",
    )
    slr.add_argument("--interval", type=int, default=15, help="Seconds between scans (default 15)")
    slr.add_argument("--coins", default="btc,eth,sol,xrp,doge,bnb", help="Comma coins")
    slr.add_argument("--windows", default="5", help="Comma minutes (default 5)")
    slr.add_argument("--catchup", type=float, default=0.70)
    slr.add_argument("--slip", type=float, default=0.02)
    slr.add_argument("--min-edge", type=float, default=0.04, dest="min_edge")
    slr.add_argument("--threshold", type=float, default=0.003)
    slr.add_argument("--balance", type=float, default=1000.0, help="Seed session if missing")
    slr.add_argument(
        "--live",
        action="store_true",
        help=argparse.SUPPRESS,  # refused — paper only
    )

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    settings = env_settings()

    if args.cmd == "desk":
        from stablebot.desk.entry import cmd_desk

        cmd_desk(cfg, settings, args)
        return
    if args.cmd == "calibration":
        from stablebot.desk.calibration_report import run_calibration

        return run_calibration(args.sleeve, args.buckets)

    if args.cmd == "scan":
        asyncio.run(cmd_scan(cfg, settings))
        return
    if args.cmd == "poly-scan":
        from stablebot.poly.scan import cmd_poly_scan

        asyncio.run(cmd_poly_scan(cfg, args.coins, args.windows))
        return
    if args.cmd == "poly-live-check":
        from stablebot.poly.live import cmd_poly_live_check

        cmd_poly_live_check()
        return
    if args.cmd == "poly-run":
        from stablebot.poly.scan import cmd_poly_run

        if args.fade and (args.live or args.live_dry_run):
            raise SystemExit("--fade is forbidden with --live / --live-dry-run")
        asyncio.run(
            cmd_poly_run(
                cfg,
                interval=args.interval,
                fade=args.fade,
                coins_raw=args.coins,
                windows_raw=args.windows,
                live=args.live,
                live_dry_run=args.live_dry_run,
            )
        )
        return
    if args.cmd == "run":
        interval = args.interval or cfg.strategy.scan_interval_sec
        asyncio.run(cmd_run(cfg, settings, interval))
        return
    if args.cmd == "digest":
        cmd_digest(args.window)
        return
    if args.cmd == "backtest":
        if args.balance < 100:
            raise SystemExit("--balance must be at least 100")
        if args.days is None and not (args.start and args.end):
            args.days = 7
        asyncio.run(
            cmd_backtest(
                cfg,
                days=args.days,
                start=args.start,
                end=args.end,
                balance=args.balance,
                fixture=args.fixture,
                strategies=args.strategies,
            )
        )
        return

    if args.cmd == "kalshi-scan":
        from stablebot.kalshi.scan import cmd_kalshi_scan

        asyncio.run(cmd_kalshi_scan(cfg, args.series))
        return
    if args.cmd == "kalshi-run":
        from stablebot.kalshi.scan import cmd_kalshi_run

        asyncio.run(cmd_kalshi_run(cfg, interval=args.interval, series_raw=args.series))
        return
    if args.cmd == "poly-backtest":
        if args.balance < 100:
            raise SystemExit("--balance must be at least 100")
        if args.days < 1:
            raise SystemExit("--days must be >= 1")
        from stablebot.poly.backtest import cmd_poly_backtest

        asyncio.run(
            cmd_poly_backtest(
                cfg,
                days=args.days,
                balance=args.balance,
                fade=args.fade,
                coins_raw=args.coins,
                windows_raw=args.windows,
                shares=args.shares,
            )
        )
        return

    if args.cmd == "spot-lag-scan":
        if getattr(args, "live", False):
            raise SystemExit("spot-lag is paper-only; --live is refused")
        from stablebot.poly.spot_lag import cmd_spot_lag_scan

        asyncio.run(cmd_spot_lag_scan(cfg, args))
        return
    if args.cmd == "spot-lag-run":
        if getattr(args, "live", False):
            raise SystemExit("spot-lag is paper-only; --live is refused")
        from stablebot.poly.spot_lag import cmd_spot_lag_run

        asyncio.run(cmd_spot_lag_run(cfg, args))
        return
