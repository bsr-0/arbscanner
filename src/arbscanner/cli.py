"""CLI entry point for arbscanner."""

import argparse
import sys

from rich.console import Console

from arbscanner.config import settings
from arbscanner.dashboard import run_dashboard
from arbscanner.db import get_connection, log_opportunities
from arbscanner.engine import scan_all_pairs
from arbscanner.exchanges import create_exchanges, fetch_all_markets
from arbscanner.logging_config import setup_logging
from arbscanner.matcher import load_cache, run_matching

console = Console()


def cmd_scan(args: argparse.Namespace) -> None:
    """Run the live arb scanner dashboard."""
    from arbscanner.alerts import send_alerts
    from arbscanner.paper_trading import PaperTradingEngine

    interval = args.interval
    threshold = args.threshold
    max_workers = args.max_workers
    settings.edge_threshold = threshold
    settings.max_workers = max_workers

    console.print("[bold]Initializing exchanges...[/bold]")
    poly, kalshi = create_exchanges()

    # Load or build matched pairs
    cache = load_cache()
    if not cache.pairs:
        console.print("[yellow]No matched pairs found. Running matcher first...[/yellow]")
        poly_markets = fetch_all_markets(poly, "Polymarket")
        kalshi_markets = fetch_all_markets(kalshi, "Kalshi")
        cache = run_matching(poly_markets, kalshi_markets)

    if not cache.pairs:
        console.print("[red]No matched pairs found. Cannot scan.[/red]")
        sys.exit(1)

    console.print(f"Loaded {len(cache.pairs)} matched pairs (workers: {max_workers})")

    alerts_enabled = bool(settings.telegram_bot_token or settings.discord_webhook_url)
    if alerts_enabled:
        console.print(f"[green]Alerts enabled (threshold: {settings.alert_threshold:.1%})[/green]")

    paper_engine: PaperTradingEngine | None = None
    paper_threshold = args.paper_threshold
    if args.paper:
        paper_engine = PaperTradingEngine(initial_balance=args.paper_balance)
        console.print(
            f"[green]Paper trading enabled "
            f"(balance: ${args.paper_balance:.2f}, threshold: {paper_threshold:.1%})[/green]"
        )

    db_conn = get_connection()

    def do_scan():
        opps = scan_all_pairs(
            poly, kalshi, cache.pairs, threshold=threshold, max_workers=max_workers
        )
        log_opportunities(db_conn, opps)
        if alerts_enabled:
            send_alerts(opps)
        if paper_engine is not None:
            _auto_open_paper_positions(paper_engine, opps, paper_threshold)
        return opps, len(cache.pairs)

    paper_summary_fn = paper_engine.summary if paper_engine is not None else None
    try:
        run_dashboard(do_scan, interval=interval, paper_summary_fn=paper_summary_fn)
    finally:
        db_conn.close()
        if paper_engine is not None:
            paper_engine.close()


def _auto_open_paper_positions(engine, opps, threshold: float) -> None:
    """Auto-open a paper position for each new opportunity above threshold.

    Skips any (pair_id, direction) combination that already has an open
    position so we don't double-up on the same market.
    """
    if not opps:
        return

    open_keys = {
        (p.pair_id, p.direction) for p in engine.get_open_positions()
    }
    for opp in opps:
        if opp.net_edge < threshold:
            continue
        pair_id = f"{opp.poly_market_id}::{opp.kalshi_market_id}"
        if (pair_id, opp.direction) in open_keys:
            continue
        try:
            engine.open_position(opp)
            open_keys.add((pair_id, opp.direction))
        except ValueError as e:
            console.print(f"[yellow]Skipping paper open for {opp.poly_title}: {e}[/yellow]")


def cmd_match(args: argparse.Namespace) -> None:
    """Run the market matching pipeline."""
    console.print("[bold]Initializing exchanges...[/bold]")
    poly, kalshi = create_exchanges()

    console.print("Fetching markets from Polymarket...")
    poly_markets = fetch_all_markets(poly, "Polymarket")
    console.print(f"  Found {len(poly_markets)} binary markets")

    console.print("Fetching markets from Kalshi...")
    kalshi_markets = fetch_all_markets(kalshi, "Kalshi")
    console.print(f"  Found {len(kalshi_markets)} binary markets")

    console.print("Running matcher...")
    cache = run_matching(poly_markets, kalshi_markets, rematch=args.rematch)

    console.print("\n[bold green]Matching complete:[/bold green]")
    console.print(f"  Matched pairs: {len(cache.pairs)}")
    console.print(f"  Rejected pairs: {len(cache.rejected)}")


