# Contributing to arbscanner

Thanks for your interest in contributing to **arbscanner** — a cross-platform
prediction market arbitrage scanner that continuously monitors overlapping
markets on Polymarket and Kalshi and surfaces the best opportunities with
fee-aware, calibration-aware context.

This doc explains how to get set up, where things live, and how to land a good
pull request. If anything here is unclear, open an issue and we'll fix it.

## Code of Conduct

We follow a standard "be kind, be constructive, assume good faith" approach.
A formal `CODE_OF_CONDUCT.md` will be added in the future (placeholder for now).
Harassment, personal attacks, and bad-faith behavior will not be tolerated.

## Development Environment

arbscanner targets **Python 3.12+** and uses [`uv`](https://github.com/astral-sh/uv)
for dependency management. The data layer depends on
[`pmxt`](https://www.npmjs.com/package/pmxtjs), which is a Node package installed
globally.

### One-time setup

```bash
# 1. Clone
git clone https://github.com/<your-fork>/arbscanner.git
cd arbscanner

# 2. Install Python deps into a managed venv (includes the dev group
#    automatically: pytest + ruff, via PEP 735 `[dependency-groups]`).
uv sync

# 4. Install pmxt (Node-based exchange connector CLI)
npm install -g pmxtjs
```

After that, you should be able to run:

```bash
uv run arbscanner --help
```

### Environment variables

Copy any example `.env` file (if present) and fill in credentials for the
exchanges, Anthropic API, Telegram/Discord webhooks, and Stripe if you're
working on those subsystems. The `arbscanner.config` module is the single
source of truth for settings.

## Project Structure

All code lives under `src/arbscanner/`:

- `cli.py` — `argparse`-based CLI entry point with subcommands
  (`scan`, `match`, `pairs`, `serve`, `calibrate`).
- `config.py` — Centralized settings loaded from env vars (`settings` object).
- `models.py` — Dataclasses for `Market`, `OrderBook`, `MatchedPair`,
  `Opportunity`, etc. All cross-module data structures live here.
- `exchanges.py` — Thin wrappers around `pmxt` for Polymarket and Kalshi:
  `create_exchanges()`, `fetch_all_markets()`, order book fetches.
- `matcher.py` — Market matching pipeline: title normalization, embedding
  similarity, LLM confirmation, and the on-disk `matched_pairs.json` cache.
- `engine.py` — Arb calculation: pulls order books for matched pairs,
  computes gross and net edges, and ranks opportunities.
- `alerts.py` — Telegram/Discord webhook delivery for opportunities above
  the alert threshold.
- `dashboard.py` — Rich/Textual terminal dashboard for `arbscanner scan`.
- `web.py` — FastAPI app serving the JSON API and HTML dashboard.
- `db.py` — SQLite/DuckDB connection helpers and opportunity logging.
- `calibration.py` — Historical resolution ingestion and category-level
  calibration curves (the differentiator layer).
- `utils.py` — Small shared helpers.
- `templates/` — Jinja2 templates for the web dashboard.

## Coding Conventions

- **Python version**: 3.12+. You may use modern syntax (`match`, PEP 695
  generics, `X | None`, etc.).
- **Type hints**: Required on all new functions, methods, and public
  attributes. Prefer `from __future__ import annotations` if it keeps things
  clean, but don't mix styles within a module.
- **Data models**: Use `@dataclass` (or `@dataclass(frozen=True, slots=True)`
  where appropriate) for any structured data that crosses module boundaries.
  Put them in `models.py`.
- **Logging**: Use the standard `logging` module with module-level loggers:

  ```python
  import logging
  logger = logging.getLogger(__name__)
  ```

  Do **not** use `print()` for diagnostics in library code. `rich.console`
  is fine for user-facing CLI output in `cli.py` / `dashboard.py`.
- **Linting/formatting**: `ruff` is configured in `pyproject.toml` with a
  **100-character line length** and `target-version = "py312"`. Run
  `uv run ruff check .` and `uv run ruff format .` before pushing.
- **Imports**: Absolute imports from `arbscanner.*`. Group stdlib / third-party
  / local; `ruff` will sort them for you.
- **Docstrings**: Short one-liners on public functions are enough. Reserve
  longer docstrings for non-obvious algorithms (e.g., the matcher).

## Commits and Branches

### Commit messages

We use a lightweight, conventional-ish style. Start with an imperative verb:

- `Add calibration curves for entertainment markets`
- `Fix Kalshi fee calculation for sub-dollar prices`
- `Refactor matcher cache to use DuckDB`
- `Update README with pmxt install instructions`

Keep the subject under ~72 characters. Add a body paragraph if the *why*
isn't obvious from the diff.

### Branch names

- `feature/<short-slug>` — new functionality (e.g. `feature/discord-alerts`)
- `fix/<short-slug>` — bug fixes (e.g. `fix/kalshi-fee-rounding`)
- `docs/<short-slug>` — documentation only (e.g. `docs/contributing`)

## Running Tests

```bash
uv run pytest
```

Add tests alongside any new logic in `tests/`. Favor small, fast unit tests
over end-to-end ones that hit real exchanges — mock `pmxt` calls where
possible. For the matcher, prefer deterministic tests that feed in fixed
strings rather than live embedding model calls.

## How-to Recipes

### Add a new CLI subcommand

All subcommands live in `src/arbscanner/cli.py`. Follow the existing pattern:

1. Write a `cmd_<name>(args: argparse.Namespace) -> None` handler function.
2. Register a parser block inside `main()` via
   `subparsers.add_parser("<name>", help="...")` and add any flags.
3. Register the handler in the `commands = { ... }` dispatch dict at the
   bottom of `main()`.
4. Use the module-level `console` for user-facing output and delegate real
   work to the relevant module (`engine`, `matcher`, `calibration`, etc.).

### Add a new exchange

Today only Polymarket and Kalshi are wired up. To add a third venue (say,
Limitless):

1. In `models.py`, make sure `Market`, `OrderBook`, and `Opportunity` have
   everything the new venue needs (e.g. a `venue` enum or string field).
2. In `exchanges.py`, add a constructor/wrapper for the new exchange via
   `pmxt`, and extend `create_exchanges()` / `fetch_all_markets()` so
   callers can ask for it.
3. In `matcher.py`, extend `run_matching()` to consider the new venue as
   either a source or target (and update the cache schema version if you
   change the on-disk format).
4. In `engine.py`, generalize the pairwise arb calculation so it doesn't
   assume exactly two venues. Sub-in fee rates per venue.
5. Update `cli.py` help strings and `web.py` responses as needed.

### Improve the matcher

The matcher is the hardest, most valuable part of the codebase. Good
contributions here:

- Improve `normalize_title()` in `matcher.py` — abbreviations, ticker
  decoding (Kalshi's `KX*` prefixes), date normalization, removing boilerplate.
- Tune the embedding similarity threshold for candidate generation. Higher
  threshold = fewer LLM calls but more missed matches.
- Tighten the LLM confirmation prompt to reduce false positives on
  near-miss markets (e.g. same topic, different resolution dates).
- Add regression tests with real Polymarket/Kalshi title pairs — both
  positive and negative cases.

If you touch the cache file format, bump the version and handle migration
so existing users don't have to rematch from scratch.

## Pull Request Checklist

Before requesting review, please confirm:

- [ ] `uv run pytest` passes.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` are clean.
- [ ] No new runtime dependencies added without prior discussion in an
      issue (dev-only deps are more flexible but still mention them).
- [ ] User-facing changes (CLI flags, API responses, dashboard columns)
      are reflected in the README or other relevant docs.
- [ ] New modules/functions have type hints and a module-level logger if
      they do any logging.
- [ ] Commit messages follow the style above and the branch is named
      `feature/*`, `fix/*`, or `docs/*`.
- [ ] PR description explains the *why*, not just the *what*, and links
      any related issues.

## Reporting Bugs and Requesting Features

- **Bugs**: open a GitHub issue with reproduction steps, expected vs. actual
  behavior, your Python/`pmxt`/OS versions, and a redacted log snippet if
  relevant. If it's a matcher false-positive/negative, include the exact
  Polymarket and Kalshi titles.
- **Feature requests**: open a GitHub issue tagged `enhancement` describing
  the use case first, then the proposed solution. For bigger changes
  (new exchange, schema migration, new subcommand), please start a
  discussion before writing the code so we can align on the design.

Thanks for helping build arbscanner. Happy arbing.
