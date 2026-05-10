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
from arbscanner.logging_config import setup_logging
from arbscanner.matcher import load_cache, run_matching

logger = logging.getLogger(__name__)

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

    # Prune pairs for resolved markets
    from arbscanner.matcher import prune_stale_pairs, save_cache as save_pairs_cache

    cache, pruned = prune_stale_pairs(cache)
    if pruned:
        save_pairs_cache(cache)
        console.print(f"[dim]Pruned {pruned} stale pairs (already resolved)[/dim]")

    console.print(f"Loaded {len(cache.pairs)} matched pairs (workers: {max_workers})")

    alerts_enabled = bool(settings.telegram_bot_token or settings.discord_webhook_url)
    if alerts_enabled:
        console.print(f"[green]Alerts enabled (threshold: {settings.alert_threshold:.1%})[/green]")

    rematch_every = args.rematch_every
    if rematch_every:
        console.print(f"[dim]Auto re-matching every {rematch_every} cycles[/dim]")

    paper_engine: PaperTradingEngine | None = None
    paper_threshold = args.paper_threshold
    if args.paper:
        paper_engine = PaperTradingEngine(initial_balance=args.paper_balance)
        console.print(
            f"[green]Paper trading enabled "
            f"(balance: ${args.paper_balance:.2f}, threshold: {paper_threshold:.1%})[/green]"
        )

    db_conn = get_connection()
    scan_count = 0

    def do_scan():
        nonlocal cache, scan_count
        scan_count += 1

        # Periodic re-matching to pick up new markets
        if rematch_every and scan_count % rematch_every == 0:
            try:
                new_poly = fetch_all_markets(poly, "Polymarket")
                new_kalshi = fetch_all_markets(kalshi, "Kalshi")
                cache = run_matching(new_poly, new_kalshi)
                cache, pruned = prune_stale_pairs(cache)
                if pruned:
                    save_pairs_cache(cache)
                logger.info(
                    "Auto re-match complete: %d pairs (%d pruned)",
                    len(cache.pairs), pruned,
                )
            except Exception:
                logger.debug("Auto re-match failed, continuing with existing pairs", exc_info=True)

        once_mode = getattr(args, "once", False)
        if once_mode:
            opps = scan_all_pairs(
                poly, kalshi, cache.pairs, threshold=threshold, max_workers=max_workers,
                chunk_size=args.chunk_size,
                on_opportunities=lambda batch: log_opportunities(db_conn, batch),
            )
        else:
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

    if getattr(args, "once", False):
        try:
            opps, pair_count = do_scan()
            console.print(
                f"[green]Scan complete: {len(opps)} opportunit{'y' if len(opps) == 1 else 'ies'} "
                f"across {pair_count} pairs[/green]"
            )
        finally:
            db_conn.close()
            if paper_engine is not None:
                paper_engine.close()
        return

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
    from arbscanner.matcher import prune_stale_pairs, save_cache as save_pairs_cache

    if args.prune_only:
        cache = load_cache()
        cache, pruned = prune_stale_pairs(cache)
        if pruned:
            save_pairs_cache(cache)
            console.print(f"[green]Pruned {pruned} stale pairs. {len(cache.pairs)} remaining.[/green]")
        else:
            console.print(f"[dim]No stale pairs to prune. {len(cache.pairs)} active.[/dim]")
        return

    console.print("[bold]Initializing exchanges...[/bold]")
    poly, kalshi = create_exchanges()

    # Prune stale pairs before fetching new markets
    existing_cache = load_cache()
    existing_cache, pruned = prune_stale_pairs(existing_cache)
    if pruned:
        save_pairs_cache(existing_cache)
        console.print(f"[dim]Pruned {pruned} stale pairs before matching[/dim]")

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
        ingest_kalshi_direct,
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
        poly, _kalshi = create_exchanges()
        poly_count = ingest_from_exchange(poly, "Polymarket", limit=args.limit)
        # Use direct Kalshi API instead of pmxt (pmxt ignores status field)
        kalshi_count = ingest_kalshi_direct(limit=args.limit)
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


