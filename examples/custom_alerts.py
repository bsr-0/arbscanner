#!/usr/bin/env python3
"""Custom alerter example for arbscanner.

Demonstrates how to extend arbscanner with your own alert channels beyond
the built-in Telegram/Discord integrations. The extension point is a small
`Alerter` Protocol that any class can satisfy — no inheritance required.
Implement `send(opp) -> bool` and you're done.

Run it with:
    uv run python examples/custom_alerts.py
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx
from rich.console import Console
from rich.panel import Panel

from arbscanner.alerts import format_alert
from arbscanner.engine import scan_all_pairs
from arbscanner.exchanges import create_exchanges
from arbscanner.matcher import load_cache
from arbscanner.models import ArbOpportunity

console = Console()


# --- 1. The Alerter Protocol — the extension point ------------------------
# Using `typing.Protocol` means any object with a matching `send` method
# automatically satisfies the interface. No subclassing needed.
@runtime_checkable
class Alerter(Protocol):
    """Anything that can deliver an ArbOpportunity somewhere.

    Return True on success, False on failure. Implementations should never
    raise — log and return False instead so CompositeAlerter can continue.
    """

    def send(self, opp: ArbOpportunity) -> bool: ...


# --- 2. Three concrete alerters --------------------------------------------
class ConsoleAlerter:
    """Pretty-prints opportunities to stdout using rich."""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def send(self, opp: ArbOpportunity) -> bool:
        # Reuse the library's format_alert so the wording stays consistent.
        body = format_alert(opp)
        self._console.print(
            Panel(body, title="[bold green]Arb Opportunity[/bold green]", expand=False)
        )
        return True


class FileAlerter:
    """Appends each opportunity as a JSON line to a log file.

    Useful for later analysis, backtesting, or feeding another pipeline.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        # Make sure the parent exists so first-run writes don't explode.
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, opp: ArbOpportunity) -> bool:
        try:
            record = dataclasses.asdict(opp)
            # datetime isn't JSON-serializable out of the box.
            record["timestamp"] = opp.timestamp.isoformat()
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            return True
        except OSError as exc:
            console.print(f"[red]FileAlerter failed: {exc}[/red]")
            return False


class WebhookAlerter:
    """POSTs the opportunity as JSON to an arbitrary webhook URL.

    This is the generic escape hatch: point it at Slack, Zapier, n8n, your
    own service — anything that accepts an HTTP POST with a JSON body.
    """

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout

    def send(self, opp: ArbOpportunity) -> bool:
        payload = dataclasses.asdict(opp)
        payload["timestamp"] = opp.timestamp.isoformat()
        payload["message"] = format_alert(opp)
        try:
            resp = httpx.post(self.url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            console.print(f"[red]WebhookAlerter failed: {exc}[/red]")
            return False


# --- 3. CompositeAlerter — fan out to many channels -----------------------
class CompositeAlerter:
    """Dispatches each opportunity to every wrapped alerter.

    Returns True only if *all* sub-alerters succeeded. One broken channel
    never prevents the others from firing — failure isolation matters when
    you're paying for uptime.
    """

    def __init__(self, alerters: list[Alerter]) -> None:
        self.alerters = alerters

    def send(self, opp: ArbOpportunity) -> bool:
        results = [a.send(opp) for a in self.alerters]
        return all(results)


# --- 4. Wire it all together -----------------------------------------------
def main() -> int:
    console.print("[bold cyan]arbscanner custom alerts demo[/bold cyan]")

    # Step 1: set up exchanges and load the pre-built matched pair cache.
    # Same pattern the CLI uses in src/arbscanner/cli.py.
    poly, kalshi = create_exchanges()
    cache = load_cache()
    if not cache.pairs:
        console.print(
            "[yellow]No matched pairs found. Run `arbscanner match` first.[/yellow]"
        )
        return 1
    console.print(f"Loaded [bold]{len(cache.pairs)}[/bold] matched pairs")

    # Step 2: run a single scan cycle. scan_all_pairs already applies the
    # configured edge threshold and sorts by expected profit descending.
    console.print("Scanning...")
    opportunities = scan_all_pairs(poly, kalshi, cache.pairs)
    console.print(f"Found [bold]{len(opportunities)}[/bold] opportunities")

    # Step 3: build a composite alerter. Add a WebhookAlerter here to also
    # fire at Slack / Zapier / your own endpoint.
    alerter = CompositeAlerter(
        [
            ConsoleAlerter(console=console),
            FileAlerter(Path("alerts.log")),
            # WebhookAlerter("https://hooks.slack.com/services/XXX/YYY/ZZZ"),
        ]
    )

    # Step 4: dispatch opportunities above a 2% net edge.
    sent = 0
    for opp in opportunities:
        if opp.net_edge >= 0.02:
            if alerter.send(opp):
                sent += 1

    console.print(
        f"\n[bold green]Summary:[/bold green] dispatched {sent} alert(s) "
        f"from {len(opportunities)} opportunity/ies"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted — exiting.[/yellow]")
        sys.exit(130)