def cmd_pairs(args: argparse.Namespace) -> None:
    """Display current matched pairs."""
    cache = load_cache()

    if not cache.pairs:
        console.print("[yellow]No matched pairs found. Run 'arbscanner match' first.[/yellow]")
        return

    console.print(f"[bold]{len(cache.pairs)} matched pairs[/bold] (updated: {cache.updated_at})\n")

    for i, pair in enumerate(cache.pairs, 1):
        console.print(
            f"  {i:3d}. [cyan]{pair.poly_title}[/cyan]\n"
            f"       [dim]↔[/dim] [magenta]{pair.kalshi_title}[/magenta]\n"
            f"       [dim]Confidence: {pair.confidence:.1%} | Source: {pair.source}[/dim]"
        )


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the FastAPI web server."""
    import uvicorn

    console.print(f"[bold]Starting web server on port {args.port}...[/bold]")
    console.print(f"  Dashboard: http://localhost:{args.port}/dashboard")
    console.print(f"  API:       http://localhost:{args.port}/api/opportunities")
    console.print(f"  Landing:   http://localhost:{args.port}/")

    uvicorn.run(
        "arbscanner.web:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


def cmd_calibrate(args: argparse.Namespace) -> None:
    """Compute, ingest, or view calibration data."""
    from pathlib import Path

    from arbscanner.calibration import (
        compute_calibration_curves,
        get_historical_edge_stats,
        ingest_from_exchange,
        ingest_from_url,
    )

    if args.ingest_url:
        console.print(f"[bold]Downloading historical dataset from {args.ingest_url}...[/bold]")
        try:
            count = ingest_from_url(args.ingest_url)
            console.print(f"[green]Ingested {count} rows[/green]")
        except Exception as e:
            console.print(f"[red]Ingestion failed: {e}[/red]")
            sys.exit(1)
        return

    if args.ingest_live:
        console.print("[bold]Fetching resolved markets from both exchanges...[/bold]")
        poly, kalshi = create_exchanges()
        poly_count = ingest_from_exchange(poly, "Polymarket", limit=args.limit)
        kalshi_count = ingest_from_exchange(kalshi, "Kalshi", limit=args.limit)
        console.print(
            f"[green]Ingested {poly_count} Polymarket + {kalshi_count} Kalshi resolved markets[/green]"
        )
        return

    if args.data_file:
        data_path = Path(args.data_file)
        if not data_path.exists():
            console.print(f"[red]Data file not found: {data_path}[/red]")
            sys.exit(1)

        console.print(f"Computing calibration curves from {data_path}...")
        curves = compute_calibration_curves(data_path)
        console.print(f"[bold green]Computed {len(curves)} calibration entries[/bold green]")
        console.print(curves.to_string())
        return

    # Default: show stats from scanner DB
    console.print("[bold]Historical edge statistics from scanner database:[/bold]\n")
    stats = get_historical_edge_stats()
    if not stats:
        console.print("[yellow]No historical data yet. Run 'arbscanner scan' first.[/yellow]")
        return
    for k, v in stats.items():
        if isinstance(v, float):
            console.print(f"  {k}: {v:.4f}")
        else:
            console.print(f"  {k}: {v}")


def cmd_paper(args: argparse.Namespace) -> None:
    """Manage the paper trading account (summary, list, open, close, resolve)."""
    from arbscanner.paper_trading import PaperTradingEngine

    _validate_paper_args(args)

    engine = PaperTradingEngine(initial_balance=args.balance)
    try:
        if args.action == "summary":
            _print_paper_summary(engine)
        elif args.action == "list":
            _print_paper_positions(engine, status=args.status)
        elif args.action == "open":
            opp = _load_opportunity(args.opportunity_id)
            if opp is None:
                console.print(
                    f"[red]No logged opportunity with id={args.opportunity_id}[/red]"
                )
                sys.exit(1)
            try:
                pos = engine.open_position(
                    opp, size=args.size, opportunity_id=args.opportunity_id
                )
            except ValueError as e:
                console.print(f"[red]{e}[/red]")
                sys.exit(1)
            console.print(
                f"[green]Opened paper position id={pos.id} size={pos.size:.2f} "
                f"expected_profit=${pos.expected_profit:.2f}[/green]"
            )
        elif args.action == "close":
            try:
                pnl = engine.close_position(
                    args.position_id, poly_price=args.poly_price, kalshi_price=args.kalshi_price
                )
            except ValueError as e:
                console.print(f"[red]{e}[/red]")
                sys.exit(1)
            console.print(f"[green]Closed position {args.position_id}: pnl=${pnl:.4f}[/green]")
        elif args.action == "resolve":
            try:
                pnl = engine.close_resolved_position(
                    args.position_id, yes_won=args.outcome == "yes"
                )
            except ValueError as e:
                console.print(f"[red]{e}[/red]")
                sys.exit(1)
            console.print(
                f"[green]Resolved position {args.position_id} ({args.outcome}): "
                f"pnl=${pnl:.4f}[/green]"
            )
    finally:
        engine.close()


def _validate_paper_args(args: argparse.Namespace) -> None:
    """Fail fast with a friendly message when required sub-action args are missing."""
    if args.action == "open" and args.opportunity_id is None:
        console.print("[red]`paper open` requires --opportunity-id[/red]")
        sys.exit(2)
    if args.action == "close":
        missing = [
            name
            for name, val in (
                ("--position-id", args.position_id),
                ("--poly-price", args.poly_price),
                ("--kalshi-price", args.kalshi_price),
            )
            if val is None
        ]
        if missing:
            console.print(
                f"[red]`paper close` requires {' '.join(missing)}[/red]"
            )
            sys.exit(2)
    if args.action == "resolve":
        missing = [
            name
            for name, val in (
                ("--position-id", args.position_id),
                ("--outcome", args.outcome),
            )
            if val is None
        ]
        if missing:
            console.print(
                f"[red]`paper resolve` requires {' '.join(missing)}[/red]"
            )
            sys.exit(2)


def _load_opportunity(opportunity_id: int):
    """Load a logged opportunity from the SQLite log as an ArbOpportunity."""
    from datetime import datetime

    from arbscanner.models import ArbOpportunity

    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT timestamp, poly_market_id, kalshi_market_id, market_title,
                      direction, gross_edge, net_edge, available_size,
                      expected_profit, poly_price, kalshi_price
               FROM opportunities WHERE id = ?""",
            (opportunity_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return ArbOpportunity(
        poly_title=row[3],
        kalshi_title=row[3],
        poly_market_id=row[1],
        kalshi_market_id=row[2],
        direction=row[4],
        poly_price=row[9],
        kalshi_price=row[10],
        gross_edge=row[5],
        net_edge=row[6],
        available_size=row[7],
        expected_profit=row[8],
        timestamp=datetime.fromisoformat(row[0]),
    )


def _print_paper_summary(engine) -> None:
    summary = engine.summary()
    console.print("[bold]Paper trading account[/bold]")
    console.print(f"  Balance:             ${summary['balance']:.2f}")
    console.print(f"  Open positions:      {summary['open_positions']}")
    console.print(f"  Total trades:        {summary['total_trades']}")
    console.print(f"  Total realized PnL:  ${summary['total_pnl']:.4f}")
    console.print(f"  Win rate:            {summary['win_rate']:.1%}")
    console.print(f"  Avg PnL/trade:       ${summary['avg_pnl_per_trade']:.4f}")


def _print_paper_positions(engine, status: str) -> None:
    account = engine.get_account()
    if status == "open":
        positions = [p for p in account.positions if p.status == "open"]
    elif status == "closed":
        positions = [p for p in account.positions if p.status == "closed"]
    else:
        positions = account.positions

    if not positions:
        console.print(f"[yellow]No {status} paper positions.[/yellow]")
        return

    console.print(f"[bold]{len(positions)} {status} position(s):[/bold]\n")
    for p in positions:
        entry = f"poly={p.entry_poly_price:.3f} kalshi={p.entry_kalshi_price:.3f}"
        line = (
            f"  id={p.id:<4d} pair={p.pair_id}\n"
            f"       direction={p.direction} size={p.size:.2f} "
            f"entry=({entry})\n"
            f"       status={p.status} expected=${p.expected_profit:.4f}"
        )
        if p.status == "closed" and p.realized_pnl is not None:
            line += f" realized=${p.realized_pnl:.4f}"
        console.print(line)


def cmd_backup(args: argparse.Namespace) -> None:
    """Backup, restore, list, or prune the SQLite database."""
    from pathlib import Path

    from arbscanner.backup import (
        backup_database,
        list_backups,
        prune_backups,
        prune_old_opportunities,
        restore_database,
    )

    if args.action == "create":
        path = backup_database()
        console.print(f"[green]Backup created: {path}[/green]")
    elif args.action == "list":
        backups = list_backups()
        if not backups:
            console.print("[yellow]No backups found.[/yellow]")
            return
        console.print(f"[bold]{len(backups)} backups:[/bold]")
        for b in backups:
            size_mb = b.stat().st_size / 1024 / 1024
            console.print(f"  {b.name} ({size_mb:.2f} MB)")
    elif args.action == "restore":
        if not args.file:
            console.print("[red]--file is required for restore[/red]")
            sys.exit(1)
        restore_database(Path(args.file), force=args.force)
        console.print(f"[green]Restored from {args.file}[/green]")
    elif args.action == "prune":
        deleted = prune_backups(keep=args.keep)
        console.print(f"[green]Pruned {deleted} old backups (kept newest {args.keep})[/green]")
    elif args.action == "prune-opps":
        deleted = prune_old_opportunities(keep_days=args.days)
        console.print(
            f"[green]Deleted {deleted} opportunities older than {args.days} days[/green]"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="arbscanner",
        description="Cross-platform prediction market arbitrage scanner",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scan command
    scan_parser = subparsers.add_parser("scan", help="Run the live arb scanner")
    scan_parser.add_argument(
        "--interval", type=int, default=30, help="Refresh interval in seconds (default: 30)"
    )
    scan_parser.add_argument(
        "--threshold", type=float, default=0.01, help="Min net edge threshold (default: 0.01)"
    )
    scan_parser.add_argument(
        "--max-workers",
        type=int,
        default=settings.max_workers,
        help=f"Parallel workers for order book fetches (default: {settings.max_workers})",
    )
    scan_parser.add_argument(
        "--paper",
        action="store_true",
        help="Auto-open simulated paper trading positions for high-edge opportunities",
    )
    scan_parser.add_argument(
        "--paper-balance",
        type=float,
        default=10000.0,
        help="Starting paper trading balance in USD (default: 10000)",
    )
    scan_parser.add_argument(
        "--paper-threshold",
        type=float,
        default=0.02,
        help="Min net edge to auto-open a paper position (default: 0.02)",
    )

    # match command
    match_parser = subparsers.add_parser("match", help="Run market matching pipeline")
    match_parser.add_argument(
        "--rematch", action="store_true", help="Force full re-matching (ignore cache)"
    )

    # pairs command
    subparsers.add_parser("pairs", help="Show current matched pairs")

    # serve command
    serve_parser = subparsers.add_parser("serve", help="Start the web dashboard server")
    serve_parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)"
    )
    serve_parser.add_argument(
        "--port", type=int, default=8000, help="Port to bind to (default: 8000)"
    )
    serve_parser.add_argument(
        "--reload", action="store_true", help="Enable auto-reload for development"
    )

    # calibrate command
    cal_parser = subparsers.add_parser("calibrate", help="Compute, ingest, or view calibration data")
    cal_parser.add_argument(
        "--data-file", help="Path to historical resolution data (Parquet) to compute curves from"
    )
    cal_parser.add_argument(
        "--ingest-url", help="Download a historical Parquet dataset from a URL"
    )
    cal_parser.add_argument(
        "--ingest-live",
        action="store_true",
        help="Fetch resolved markets from Polymarket and Kalshi via pmxt",
    )
    cal_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max resolved markets to fetch per exchange (for --ingest-live)",
    )

    # paper command
    paper_parser = subparsers.add_parser(
        "paper", help="Manage the paper trading account (simulated execution)"
    )
    paper_parser.add_argument(
        "action",
        choices=["summary", "list", "open", "close", "resolve"],
        help="Paper trading action",
    )
    paper_parser.add_argument(
        "--balance",
        type=float,
        default=10000.0,
        help="Starting account balance (only used the first time, default: 10000)",
    )
    paper_parser.add_argument(
        "--status",
        choices=["open", "closed", "all"],
        default="all",
        help="Filter paper positions by status (for `list`)",
    )
    paper_parser.add_argument(
        "--opportunity-id",
        type=int,
        help="Logged opportunity ID to open a paper position from (for `open`)",
    )
    paper_parser.add_argument(
        "--size",
        type=float,
        help="Override position size in contracts (for `open`)",
    )
    paper_parser.add_argument(
        "--position-id",
        type=int,
        help="Paper position ID to act on (for `close` / `resolve`)",
    )
    paper_parser.add_argument(
        "--poly-price",
        type=float,
        help="Polymarket mark price for `close`",
    )
    paper_parser.add_argument(
        "--kalshi-price",
        type=float,
        help="Kalshi mark price for `close`",
    )
    paper_parser.add_argument(
        "--outcome",
        choices=["yes", "no"],
        help="Market outcome for `resolve`",
    )

    # backup command
    backup_parser = subparsers.add_parser(
        "backup", help="Database backup, restore, list, prune"
    )
    backup_parser.add_argument(
        "action",
        choices=["create", "list", "restore", "prune", "prune-opps"],
        help="Backup action to perform",
    )
    backup_parser.add_argument("--file", help="Backup file path (for restore)")
    backup_parser.add_argument(
        "--force", action="store_true", help="Force restore even with active DB journal"
    )
    backup_parser.add_argument(
        "--keep", type=int, default=10, help="Number of backups to keep when pruning"
    )
    backup_parser.add_argument(
        "--days", type=int, default=30, help="Keep opportunities newer than N days"
    )

    args = parser.parse_args()

    # Configure logging via our structured logging setup
    setup_logging(level="DEBUG" if args.verbose else "INFO")

    commands = {
        "scan": cmd_scan,
        "match": cmd_match,
        "pairs": cmd_pairs,
        "serve": cmd_serve,
        "calibrate": cmd_calibrate,
        "backup": cmd_backup,
        "paper": cmd_paper,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
