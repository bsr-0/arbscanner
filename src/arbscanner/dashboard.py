"""Rich-based terminal dashboard for live arb monitoring."""

import time
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from arbscanner.models import ArbOpportunity


def build_table(
    opportunities: list[ArbOpportunity],
    matched_pairs_count: int,
    last_refresh: datetime | None = None,
    paper_summary: dict | None = None,
) -> Table:
    """Build a Rich table displaying current arb opportunities."""
    table = Table(
        title="Arb Scanner",
        caption=_build_caption(
            len(opportunities), matched_pairs_count, last_refresh, paper_summary
        ),
        show_lines=True,
    )

    table.add_column("Market", style="cyan", max_width=40)
    table.add_column("Direction", style="white")
    table.add_column("Poly", justify="right")
    table.add_column("Kalshi", justify="right")
    table.add_column("Gross Edge", justify="right")
    table.add_column("Net Edge", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("$ Profit", justify="right")
    table.add_column("Calibration")

    for opp in opportunities:
        # Truncate title for display
        title = opp.poly_title
        if len(title) > 38:
            title = title[:35] + "..."

        direction = "P.Yes + K.No" if opp.direction == "poly_yes_kalshi_no" else "P.No + K.Yes"

        # Highlight strong edges in green
        net_style = "bold green" if opp.net_edge >= 0.01 else "yellow"

        table.add_row(
            title,
            direction,
            f"{opp.poly_price:.3f}",
            f"{opp.kalshi_price:.3f}",
            f"{opp.gross_edge:.1%}",
            Text(f"{opp.net_edge:.1%}", style=net_style),
            f"{opp.available_size:.0f}",
            Text(f"${opp.expected_profit:.2f}", style=net_style),
            _format_calibration(opp.calibration),
        )

    if not opportunities:
        table.add_row(
            "No opportunities found", "", "", "", "", "", "", "", "",
            style="dim",
        )

    return table


def _format_calibration(cal: dict | None) -> Text:
    """Render a compact calibration indicator for the terminal table."""
    if not cal:
        return Text("? unknown", style="dim")
    label = "REAL" if cal.get("edge_likely_real") else "noise"
    style = "bold green" if cal.get("edge_likely_real") else "yellow"
    category = cal.get("category", "?")
    bucket = cal.get("time_bucket", "?")
    avg = float(cal.get("avg_mispricing") or 0.0)
    return Text(f"{label} · {category}/{bucket}d · {avg:.1f}pt", style=style)


def _build_caption(
    opp_count: int,
    pairs_count: int,
    last_refresh: datetime | None,
    paper_summary: dict | None = None,
) -> str:
    """Build the table caption with stats."""
    refresh_str = last_refresh.strftime("%H:%M:%S UTC") if last_refresh else "never"
    caption = (
        f"Matched pairs: {pairs_count} | "
        f"Active opps: {opp_count} | "
        f"Last refresh: {refresh_str}"
    )
    if paper_summary is not None:
        caption += (
            f"\nPaper: bal=${paper_summary.get('balance', 0):.2f} "
            f"open={paper_summary.get('open_positions', 0)} "
            f"trades={paper_summary.get('total_trades', 0)} "
            f"pnl=${paper_summary.get('total_pnl', 0):.2f} "
            f"win={paper_summary.get('win_rate', 0) * 100:.1f}%"
        )
    return caption


def run_dashboard(
    scan_fn,
    interval: int = 30,
    paper_summary_fn=None,
) -> None:
    """Run the live dashboard, calling scan_fn() every interval seconds.

    scan_fn should return (opportunities, matched_pairs_count).
    paper_summary_fn, if provided, should return a paper trading summary dict
    (as produced by PaperTradingEngine.summary) to render in the caption.
    """
    console = Console()

    console.print("[bold]Starting arb scanner dashboard...[/bold]")
    console.print(f"Refresh interval: {interval}s | Press Ctrl+C to stop\n")

    with Live(console=console, refresh_per_second=1) as live:
        while True:
            try:
                opportunities, pairs_count = scan_fn()
                now = datetime.now(timezone.utc)
                paper_summary = paper_summary_fn() if paper_summary_fn else None
                table = build_table(opportunities, pairs_count, now, paper_summary)
                live.update(table)
                time.sleep(interval)
            except KeyboardInterrupt:
                console.print("\n[bold]Scanner stopped.[/bold]")
                break
            except Exception as e:
                console.print(f"[red]Scan error: {e}[/red]")
                time.sleep(interval)
