"""Preflight environment check — ``arbscanner doctor``.

Verifies every runtime prerequisite a fresh checkout needs before
``arbscanner scan`` or ``arbscanner match`` will actually work. The
alternative — letting users hit a ``ModuleNotFoundError`` three minutes
into a scan, or a subprocess-spawn failure deep inside the first pmxt
call — burns trust. A single command that says exactly what's missing
and how to fix it saves the onboarding.

Exit codes:

* ``0`` — all checks passed, or the only failures are warnings (missing
  optional integrations like Telegram, empty matched-pairs cache, etc.)
* ``1`` — at least one hard prerequisite is missing. The scanner will
  not run until the reported failures are fixed.

The check list intentionally does **not** hit the network by default:
you should be able to run ``arbscanner doctor`` on a plane and still
get useful signal. ``--network`` opts into a pmxt round-trip against
each exchange.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.table import Table

from arbscanner.config import (
    CALIBRATION_DATA_DIR,
    DB_PATH,
    MATCHED_PAIRS_PATH,
    PROJECT_ROOT,
    settings,
)

#: Severity levels used by each :class:`CheckResult`. ``ok`` and ``info``
#: never affect the exit code; ``warn`` never affects the exit code
#: either but prints in yellow; ``fail`` is the only severity that makes
#: ``doctor`` exit non-zero.
Severity = str  # "ok" | "info" | "warn" | "fail"


@dataclass
class CheckResult:
    """Outcome of a single preflight check.

    ``fix`` is surfaced verbatim to the user when severity is ``warn`` or
    ``fail``, so it should read as an imperative ("run X", "set Y") — the
    point of ``doctor`` is that the next step is obvious.
    """

    name: str
    severity: Severity
    message: str
    fix: str = ""


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_python_version() -> CheckResult:
    """pyproject.toml pins ``requires-python = ">=3.12,<3.13"``.

    The 3.13+ upper bound exists because torch 2.2.2 — which is itself
    pinned for macOS x86_64 wheel availability — only ships cp312
    wheels. Running the project under 3.13 or 3.14 will fail during
    ``uv sync`` with a "no wheel for current platform" error on torch.
    We surface that as a ``fail`` here so the signal lands in ``doctor``
    rather than mid-sync.
    """
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 12):
        return CheckResult(
            name="python",
            severity="fail",
            message=f"Python {major}.{minor} detected; need 3.12.x",
            fix="Install Python 3.12: `uv python install 3.12` and `uv sync`",
        )
    if (major, minor) >= (3, 13):
        return CheckResult(
            name="python",
            severity="fail",
            message=(
                f"Python {major}.{minor} detected; project requires 3.12.x "
                "(torch 2.2.2 only ships cp312 wheels)"
            ),
            fix="Run `uv python install 3.12` and re-run `uv sync`",
        )
    return CheckResult(
        name="python",
        severity="ok",
        message=f"Python {major}.{minor}.{sys.version_info.micro}",
    )


def check_pmxt() -> CheckResult:
    """``pmxt`` is the Python wrapper around the Node sidecar.

    We use :func:`importlib.util.find_spec` instead of a bare ``import``
    so a missing ``pmxt`` doesn't cascade into a partial import state.
    """
    spec = importlib.util.find_spec("pmxt")
    if spec is None:
        return CheckResult(
            name="pmxt",
            severity="fail",
            message="pmxt Python package not importable",
            fix="Run `uv sync` (or `pip install pmxt`) in the project root",
        )
    try:
        import pmxt  # noqa: F401

        version = getattr(pmxt, "__version__", "unknown")
    except Exception as exc:
        return CheckResult(
            name="pmxt",
            severity="fail",
            message=f"pmxt import raised {type(exc).__name__}: {exc}",
            fix="Reinstall pmxt: `uv sync --refresh-package pmxt`",
        )
    return CheckResult(name="pmxt", severity="ok", message=f"pmxt {version} importable")


def check_node() -> CheckResult:
    """``pmxt`` shells out to Node. We need Node 18+ on ``PATH``."""
    node = shutil.which("node")
    if not node:
        return CheckResult(
            name="node",
            severity="fail",
            message="`node` not on PATH",
            fix="Install Node.js 18+ (https://nodejs.org) and ensure it is on PATH",
        )
    try:
        out = subprocess.run(
            [node, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return CheckResult(
            name="node",
            severity="fail",
            message=f"`node --version` failed: {exc}",
            fix="Reinstall Node.js 18+ (https://nodejs.org)",
        )
    raw = out.stdout.strip().lstrip("v")
    try:
        major = int(raw.split(".")[0])
    except (ValueError, IndexError):
        return CheckResult(
            name="node",
            severity="warn",
            message=f"Could not parse Node version {out.stdout.strip()!r}",
            fix="Verify `node --version` prints a semver string starting with 'v'",
        )
    if major < 18:
        return CheckResult(
            name="node",
            severity="fail",
            message=f"Node.js v{raw} detected; need 18+",
            fix="Upgrade Node.js to 18 or newer",
        )
    return CheckResult(name="node", severity="ok", message=f"Node.js v{raw}")


def _warn_pmxtjs_off_path(candidate: Path) -> CheckResult:
    bin_dir = candidate.parent
    return CheckResult(
        name="pmxtjs",
        severity="warn",
        message=f"pmxtjs installed at {candidate} but not on shell PATH",
        fix=(
            f'Add the npm global bin to PATH: `export PATH="{bin_dir}:$PATH"` '
            "(then add that line to ~/.zshrc or ~/.bashrc)"
        ),
    )


def check_pmxtjs() -> CheckResult:
    """The Node sidecar binary ``pmxtjs`` must be globally installed.

    Discovery order (first match wins):

    1. Shell ``PATH`` — the common case.
    2. ``npm config get prefix`` / ``bin/`` — covers standard global installs.
    3. ``npm ls -g pmxtjs --parseable`` — resolves the actual package dir,
       which lets us derive the bin path for nvm/volta-managed installations
       where the prefix changes per Node version.
    4. Common macOS install locations (homebrew, custom npm-global prefix).
    5. pmxt import probe — if pmxt itself can import, the sidecar is
       accessible to the scanner regardless of shell PATH. Downgrade to
       ``warn`` so the user knows to fix PATH but scanning won't be blocked.

    ``warn`` means scanning works; ``fail`` means pmxtjs is genuinely missing.
    """
    bin_path = shutil.which("pmxtjs")
    if bin_path:
        return CheckResult(name="pmxtjs", severity="ok", message=f"pmxtjs at {bin_path}")

    npm = shutil.which("npm")
    if npm:
        # 2. npm config get prefix
        try:
            out = subprocess.run(
                [npm, "config", "get", "prefix"],
                check=True, capture_output=True, text=True, timeout=5,
            )
            candidate = Path(out.stdout.strip()) / "bin" / "pmxtjs"
            if candidate.exists():
                return _warn_pmxtjs_off_path(candidate)
        except (subprocess.SubprocessError, OSError):
            pass

        # 3. npm ls -g --parseable: works for nvm/volta where the prefix is
        #    per-version and not the same as the shell PATH entry.
        try:
            ls = subprocess.run(
                [npm, "ls", "-g", "pmxtjs", "--parseable", "--depth=0"],
                capture_output=True, text=True, timeout=10,
            )
            for line in ls.stdout.strip().splitlines():
                pkg_dir = Path(line.strip())
                # npm returns the package dir, e.g.:
                #   nvm:  ~/.nvm/versions/node/v20/lib/node_modules/pmxtjs
                #   std:  /usr/local/lib/node_modules/pmxtjs
                # The global bin lives at <version-root>/bin/ or <prefix>/bin/.
                for candidate in [
                    pkg_dir.parent.parent.parent / "bin" / "pmxtjs",  # nvm layout
                    pkg_dir.parent.parent / "bin" / "pmxtjs",         # standard layout
                    pkg_dir.parent / ".bin" / "pmxtjs",               # node_modules/.bin
                ]:
                    if candidate.exists():
                        return _warn_pmxtjs_off_path(candidate)
            # Package listed but binary location not resolved — still a warn.
            if ls.returncode == 0 and "pmxtjs" in ls.stdout:
                return CheckResult(
                    name="pmxtjs",
                    severity="warn",
                    message="pmxtjs is globally installed but binary is not on shell PATH",
                    fix=(
                        'Run: export PATH="$(npm config get prefix)/bin:$PATH"  '
                        "then add that line to ~/.zshrc"
                    ),
                )
        except (subprocess.SubprocessError, OSError):
            pass

    # 4. Common macOS install locations.
    for candidate in [
        Path("/opt/homebrew/bin/pmxtjs"),       # Apple Silicon homebrew
        Path("/usr/local/bin/pmxtjs"),           # Intel homebrew / direct install
        Path.home() / ".npm-global" / "bin" / "pmxtjs",  # custom npm prefix
    ]:
        if candidate.exists():
            return _warn_pmxtjs_off_path(candidate)

    # 5. If pmxt itself imports cleanly, the sidecar is reachable by the
    #    scanner process — shell PATH is the only thing missing.
    try:
        import importlib.util as _ilu
        if _ilu.find_spec("pmxt") is not None:
            import pmxt as _pmxt  # noqa: F401
            return CheckResult(
                name="pmxtjs",
                severity="warn",
                message=(
                    "pmxtjs not on shell PATH but pmxt imports successfully — "
                    "scanning will work; shell invocations of pmxtjs won't"
                ),
                fix=(
                    'Run: export PATH="$(npm config get prefix)/bin:$PATH"  '
                    "then add that line to ~/.zshrc or ~/.bashrc"
                ),
            )
    except Exception:
        pass

    return CheckResult(
        name="pmxtjs",
        severity="fail",
        message="`pmxtjs` Node sidecar not found",
        fix="Run `npm install -g pmxtjs` and ensure the npm global bin is on PATH",
    )


def check_env_file() -> CheckResult:
    """``.env`` is optional — ``python-dotenv`` silently no-ops if absent.

    We surface it as ``info``/``warn`` rather than ``fail`` so users who
    export env vars some other way (systemd, docker-compose) aren't
    blocked.
    """
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        return CheckResult(name=".env", severity="ok", message=f".env present at {env_path}")
    return CheckResult(
        name=".env",
        severity="warn",
        message=".env file missing",
        fix="Copy the template: `cp .env.example .env` and fill in keys you have",
    )


def check_anthropic_key() -> CheckResult:
    """No key is OK — the matcher's no-key fallback handles it."""
    if settings.anthropic_api_key:
        return CheckResult(
            name="anthropic_key",
            severity="ok",
            message="ANTHROPIC_API_KEY set — LLM match confirmation enabled",
        )
    return CheckResult(
        name="anthropic_key",
        severity="warn",
        message=(
            "ANTHROPIC_API_KEY not set — matcher will keep only high-confidence "
            f"(similarity >= {settings.llm_confirm_high:.2f}) pairs"
        ),
        fix="Set ANTHROPIC_API_KEY in .env to enable LLM-adjudicated matches",
    )


