#!/usr/bin/env python3
"""Basic arbscanner library example — run a single scan cycle and print results.

This example demonstrates how to use arbscanner as a Python library to:
  1. Instantiate Polymarket + Kalshi exchange clients.
  2. Load the cached matched-pairs file produced by the matching pipeline.
  3. Scan every matched pair for cross-platform arbitrage opportunities.
  4. Pretty-print the top 10 opportunities as a rich table.

Prerequisites:
  You must have a populated matched-pairs cache on disk. If you do not,
  run the matching pipeline first (e.g. `uv run arbscanner match`) to
  generate it. The cache location is configured in arbscanner.config.

Run:
  uv run python examples/basic_scan.py
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from arbscanner.engine import scan_all_pairs
from arbscanner.exchanges import create_exchanges
from arbscanner.matcher import load_cache


def main() -> int:
    """Run one scan cycle and print the top opportunities. Returns an exit code."""
    console = Console()

    # Step 1: Create read-only exchange clients for Polymarket and Kalshi.
    # These wrap pmxt connectors and share a rate limiter under the hood.
    console.print("[bold cyan]Creating exchange clients...[/bold cyan]")
    poly, kalshi = create_exchanges()

    # Step 2: Load the matched-pairs cache from disk. This file is produced
    # by the matching pipeline (embeddings + optional LLM confirmation) and
    # maps Polymarket markets to their Kalshi equivalents.
    console.print("[bold cyan]Loading matched-pairs cache...[/bold cyan]")
    cache = load_cache()

    if not cache.pairs:
        console.print(
            "[bold red]No matched pairs found in cache.[/bold red]\n"
            "Run the matching pipeline first to populate it, e.g.:\n"
            "  [yellow]uv run arbscanner match[/yellow]"
        )
        return 1

    console.print(f"Loaded [bold]{len(cache.pairs)}[/bold] matched pairs.\n")

    # Step 3: Scan every matched pair. The engine fetches all order books
    # in parallel, computes both arb directions (poly-YES/kalshi-NO and
    # poly-NO/kalshi-YES), subtracts fees, and returns opportunities
    # whose net edge exceeds the threshold, sorted by expected profit.
    console.print("[bold cyan]Scanning for arbitrage opportunities...[/bold cyan]")
    opportunities = scan_all_pairs(poly, kalshi, cache.pairs, threshold=0.01)

    # Step 4: Render the top 10 opportunities as a table.
    table = Table(
        title="Top 10 Arbitrage Opportunities",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Market", style="white", overflow="fold", max_width=40)
    table.add_column("Direction", style="cyan")
    table.add_column("Poly Price", justify="right", style="green")
    table.add_column("Kalshi Price", justify="right", style="green")
    table.add_column("Net Edge", justify="right", style="yellow")
    table.add_column("Expected Profit", justify="right", style="bold yellow")

    # Compact human-readable labels for the two arb directions.
    direction_labels = {
        "poly_yes_kalshi_no": "Poly YES / Kalshi NO",
        "poly_no_kalshi_yes": "Poly NO / Kalshi YES",
    }

    for opp in opportunities[:10]:
        table.add_row(
            opp.poly_title,
            direction_labels.get(opp.direction, opp.direction),
            f"${opp.poly_price:.3f}",
            f"${opp.kalshi_price:.3f}",
            f"{opp.net_edge * 100:.2f}%",
            f"${opp.expected_profit:.2f}",
        )

    console.print()
    console.print(table)

    # Step 5: Summary stats for the whole scan (not just the top 10).
    total_profit = sum(o.expected_profit for o in opportunities)
    console.print()
    console.print("[bold]Scan summary[/bold]")
    console.print(f"  Pairs scanned:           [bold]{len(cache.pairs)}[/bold]")
    console.print(f"  Opportunities found:     [bold]{len(opportunities)}[/bold]")
    console.print(f"  Total expected profit:   [bold green]${total_profit:.2f}[/bold green]")

    return 0


if __name__ == "__main__":
    # Catch Ctrl-C so the user gets a clean exit message instead of a traceback.
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        Console().print("\n[bold yellow]Scan interrupted by user.[/bold yellow]")
        raise SystemExit(130)
