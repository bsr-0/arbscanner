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

    db_conn = get_connection()

    def do_scan():
        opps = scan_all_pairs(
            poly, kalshi, cache.pairs, threshold=threshold, max_workers=max_workers
        )
        log_opportunities(db_conn, opps)
        if alerts_enabled:
            send_alerts(opps)
        return opps, len(cache.pairs)

    run_dashboard(do_scan, interval=interval)
    db_conn.close()


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


def cmd_paper_trade(args: argparse.Namespace) -> None:
    """Paper trading: open/close/resolve positions and inspect the account."""
    from arbscanner.db import get_connection, get_opportunity_by_id
    from arbscanner.paper_trading import PaperTradingEngine

    engine = PaperTradingEngine()

    if args.action == "summary":
        s = engine.summary()
        console.print("[bold]Paper trading account[/bold]")
        console.print(f"  Balance:          ${s['balance']:.2f}")
        console.print(f"  Total PnL:        ${s['total_pnl']:+.2f}")
        console.print(f"  Open positions:   {s['open_positions']}")
        console.print(f"  Total trades:     {s['total_trades']}")
        console.print(f"  Win rate:         {s['win_rate']:.1%}")
        console.print(f"  Avg PnL / trade:  ${s['avg_pnl_per_trade']:+.2f}")
        return

    if args.action == "list":
        account = engine.get_account()
        positions = account.positions
        if not args.all:
            positions = [p for p in positions if p.status == "open"]
        if not positions:
            console.print("[yellow]No positions found.[/yellow]")
            return
        console.print(f"[bold]{len(positions)} positions[/bold]")
        for p in positions:
            realized = (
                f"pnl=${p.realized_pnl:+.2f}" if p.realized_pnl is not None else "open"
            )
            console.print(
                f"  #{p.id} [{p.status}] {p.direction} size={p.size:.0f} "
                f"entry=({p.entry_poly_price:.3f}, {p.entry_kalshi_price:.3f}) "
                f"expected=${p.expected_profit:+.2f} {realized}"
            )
        return

    if args.action == "open":
        if args.opportunity_id is None:
            console.print("[red]--opportunity-id is required for 'open'[/red]")
            sys.exit(1)
        conn = get_connection()
        try:
            opp = get_opportunity_by_id(conn, args.opportunity_id)
        finally:
            conn.close()
        if opp is None:
            console.print(
                f"[red]No opportunity with id={args.opportunity_id}[/red]"
            )
            sys.exit(1)
        try:
            position = engine.open_position(
                opp, size=args.size, opportunity_id=args.opportunity_id
            )
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
        console.print(
            f"[green]Opened position #{position.id} "
            f"size={position.size:.2f} expected=${position.expected_profit:+.2f}[/green]"
        )
        return

    if args.action == "close":
        if args.position_id is None or args.poly_price is None or args.kalshi_price is None:
            console.print(
                "[red]'close' requires --position-id, --poly-price, and --kalshi-price[/red]"
            )
            sys.exit(1)
        try:
            realized = engine.close_position(
                args.position_id, args.poly_price, args.kalshi_price
            )
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
        console.print(
            f"[green]Closed position #{args.position_id} "
            f"realized PnL=${realized:+.2f}[/green]"
        )
        return

    if args.action == "resolve":
        if args.position_id is None:
            console.print("[red]--position-id is required for 'resolve'[/red]")
            sys.exit(1)
        if args.yes and args.no:
            console.print("[red]Use exactly one of --yes or --no[/red]")
            sys.exit(1)
        if not args.yes and not args.no:
            console.print("[red]Must supply --yes or --no[/red]")
            sys.exit(1)
        try:
            realized = engine.close_resolved_position(
                args.position_id, yes_won=args.yes
            )
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
        console.print(
            f"[green]Resolved position #{args.position_id} "
            f"(yes_won={args.yes}) realized PnL=${realized:+.2f}[/green]"
        )
        return


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

    # paper-trade command
    paper_parser = subparsers.add_parser(
        "paper-trade", help="Paper trading: open, close, resolve, list, summary"
    )
    paper_parser.add_argument(
        "action",
        choices=["open", "close", "resolve", "list", "summary"],
        help="Paper trading action",
    )
    paper_parser.add_argument(
        "--opportunity-id", type=int, help="Logged opportunity id (for open)"
    )
    paper_parser.add_argument(
        "--position-id", type=int, help="Paper position id (for close/resolve)"
    )
    paper_parser.add_argument("--size", type=float, help="Contracts to buy (default: full available_size)")
    paper_parser.add_argument("--poly-price", type=float, help="Exit poly price (for close)")
    paper_parser.add_argument("--kalshi-price", type=float, help="Exit kalshi price (for close)")
    paper_parser.add_argument("--yes", action="store_true", help="Resolve YES (for resolve)")
    paper_parser.add_argument("--no", action="store_true", help="Resolve NO (for resolve)")
    paper_parser.add_argument("--all", action="store_true", help="Include closed positions (for list)")

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
        "paper-trade": cmd_paper_trade,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