def check_matched_pairs() -> CheckResult:
    """``matched_pairs.json`` is empty on first run — not a hard fail."""
    path = MATCHED_PAIRS_PATH
    if not path.exists():
        return CheckResult(
            name="matched_pairs",
            severity="warn",
            message="No matched_pairs.json — first scan will trigger a full match",
            fix="Run `arbscanner match` to build the cache explicitly",
        )
    try:
        import json

        data = json.loads(path.read_text())
        count = len(data.get("pairs", []))
    except Exception as exc:
        return CheckResult(
            name="matched_pairs",
            severity="fail",
            message=f"matched_pairs.json unreadable: {type(exc).__name__}: {exc}",
            fix="Delete the file and re-run `arbscanner match`",
        )
    if count == 0:
        return CheckResult(
            name="matched_pairs",
            severity="warn",
            message="matched_pairs.json exists but contains 0 pairs",
            fix="Run `arbscanner match` to populate it",
        )
    return CheckResult(
        name="matched_pairs",
        severity="ok",
        message=f"{count} matched pair(s) cached",
    )


def check_database() -> CheckResult:
    """SQLite must be writable by whoever runs the scanner."""
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("SELECT 1")
        conn.close()
    except Exception as exc:
        return CheckResult(
            name="database",
            severity="fail",
            message=f"Cannot open/write SQLite at {DB_PATH}: {exc}",
            fix=f"Check filesystem permissions on {DB_PATH.parent}",
        )
    return CheckResult(name="database", severity="ok", message=f"SQLite writable at {DB_PATH}")