def cmd_execute(args: argparse.Namespace) -> None:
    """Run the execution pipeline against a logged opportunity.

    By default runs in dry-run mode (no real orders). Pass ``--live`` to
    place real orders via pmxt; this requires exchange credentials in env vars
    and an explicit double-confirmation from the operator.
    """
    from arbscanner.exchanges import CredentialError, create_authenticated_exchanges, validate_credentials
    from arbscanner.execution import (
        PlanRejection,
        execute_plan,
        format_execution_report,
        get_connection as get_exec_connection,
        log_execution,
        plan_execution,
    )
    from arbscanner.matcher import load_cache

    live = getattr(args, "live", False)

    opp = _load_opportunity(args.opportunity_id)
    if opp is None:
        console.print(
            f"[red]No logged opportunity with id={args.opportunity_id}[/red]"
        )
        sys.exit(1)

    # Look up the pair so we can pass outcome IDs into the planner.
    cache = load_cache()
    pair = None
    for p in cache.pairs:
        if (
            p.poly_market_id == opp.poly_market_id
            and p.kalshi_market_id == opp.kalshi_market_id
        ):
            pair = p
            break
    if pair is None:
        console.print(
            f"[yellow]Matched-pair cache has no entry for "
            f"{opp.poly_market_id}::{opp.kalshi_market_id}. "
            f"Run `arbscanner match` to refresh.[/yellow]"
        )

    mode_label = "[bold red]LIVE[/bold red]" if live else "[bold]dry-run[/bold]"
    console.print(f"Planning {mode_label} execution for opportunity #{args.opportunity_id}")
    console.print(f"  Market: {opp.poly_title}")
    console.print(f"  Direction: {opp.direction}")
    console.print(f"  Logged prices: poly={opp.poly_price:.3f} kalshi={opp.kalshi_price:.3f}")

    if live:
        missing = validate_credentials()
        if missing:
            console.print(
                f"[red]Live execution requires credentials. "
                f"Missing env vars: {', '.join(missing)}[/red]"
            )
            sys.exit(1)
        try:
            poly_exchange, kalshi_exchange = create_authenticated_exchanges()
        except CredentialError as e:
            console.print(f"[red]Credential error: {e}[/red]")
            sys.exit(1)
    else:
        poly_exchange, kalshi_exchange = create_exchanges()

    console.print("  Re-fetching current order books...")

    plan_or_rejection = plan_execution(
        opp,
        poly_exchange,
        kalshi_exchange,
        pair,
        max_trade_usd=args.max_trade_usd,
        opportunity_id=args.opportunity_id,
    )

    if isinstance(plan_or_rejection, PlanRejection):
        console.print(
            f"[red]Plan rejected ({plan_or_rejection.status}):[/red] "
            f"{plan_or_rejection.reason}"
        )
        sys.exit(1)

    plan = plan_or_rejection
    console.print(
        f"\n[bold]Plan ready[/bold] "
        f"(size={plan.size:.2f}, cost=${plan.total_cost_usd:.2f}, "
        f"expected profit=${plan.expected_net_profit:.2f})"
    )

    if not args.yes:
        if live:
            console.print(
                "\n[bold red]WARNING: LIVE EXECUTION MODE.[/bold red] "
                "Real orders will be placed and capital will be at risk."
            )
            confirm1 = input("Type 'LIVE' to confirm: ").strip()
            if confirm1 != "LIVE":
                console.print("[dim]Aborted.[/dim]")
                return
            confirm2 = input("Are you sure? [y/N]: ").strip().lower()
            if confirm2 not in ("y", "yes"):
                console.print("[dim]Aborted.[/dim]")
                return
        else:
            console.print(
                "\n[bold yellow]About to run dry-run simulation. "
                "No real orders will be placed.[/bold yellow]"
            )
            confirm = input("Proceed? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                console.print("[dim]Aborted by user.[/dim]")
                return

    result = execute_plan(
        plan,
        poly_exchange=poly_exchange,
        kalshi_exchange=kalshi_exchange,
        simulate_leg2_failure=args.simulate_leg2_failure,
        dry_run=not live,
    )

    conn = get_exec_connection()
    log_execution(conn, result)
    conn.close()

    console.print()
    console.print(format_execution_report(result))

    # Persist to execution_log (same SQLite DB as opportunities).
    conn = get_exec_connection()
    try:
        row_id = log_execution(conn, result)
        console.print(f"\n[dim]Logged as execution_log.id={row_id}[/dim]")
    finally:
        conn.close()

    if result.result not in ("success",):
        sys.exit(1)


def cmd_export(args: argparse.Namespace) -> None:
    """Export scanner data to static JSON for the GitHub Pages dashboard."""
    from pathlib import Path

    from arbscanner.export import export_dashboard_data

    output = Path(args.output) if args.output else None
    path = export_dashboard_data(
        hours=args.hours, min_edge=args.min_edge, limit=args.limit, output_path=output
    )
    console.print(f"[green]Exported dashboard data to {path}[/green]")


def cmd_site(args: argparse.Namespace) -> None:
    """Build the static GitHub Pages site."""
    from pathlib import Path

    from arbscanner.site import build_pages_site, validate_pages_site

    output_dir = Path(args.output_dir) if args.output_dir else None
    if args.verify_only:
        result = validate_pages_site(
            output_dir=output_dir,
            max_data_age_minutes=args.max_data_age_minutes,
        )
        console.print(
            "[green]Validated static site:[/green] "
            f"{result.data_path} generated at {result.generated_at.isoformat()}"
        )
        return

    result = build_pages_site(
        hours=args.hours,
        min_edge=args.min_edge,
        limit=args.limit,
        output_dir=output_dir,
    )
    console.print(
        "[green]Built static site:[/green] "
        f"{result.index_path} and {result.data_path}"
    )


def cmd_odds(args: argparse.Namespace) -> None:
    """Fetch and display sportsbook fair values for matched sports pairs."""
    from arbscanner.matcher import load_cache
    from arbscanner.odds import get_odds_client

    client = get_odds_client()
    if client is None:
        console.print(
            "[red]No odds API keys set. Add ODDS_API_KEY, ODDSPAPI_API_KEY, "
            "or ODDS_API_IO_KEY to .env[/red]"
        )
        sys.exit(1)

    console.print(f"[dim]Provider: {client.provider_name}[/dim]")

    if args.list_sports:
        console.print("[bold]Available sports:[/bold]")
        sports = client.fetch_available_sports()
        if not sports:
            console.print("[dim]  No sports returned (check API key)[/dim]")
        for s in sports:
            console.print(f"  {s}")
        return

    cache = load_cache()
    sports_pairs = [
        p for p in cache.pairs
        if p.category and p.category.lower() in ("sports", "sport")
    ]

    if not sports_pairs:
        console.print("[yellow]No sports pairs in matched_pairs.json[/yellow]")
        return

    console.print(f"[bold]{len(sports_pairs)} sports pairs, checking top {args.limit}...[/bold]\n")

    matched = 0
    for pair in sports_pairs[: args.limit]:
        fv = client.get_fair_value(pair)
        if fv:
            matched += 1
            console.print(
                f"  [green]{pair.poly_title}[/green]\n"
                f"    Fair value: {fv.implied_prob:.1%} "
                f"({fv.num_bookmakers} bookmakers, spread: {fv.spread:.1%})\n"
                f"    Event: {fv.home_team} vs {fv.away_team} ({fv.sport_key})"
            )
        else:
            console.print(f"  [dim]{pair.poly_title} — no odds data[/dim]")

    console.print(
        f"\n[bold]Matched {matched}/{min(len(sports_pairs), args.limit)} sports pairs[/bold]"
    )
    console.print(f"[dim]Active provider: {client.provider_name}[/dim]")
    if client.requests_remaining is not None:
        console.print(f"[dim]API requests remaining: {client.requests_remaining}[/dim]")


def cmd_doctor(args: argparse.Namespace) -> None:
    """Run the preflight environment checker and exit non-zero on hard failures."""
    from arbscanner.doctor import exit_code, render, run_all_checks

    results = run_all_checks(include_network=args.network)
    render(results, console=console)
    code = exit_code(results)
    if code != 0:
        sys.exit(code)


def cmd_backtest(args: argparse.Namespace) -> None:
    """Replay logged opportunities against historical resolved-market outcomes."""
    from datetime import datetime

    from arbscanner.backtest import format_backtest_report, run_backtest

    start = datetime.fromisoformat(args.start) if args.start else None
    end = datetime.fromisoformat(args.end) if args.end else None

    console.print(
        "[bold]Running backtest[/bold] "
        f"(min_edge={args.min_edge:.1%}, initial_balance=${args.initial_balance:.2f})..."
    )

    result = run_backtest(
        start=start,
        end=end,
        min_edge=args.min_edge,
        initial_balance=args.initial_balance,
    )

    console.print(format_backtest_report(result))

    if result.total_opportunities == 0:
        console.print(
            "\n[yellow]No opportunities in the log. Run 'arbscanner scan' first.[/yellow]"
        )
    elif result.resolved == 0:
        console.print(
            "\n[yellow]No opportunities could be resolved against ingested markets. "
            "Run 'arbscanner calibrate --ingest-live' to populate resolutions.[/yellow]"
        )


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
        "--once",
        action="store_true",
        help="Run a single scan cycle and exit (no live dashboard; useful for CI/cron)",
    )
    scan_parser.add_argument(
        "--chunk-size",
        type=int,
        default=250,
        help="Pairs per batch in --once mode; logs incrementally so partial results survive timeout (default: 250)",
    )
    scan_parser.add_argument(
        "--rematch-every",
        type=int,
        default=0,
        help="Re-run market matching every N scan cycles to catch new markets (0 = disabled)",
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
        "--prune-only",
        action="store_true",
        help="Only remove resolved pairs from cache (no new matching)",
    )
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

    # execute command
    execute_parser = subparsers.add_parser(
        "execute",
        help="Run the execution pipeline against a logged opportunity (dry-run by default)",
    )
    execute_parser.add_argument(
        "opportunity_id",
        type=int,
        help="ID of the logged opportunity to execute",
    )
    execute_parser.add_argument(
        "--max-trade-usd",
        type=float,
        default=100.0,
        help="Per-trade USD notional cap (default: 100.0)",
    )
    execute_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt",
    )
    execute_parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Place real orders via pmxt (Phase A.2). Requires POLY_API_KEY, "
            "POLY_API_SECRET, POLY_PASSPHRASE, POLY_PRIVATE_KEY, KALSHI_API_KEY, "
            "KALSHI_PRIVATE_KEY in the environment. Operator will be prompted twice."
        ),
    )
    execute_parser.add_argument(
        "--simulate-leg2-failure",
        action="store_true",
        help="Force the second leg to reject (exercises the unwind path; dry-run only)",
    )

    # backtest command
    backtest_parser = subparsers.add_parser(
        "backtest",
        help="Replay logged opportunities against historical resolved outcomes",
    )
    backtest_parser.add_argument(
        "--start",
        help="ISO 8601 timestamp — only replay opportunities on or after this",
    )
    backtest_parser.add_argument(
        "--end",
        help="ISO 8601 timestamp — only replay opportunities strictly before this",
    )
    backtest_parser.add_argument(
        "--min-edge",
        type=float,
        default=0.0,
        help="Floor on net_edge: ignore opportunities below this (default: 0.0)",
    )
    backtest_parser.add_argument(
        "--initial-balance",
        type=float,
        default=10000.0,
        help="Starting paper balance for the replay (default: 10000)",
    )

    # export command
    export_parser = subparsers.add_parser(
        "export", help="Export scanner data to static JSON for GitHub Pages"
    )
    export_parser.add_argument(
        "--hours", type=int, default=24, help="Lookback window in hours (default: 24)"
    )
    export_parser.add_argument(
        "--min-edge", type=float, default=0.0, help="Min net edge filter"
    )
    export_parser.add_argument(
        "--limit", type=int, default=100, help="Max opportunities to export"
    )
    export_parser.add_argument(
        "--output", type=str, default=None, help="Output JSON path (default: docs/data.json)"
    )

    site_parser = subparsers.add_parser(
        "site", help="Build the static GitHub Pages site into docs/"
    )
    site_parser.add_argument(
        "--hours", type=int, default=24, help="Lookback window in hours (default: 24)"
    )
    site_parser.add_argument(
        "--min-edge", type=float, default=0.0, help="Min net edge filter"
    )
    site_parser.add_argument(
        "--limit", type=int, default=100, help="Max opportunities to export"
    )
    site_parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output directory (default: docs/)",
    )
    site_parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Validate an existing built site instead of rebuilding it",
    )
    site_parser.add_argument(
        "--max-data-age-minutes",
        type=int,
        default=30,
        help="Freshness threshold when using --verify-only (default: 30)",
    )

    # odds command
    odds_parser = subparsers.add_parser(
        "odds", help="Fetch sportsbook fair values for sports pairs"
    )
    odds_parser.add_argument(
        "--list-sports", action="store_true", help="List available sports from The Odds API"
    )
    odds_parser.add_argument(
        "--limit", type=int, default=20, help="Max pairs to check (default: 20)"
    )

    # doctor command
    doctor_parser = subparsers.add_parser(
        "doctor", help="Preflight: validate env, deps, sidecar, and data files"
    )
    doctor_parser.add_argument(
        "--network",
        action="store_true",
        help="Also probe Polymarket + Kalshi via pmxt (requires network)",
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
        "backtest": cmd_backtest,
        "execute": cmd_execute,
        "export": cmd_export,
        "site": cmd_site,
        "odds": cmd_odds,
        "doctor": cmd_doctor,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
