"""CLI entry point for arbscanner."""

import argparse
import logging
import sys

from rich.console import Console

from arbscanner.config import settings
from arbscanner.dashboard import run_dashboard
from arbscanner.db import get_connection, log_opportunities
from arbscanner.engine import scan_all_pairs
from arbscanner.exchanges import create_exchanges, fetch_all_markets
from arbscanner.matcher import load_cache, run_matching

console = Console()


def cmd_scan(args: argparse.Namespace) -> None:
    """Run the live arb scanner dashboard."""
    from arbscanner.alerts import send_alerts

    interval = args.interval
    threshold = args.threshold
    settings.edge_threshold = threshold

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

    console.print(f"Loaded {len(cache.pairs)} matched pairs")

    alerts_enabled = bool(settings.telegram_bot_token or settings.discord_webhook_url)
    if alerts_enabled:
        console.print(f"[green]Alerts enabled (threshold: {settings.alert_threshold:.1%})[/green]")

    db_conn = get_connection()

    def do_scan():
        opps = scan_all_pairs(poly, kalshi, cache.pairs, threshold=threshold)
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
    """Compute calibration curves from historical data."""
    from pathlib import Path

    from arbscanner.calibration import compute_calibration_curves, get_historical_edge_stats

    if args.data_file:
        data_path = Path(args.data_file)
        if not data_path.exists():
            console.print(f"[red]Data file not found: {data_path}[/red]")
            sys.exit(1)

        console.print(f"Computing calibration curves from {data_path}...")
        curves = compute_calibration_curves(data_path)
        console.print(f"[bold green]Computed {len(curves)} calibration entries[/bold green]")
        console.print(curves.to_string())
    else:
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
    cal_parser = subparsers.add_parser("calibrate", help="Compute or view calibration data")
    cal_parser.add_argument(
        "--data-file", help="Path to historical resolution data (Parquet)"
    )

    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    commands = {
        "scan": cmd_scan,
        "match": cmd_match,
        "pairs": cmd_pairs,
        "serve": cmd_serve,
        "calibrate": cmd_calibrate,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