def check_calibration_data() -> CheckResult:
    """Calibration is optional — default curves ship baked in."""
    if not CALIBRATION_DATA_DIR.exists():
        return CheckResult(
            name="calibration",
            severity="info",
            message="No calibration data ingested — using built-in default profiles",
            fix="(optional) Run `arbscanner calibrate --ingest-live` for live-data curves",
        )
    data_files = [
        p
        for p in CALIBRATION_DATA_DIR.iterdir()
        if p.suffix in {".parquet", ".json"} and p.is_file()
    ]
    if not data_files:
        return CheckResult(
            name="calibration",
            severity="info",
            message="Calibration dir empty — using built-in default profiles",
            fix="(optional) Run `arbscanner calibrate --ingest-live` for live-data curves",
        )
    return CheckResult(
        name="calibration",
        severity="ok",
        message=f"{len(data_files)} calibration file(s) in {CALIBRATION_DATA_DIR}",
    )


def check_alert_sinks() -> CheckResult:
    """Alerts are optional. Flag a specific foot-gun: tier=free silences them."""
    telegram = bool(settings.telegram_bot_token and settings.telegram_chat_id)
    discord = bool(settings.discord_webhook_url)
    sinks = []
    if telegram:
        sinks.append("telegram")
    if discord:
        sinks.append("discord")

    if not sinks:
        return CheckResult(
            name="alerts",
            severity="info",
            message="No alert sinks configured (scan will log to terminal only)",
        )

    sink_str = "+".join(sinks)
    if settings.tier == "free":
        return CheckResult(
            name="alerts",
            severity="warn",
            message=f"{sink_str} configured but ARBSCANNER_TIER=free silently drops alerts",
            fix="Set ARBSCANNER_TIER=pro to deliver alerts",
        )
    return CheckResult(name="alerts", severity="ok", message=f"{sink_str} enabled")


def check_network_pmxt() -> CheckResult:
    """Opt-in round-trip through pmxt — the only network-hitting check."""
    try:
        import pmxt  # noqa: F401
    except Exception as exc:
        return CheckResult(
            name="network",
            severity="fail",
            message=f"pmxt import failed: {exc}",
            fix="See the pmxt check above",
        )

    try:
        from arbscanner.exchanges import create_exchanges

        poly, kalshi = create_exchanges()
        # One market per exchange is enough to prove the sidecar is alive.
        poly.fetch_markets_paginated(limit=1)
        kalshi.fetch_markets_paginated(limit=1)
    except Exception as exc:
        return CheckResult(
            name="network",
            severity="fail",
            message=f"pmxt round-trip failed: {type(exc).__name__}: {exc}",
            fix="Verify network, that pmxtjs is installed, and Polymarket/Kalshi are reachable",
        )
    return CheckResult(
        name="network",
        severity="ok",
        message="Polymarket + Kalshi reachable via pmxt",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


#: Ordered list of checks. Ordering matters — later checks often assume
#: earlier ones passed (e.g., ``check_network_pmxt`` assumes ``pmxt`` imports).
OFFLINE_CHECKS: list[Callable[[], CheckResult]] = [
    check_python_version,
    check_pmxt,
    check_node,
    check_pmxtjs,
    check_env_file,
    check_anthropic_key,
    check_matched_pairs,
    check_database,
    check_calibration_data,
    check_alert_sinks,
]


def run_all_checks(include_network: bool = False) -> list[CheckResult]:
    """Run every registered check in order and collect results.

    Each check is isolated in its own try/except at this layer so one
    unexpectedly-raising check can't hide the rest.
    """
    checks = list(OFFLINE_CHECKS)
    if include_network:
        checks.append(check_network_pmxt)

    results: list[CheckResult] = []
    for check in checks:
        try:
            results.append(check())
        except Exception as exc:  # noqa: BLE001 — defensive, unreachable in normal flow
            results.append(
                CheckResult(
                    name=check.__name__.removeprefix("check_"),
                    severity="fail",
                    message=f"Check raised {type(exc).__name__}: {exc}",
                    fix="File a bug — doctor checks should never raise",
                )
            )
    return results


_SEVERITY_STYLE = {
    "ok": ("[green]OK[/green]", "green"),
    "info": ("[cyan]info[/cyan]", "cyan"),
    "warn": ("[yellow]warn[/yellow]", "yellow"),
    "fail": ("[red]FAIL[/red]", "red"),
}


def render(results: list[CheckResult], console: Console | None = None) -> None:
    """Pretty-print a table of results plus a fix punch list."""
    console = console or Console()

    table = Table(show_header=True, header_style="bold", show_lines=False)
    table.add_column("Check", style="bold", no_wrap=True)
    table.add_column("Status")
    table.add_column("Details")

    for r in results:
        label, _ = _SEVERITY_STYLE.get(r.severity, (r.severity, ""))
        table.add_row(r.name, label, r.message)

    console.print(table)

    fixes = [r for r in results if r.severity in {"warn", "fail"} and r.fix]
    if fixes:
        console.print("\n[bold]Next steps:[/bold]")
        for r in fixes:
            marker = "[red]✗[/red]" if r.severity == "fail" else "[yellow]![/yellow]"
            console.print(f"  {marker} [bold]{r.name}[/bold]: {r.fix}")


def exit_code(results: list[CheckResult]) -> int:
    """``1`` if anything failed; ``0`` otherwise (warnings don't fail CI)."""
    return 1 if any(r.severity == "fail" for r in results) else 0
