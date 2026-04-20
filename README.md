# arbscanner

> Continuous cross-platform prediction market arbitrage scanner with calibration-aware edge scoring.

**arbscanner** is a Python 3.12 CLI + web application that watches every overlapping market between Polymarket and Kalshi simultaneously, detects price discrepancies net of fees, and surfaces the best opportunities on a live terminal dashboard and web UI. Unlike existing tools that require you to hardcode specific market URLs or manually browse between platforms, arbscanner builds and maintains a matched-pair map automatically using sentence-transformer embeddings combined with a Claude LLM confirmation pass — solving the hardest part of cross-exchange arbitrage: reliably identifying that "Will the Fed cut rates in June?" on Polymarket is the same event as `KXFEDCUT-26JUN` on Kalshi.

Every candidate opportunity is scored against a historical calibration layer derived from resolved markets: arbs in categories and timeframes that have historically been mispriced get boosted, while arbs in high-efficiency regimes (e.g. liquid politics markets close to resolution) are flagged as probable stale quotes or execution risk. That calibration context — not the price comparator itself — is the moat.

---

## Key features

- **Automated market matching** across Polymarket and Kalshi using sentence-transformer embeddings + Anthropic Claude LLM confirmation for ambiguous pairs
- **Persistent match cache** (`matched_pairs.json`) so you never re-match the same pair twice
- **Exact fee-aware arb engine** subtracting realistic Kalshi (~1-2%) and Polymarket (0.1% taker) fees
- **Liquidity-aware sizing**: every opportunity reports minimum cross-platform size and expected dollar profit
- **Rich terminal dashboard** that refreshes on a configurable interval, highlighting net edges above threshold
- **FastAPI web dashboard** with a live-updating HTML table and JSON API
- **Telegram + Discord webhooks** for real-time alerts when edge crosses a threshold
- **SQLite opportunity log** for historical analysis and backtesting
- **Paper trading simulator** that auto-opens simulated positions on high-edge opportunities, tracks expected-vs-realized edge, and exposes a CLI + JSON API for the account
- **Calibration layer** powered by Jon Becker's historical dataset (Parquet) or live resolved-market ingestion, joined inline to every detected opportunity (terminal + web + `/api/opportunities`) so users can see "edge likely real" vs. "likely noise" at a glance
- **Parallel order-book fetches** with configurable worker pool for fast scans across hundreds of pairs
- **Stripe-ready** landing page for the paid tier (optional)

---

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python      | 3.12.x  | Pinned in `.python-version`; 3.13+ doesn't have torch 2.2.2 wheels. Use `uv python install 3.12` |
| Node.js     | 18+     | Required by the `pmxtjs` sidecar used by `pmxt` |
| uv          | latest  | Python package + project manager |
| Anthropic API key | —  | Only needed for LLM-assisted match confirmation |

The `pmxt` Python library shells out to a Node.js sidecar (`pmxtjs`) to talk to Polymarket and Kalshi. You must install it globally (or make it available on `PATH`) before running any scanner commands.

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/YOUR-ORG/arbscanner.git
cd arbscanner

# 2. Install Python 3.12 if you don't have it. `.python-version` pins
#    the project to 3.12.x, and `tool.uv.python-preference = "only-managed"`
#    forces uv to use its own interpreter (not conda, not system Python).
uv python install 3.12

# 3. Install Python deps (creates .venv automatically using 3.12)
uv sync

# 4. Install the Node.js sidecar required by pmxt
npm install -g pmxtjs

# 5. Copy the env template and fill in any keys you need
cp .env.example .env
$EDITOR .env
```

**Conda users**: `tool.uv.python-preference = "only-managed"` means `uv sync` ignores your active conda env. If you want to use your conda Python instead, pass `--python /path/to/python3.12` explicitly. Otherwise just let uv manage it.

**Intel Mac (`x86_64`) users**: PyTorch dropped macOS x86_64 wheels after 2.2.2, and the rest of the ML stack has moved on from it. The supported path for Intel Macs is Docker — `docker compose up --build` works out of the box. Native Intel-Mac installs are possible by pinning `torch<=2.2.2`, `numpy<2`, and `transformers<4.50` in your local `pyproject.toml`, but that path isn't shipped here.

The scanner runs in read-only mode by default — you only need exchange private keys if you plan to add execution later. `ANTHROPIC_API_KEY` is strongly recommended so the matcher can confirm ambiguous pairs.

---

## Quick start

A full end-to-end walkthrough from zero to a live dashboard:

```bash
# 0. Clone + install (see above)
git clone https://github.com/YOUR-ORG/arbscanner.git
cd arbscanner
uv sync
npm install -g pmxtjs
cp .env.example .env   # add ANTHROPIC_API_KEY

# 1. Verify the environment is good to go (fails fast with a fix-it list)
uv run arbscanner doctor

# 2. Build the matched-pair map (run once, then occasionally)
uv run arbscanner match

# 3. Inspect the matches the pipeline produced
uv run arbscanner pairs

# 4. Start the live terminal scanner (refreshes every 30s by default)
uv run arbscanner scan --interval 30 --threshold 0.01

# 5. In another terminal, start the FastAPI web dashboard
uv run arbscanner serve --port 8000
#    open http://localhost:8000/dashboard

# 6. (Optional) Ingest historical resolutions for calibration context
uv run arbscanner calibrate --ingest-live --limit 500
```

After step 3, any opportunity whose net edge exceeds the threshold will be highlighted in the terminal UI and (if `TELEGRAM_BOT_TOKEN` or `DISCORD_WEBHOOK_URL` are set) pushed to your alert channels. Every opportunity is also logged to SQLite for later analysis.

---

## CLI reference

The top-level command is `arbscanner` (installed as a script by `uv sync`). All subcommands support the global `-v/--verbose` flag for debug logging.

```bash
uv run arbscanner <command> [flags]
```

### `arbscanner doctor` — preflight check

Validates every runtime prerequisite a fresh checkout needs *before* you run `scan` or `match`. Prints a table of check results and a fix-it punch list, then exits non-zero if any hard prerequisite is missing.

| Flag | Default | Description |
|------|---------|-------------|
| `--network` | off | Also round-trip a single market fetch through pmxt against Polymarket + Kalshi |

Offline checks cover Python version, `pmxt` importability, Node.js and `pmxtjs` on `PATH`, the `.env` file, `ANTHROPIC_API_KEY`, the matched-pair cache, SQLite writability, calibration data, and alert sinks (including the free-tier-silences-alerts foot-gun).

Exit codes:
- `0` — all checks passed, or only warnings/info remain (e.g. empty cache on a fresh checkout).
- `1` — at least one hard prerequisite is missing; the scanner will not run until you address the reported items.

```bash
# Fast offline check (no network, no credentials needed)
uv run arbscanner doctor

# Also prove Polymarket + Kalshi are reachable via the Node sidecar
uv run arbscanner doctor --network
```

### `arbscanner scan` — live arb scanner

Runs the matching pipeline (if no cache exists), then continuously scans every matched pair, refreshes the Rich terminal dashboard, logs opportunities to SQLite, and fires Telegram/Discord alerts above the alert threshold.

| Flag | Default | Description |
|------|---------|-------------|
| `--interval` | `30` | Seconds between refreshes |
| `--threshold` | `0.01` | Minimum net edge (1%) for an opportunity to appear |
| `--max-workers` | from settings | Parallel workers for order-book fetches |
| `--paper` | off | Auto-open simulated paper trading positions for every new high-edge opportunity |
| `--paper-balance` | `10000` | Starting balance for the paper trading account (first run only) |
| `--paper-threshold` | `0.02` | Minimum net edge required to auto-open a paper position |

```bash
# Aggressive: 10s refresh, show everything above 0.5% net edge, 16 workers
uv run arbscanner scan --interval 10 --threshold 0.005 --max-workers 16

# Same, but also simulate trades at 2%+ net edge with a $25k starting bankroll
uv run arbscanner scan --paper --paper-balance 25000 --paper-threshold 0.02
```

### `arbscanner match` — build the matched-pair map

Fetches all binary markets from Polymarket and Kalshi, runs the embedding-similarity + LLM confirmation pipeline, and writes the result to the pair cache.

| Flag | Default | Description |
|------|---------|-------------|
| `--rematch` | off | Ignore the existing cache and re-match from scratch |

```bash
# Incremental update — only match new markets
uv run arbscanner match

# Force a full rebuild (expensive; uses Claude API credits)
uv run arbscanner match --rematch
```

### `arbscanner pairs` — inspect the cache

Prints every matched pair in the cache with its confidence score and the source (embedding, LLM, manual).

```bash
uv run arbscanner pairs
```

### `arbscanner serve` — FastAPI web dashboard

Launches a Uvicorn web server exposing the landing page, HTML dashboard, and JSON API.

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `0.0.0.0` | Host to bind to |
| `--port` | `8000` | Port to bind to |
| `--reload` | off | Enable auto-reload (development only) |

```bash
# Production
uv run arbscanner serve --host 0.0.0.0 --port 8000

# Development with hot reload
uv run arbscanner serve --reload
```

Every instance also exposes `/metrics` in Prometheus text exposition format (version 0.0.4). Scan cycles, order-book fetches, rate-limit waits, alerts delivered, and the scan-cycle duration histogram are all populated by `arbscanner.engine`; scraping `http://<host>:8000/metrics` at your preferred interval gets a fleet view without extra configuration:

```bash
curl -s http://localhost:8000/metrics | head -20
```

### `arbscanner paper` — paper trading account

Manage a simulated execution account populated either by `scan --paper` or by
manually opening positions from logged opportunities. Positions are persisted
to a dedicated `paper_positions` table in the scanner SQLite DB, so account
state survives restarts.

```bash
# Aggregate account summary (balance, PnL, win rate)
uv run arbscanner paper summary

# List all positions, only open, or only closed
uv run arbscanner paper list --status open

# Open a position from a logged opportunity row (see /api/opportunities for ids)
uv run arbscanner paper open --opportunity-id 42 --size 50

# Mark-to-market close at supplied prices
uv run arbscanner paper close --position-id 7 --poly-price 0.55 --kalshi-price 0.40

# Close at final resolution — "yes" or "no" is the market outcome
uv run arbscanner paper resolve --position-id 7 --outcome yes
```

| Flag | Description |
|------|-------------|
| `--balance` | Starting balance (first run only, default 10000) |
| `--status` | Filter for `list`: `open` / `closed` / `all` (default `all`) |
| `--opportunity-id` | Logged opportunity ID for `open` |
| `--size` | Override size (contracts) for `open` |
| `--position-id` | Paper position ID for `close` / `resolve` |
| `--poly-price`, `--kalshi-price` | Mark prices for `close` |
| `--outcome` | `yes` or `no` for `resolve` |

### `arbscanner execute` — dry-run the execution pipeline

> ⚠️ **Phase A ships dry-run only.** No real orders are placed on either exchange. The full planning, safety-check, two-leg-placement, and partial-fill-unwind pipeline runs as a pure simulation using the current order-book state. Credentials are not consulted. A later Phase A.2 will add a live path behind an explicit opt-in; until then `arbscanner execute` is safe to run against any logged opportunity.

Loads an opportunity from the SQLite log, re-fetches both exchanges' current order books, validates the arb still exists, caps the trade size by both available liquidity and the per-trade USD cap, and runs a two-leg simulated placement. If the second leg simulates a rejection (via `--simulate-leg2-failure`), the pipeline plans and logs a market-order unwind of the first leg at the current best bid. Every outcome is persisted to a new `execution_log` SQLite table.

| Flag | Default | Description |
|------|---------|-------------|
| `opportunity_id` | (required) | ID of the logged opportunity to dry-run |
| `--max-trade-usd` | `100.0` | Per-trade USD notional cap (sizing floor is applied across liquidity + cap) |
| `--yes` | off | Skip the interactive "proceed?" confirmation |
| `--simulate-leg2-failure` | off | Force the Kalshi leg to reject; exercises the unwind path |

```bash
# Dry-run opportunity #42 under the default $100 cap
uv run arbscanner execute 42

# Same but non-interactive, tighter cap
uv run arbscanner execute 42 --yes --max-trade-usd 25

# Exercise the partial-fill recovery path
uv run arbscanner execute 42 --simulate-leg2-failure --yes
```

Sample output:

```
Execution report (DRY RUN — no real orders placed)
============================================================
  Opportunity ID:       42
  Market:               Will the Fed cut rates in June?
  Direction:            poly_yes_kalshi_no
  Current poly ask:     0.4200
  Current kalshi ask:   0.4400
  Per-contract cost:    0.8600
  Per-contract fees:    0.0358
  Per-contract net:     0.1042
  Size:                 80.00 contracts
  Total cost:           $68.80
  Total fees:           $2.87
  Expected net profit:  $8.34
  Per-trade USD cap:    $100.00

  Leg 1 (polymarket):   FILLED  filled=80.00 @ 0.4200  fee=$0.0336
  Leg 2 (kalshi):       FILLED  filled=80.00 @ 0.4400  fee=$2.8000

  Result:               SUCCESS
  Final realized PnL:   $8.3400
```

**Safety model** (from the Phase A design decisions):

- **Dry-run only** — `arbscanner.execution.EXECUTION_MODE == "dry_run"` is a module constant with a test that asserts it. Flipping it is a conscious opt-in, not a runtime override.
- **Per-trade USD cap** hard defaulted to $100. Overridable per call via `--max-trade-usd`.
- **Integer contract rounding** — size is floored to whole contracts since Kalshi trades in integers. Sub-dollar caps are rejected with `insufficient_liquidity`.
- **Stale arb detection** — if the re-fetched order books no longer show a positive net edge, the plan is rejected with `status="stale"` and no simulated orders are placed.
- **Partial-fill unwind** — if leg 2 fails, leg 1 is immediately unwound at the current best bid (or entry − $0.01 fallback). Realized slippage is logged.
- **CLI-only trigger** — no HTTP execution endpoint, no auto-trigger from the scan loop in Phase A.

### `arbscanner backtest` — replay the opportunity log

Replays every logged opportunity against historical resolved-market outcomes (ingested by `arbscanner calibrate --ingest-live` or `--ingest-url`) and reports realized PnL, win rate, and a per-category breakdown. Uses the paper trading engine under the hood with an isolated temp-file SQLite DB, so your live `paper_positions` table is never touched.

| Flag | Default | Description |
|------|---------|-------------|
| `--start` | none | ISO 8601 timestamp — only replay opportunities on or after this |
| `--end` | none | ISO 8601 timestamp — only replay opportunities strictly before this |
| `--min-edge` | `0.0` | Floor on `net_edge` — ignore logged opportunities below this |
| `--initial-balance` | `10000` | Starting paper balance for the replay |

```bash
# Backtest everything in the log
uv run arbscanner backtest

# Only replay the past week, ignoring anything below 2% net edge
uv run arbscanner backtest --start 2026-04-04 --min-edge 0.02
```

Typical output:

```
Backtest results
============================================================
  Logged opportunities:     142
  Resolved (replayed):      89
  Unresolved (skipped):     52
  Disagreement (skipped):   1

  Initial balance:          $10000.00
  Final balance:            $10423.50
  Realized PnL:             $423.50
  Wins / Losses:            89 / 0
  Win rate:                 100.0%
  Avg PnL / trade:          $4.76

  By category:
    Category          Trades   Wins     Win%    Total PnL
    ---------------- ------- ------ -------- ------------
    economics              18      18  100.0% $    195.00
    politics               22      22  100.0% $     82.00
    sports                 49      49  100.0% $    146.50
```

**Key property:** on genuinely locked-in arbs, realized PnL equals `gross_edge × size` regardless of which side of the market wins — `backtest` confirms the scanner's detected edge survives to resolution. Run `arbscanner calibrate --ingest-live --limit 1000` first to populate `historical_polymarket.parquet` / `historical_kalshi.parquet` so opportunities have resolutions to join against.

### `arbscanner calibrate` — calibration data

Computes, ingests, or views the calibration layer.

| Flag | Description |
|------|-------------|
| `--data-file PATH` | Compute calibration curves from a local Parquet file of historical resolutions |
| `--ingest-url URL` | Download a Parquet dataset from a URL and ingest it |
| `--ingest-live` | Fetch resolved markets live from Polymarket + Kalshi via `pmxt` |
| `--limit N` | Max resolved markets per exchange (only for `--ingest-live`) |

With no flags, prints aggregate historical edge statistics pulled from the scanner's SQLite log.

```bash
# Ingest Jon Becker's public historical dataset
uv run arbscanner calibrate --ingest-url https://example.com/polymarket_history.parquet

# Ingest the 1000 most recently resolved markets from each exchange
uv run arbscanner calibrate --ingest-live --limit 1000

# Compute curves from a local file
uv run arbscanner calibrate --data-file data/resolutions.parquet

# Show stats from the scanner DB
uv run arbscanner calibrate
```

---

## Configuration

All configuration is driven by environment variables loaded from `.env` (see `.env.example`).

| Variable | Required? | Purpose |
|----------|-----------|---------|
| `POLYMARKET_PRIVATE_KEY` | Only for trading | Wallet key for Polymarket execution |
| `KALSHI_API_KEY` | Only for trading | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY` | Only for trading | Kalshi API signing key |
| `ANTHROPIC_API_KEY` | Recommended | Used by the matcher to confirm/reject ambiguous pairs |
| `TELEGRAM_BOT_TOKEN` | Optional | Bot token for Telegram alerts |
| `TELEGRAM_CHAT_ID` | Optional | Target chat ID for Telegram alerts |
| `DISCORD_WEBHOOK_URL` | Optional | Webhook URL for Discord alerts |
| `STRIPE_SECRET_KEY` | Optional | Stripe secret key for the paid-tier landing page |
| `STRIPE_WEBHOOK_SECRET` | Optional | Stripe webhook signing secret |
| `STRIPE_PRICE_ID` | Optional | Stripe price ID for the subscription |
| `ARBSCANNER_PUBLIC_URL` | If Stripe enabled | Absolute base URL (e.g. `https://arbscanner.example.com`) used to build Stripe Checkout success/cancel redirects. Default `http://localhost:8000` |
| `ARBSCANNER_SECRET_KEY` | Recommended | Session secret for the FastAPI web app |
| `ARBSCANNER_TIER` | Optional | `pro` (default) or `free`. Enforces the landing-page free-tier caps: top 3 opportunities, 5-minute delayed view, no Telegram/Discord alerts, no calibration context. Individual requests can override with an `X-Arbscanner-Tier` header. |

Read-only scanning requires only `ANTHROPIC_API_KEY` (and only for the matching step). Everything else is opt-in.

### Free vs. Pro tier

The landing page advertises a two-tier model (CLAUDE.md Day 10). The operator picks a global default via `ARBSCANNER_TIER`; callers can override per request with the `X-Arbscanner-Tier: free|pro` header (useful for demo deployments that want the dashboard to behave as "pro" for the operator but "free" for anonymous visitors fronted by a reverse proxy that injects the header).

| Feature | Free | Pro |
|---|---|---|
| `/api/opportunities` row cap | Top 3 by expected profit | No cap |
| Visible window | ≥ 5 minutes old | Real-time |
| Calibration context on each row | Stripped (`null`) | Included |
| `/api/calibration` | HTTP 402 | Available |
| Telegram / Discord alerts | Skipped | Delivered |
| Web dashboard access | ✓ | ✓ |
| `arbscanner scan` (CLI) | Full (local operator) | Full |

Note: the CLI scanner (`arbscanner scan`) is unaffected by the tier setting — the operator running the scanner locally always gets the full experience. Only the HTTP-facing surfaces (web dashboard and alerts dispatched by the scan loop) observe the tier.

---

## Architecture

```
┌─────────────────────────────────────────┐
│            Data Layer (pmxt)            │
│  Polymarket · Kalshi · Limitless        │
│  fetch_markets() · fetch_order_book()   │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│         Market Matcher                  │
│  LLM-assisted fuzzy match + manual map  │
│  "Fed rate cut June" ↔ KXFEDCUT-26JUN   │
│  Output: matched_pairs.json             │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│         Arb Engine                      │
│  For each matched pair:                 │
│    - Pull best bid/ask from both books  │
│    - Compute: yes_A + no_B < $1.00?     │
│    - Compute: yes_B + no_A < $1.00?     │
│    - Subtract fees (Kalshi ~1%, Poly    │
│      0.1% taker)                        │
│    - Net edge > threshold? → ALERT      │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│         Delivery                        │
│  v1: Terminal dashboard (rich/textual)  │
│  v2: Web dashboard + email/Telegram     │
│  v3: One-click execution via pmxt       │
└─────────────────────────────────────────┘
```

### Module layout

| Module | Responsibility |
|--------|---------------|
| `arbscanner.cli` | argparse entry point and subcommand wiring |
| `arbscanner.config` | `.env` loading and runtime settings |
| `arbscanner.exchanges` | `pmxt` wrappers: `create_exchanges`, `fetch_all_markets` |
| `arbscanner.matcher` | Embedding + LLM matching pipeline and cache I/O |
| `arbscanner.engine` | Arb calculation, fee model, and parallel scanning |
| `arbscanner.dashboard` | Rich terminal UI loop |
| `arbscanner.web` | FastAPI app: landing page, HTML dashboard, JSON API |
| `arbscanner.alerts` | Telegram + Discord webhook dispatch |
| `arbscanner.calibration` | Historical dataset ingestion and curve computation |
| `arbscanner.db` | SQLite opportunity log |
| `arbscanner.paper_trading` | Simulated execution account for expected-vs-realized edge tracking |
| `arbscanner.backtest` | Replay the opportunity log against resolved-market outcomes and report realized PnL |
| `arbscanner.execution` | Dry-run execution pipeline: planning, safety checks, two-leg simulation, partial-fill unwind, audit log |

---

## Web dashboard

Start the server with `uv run arbscanner serve` and browse to the endpoints below.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Landing page (marketing + Stripe checkout for the paid tier) |
| `/dashboard` | GET | Live HTML arb table with auto-refresh |
| `/api/opportunities` | GET | JSON list of current opportunities sorted by expected dollar value |
| `/api/stats` | GET | Aggregate scanner stats (pair count, last scan time, edge histogram) |
| `/api/calibration` | GET | Calibration curves by category × time-to-resolution |
| `/api/pairs` | GET | Every matched pair with confidence and source |
| `/api/paper/summary` | GET | Paper trading account summary |
| `/api/paper/positions` | GET | Paper positions (`?status=open\|closed\|all`) |
| `/api/paper/positions/{id}/close` | POST | Mark-to-market close: `{poly_price, kalshi_price}` |
| `/api/paper/positions/{id}/resolve` | POST | Resolve at outcome: `{yes_won: true\|false}` |

The JSON endpoints are public by default and safe to poll from external tools or scripts. If you're exposing the server beyond localhost, put it behind a reverse proxy and set `ARBSCANNER_SECRET_KEY` to something long and random.

---

## Running tests

```bash
uv run pytest
```

Add `-v` for verbose output, `-k <pattern>` to run a subset, or point at a specific file (e.g. `uv run pytest tests/test_engine.py`).

---

## Project status

Both planned weeks from the technical plan are complete, plus a round of pipeline improvements:

**Week 1 — Core scanner (CLI)** — complete
- Data ingestion via `pmxt` for Polymarket and Kalshi
- Embedding-based market matcher with Claude LLM confirmation and persistent JSON cache
- Fee-aware arb engine with liquidity sizing and parallel order-book fetches
- Rich terminal dashboard with configurable interval and threshold
- SQLite opportunity logging

**Week 2 — Productize** — complete
- FastAPI web backend with HTML dashboard and JSON API
- Telegram + Discord webhook alerts
- Calibration layer backed by historical Parquet datasets and live resolved-market ingestion
- Stripe-ready landing page scaffolding for the paid tier

**Pipeline improvements** — complete
- Parallel worker pool for order-book fetches (configurable via `--max-workers`)
- Incremental re-matching so `arbscanner match` only processes new markets
- `calibrate --ingest-live` for on-demand historical ingestion straight from the exchanges
- Aggregate edge statistics surfaced via `arbscanner calibrate` and `/api/stats`

**Paper trading simulator** — complete
- Persistent `paper_positions` SQLite table with open/close/resolve lifecycle
- `arbscanner scan --paper` auto-opens simulated positions on new high-edge opportunities (with pair+direction dedup)
- `arbscanner paper {summary, list, open, close, resolve}` CLI
- `/api/paper/*` JSON endpoints for dashboards and scripts
- Web dashboard shows a live Paper Trading Account panel (balance, open positions, P&L, win rate) as soon as the engine has any activity
- Terminal dashboard caption adds a paper account line when `--paper` is enabled

**Calibration-aware edge scoring** — complete
- Matcher now persists `category` and `resolution_date` alongside each matched pair (backward compatible with old `matched_pairs.json` files)
- The engine attaches a calibration context to every detected opportunity (bucketed mispricing baseline, "edge likely real" flag, and a human-readable note)
- `/api/opportunities` joins the matched-pair cache at query time to return calibration inline per row (no SQL migration needed)
- HTML dashboard has a new Calibration column with Real/Noise badges and tooltip-rendered confidence notes
- Terminal dashboard adds a Calibration column showing `REAL · politics/30-90d · 5.0pt` style indicators

**Free vs. Pro tier gating** — complete
- `ARBSCANNER_TIER=free|pro` environment variable or per-request `X-Arbscanner-Tier` header controls tier
- Free tier caps `/api/opportunities` to top 3 by expected profit, applies a 5-minute delay window, strips calibration context, and returns HTTP 402 on `/api/calibration`
- Free tier also skips Telegram/Discord alert delivery in `alerts.send_alerts`
- Pro tier is the default so a fresh self-hosted install keeps the full experience

**Backtest harness** — complete
- `arbscanner backtest` replays every logged opportunity against the resolved-market Parquet files ingested by the calibration module
- Uses the paper trading engine with an isolated temp-file SQLite DB so the live `paper_positions` table is never polluted
- Reports realized PnL, win rate, initial / final balance, and a per-category breakdown pulled from the matched-pair cache
- Completes the CLAUDE.md Day 5 "log historical opportunities to SQLite for backtesting later" deliverable

**Execution pipeline (Phase A, dry-run)** — complete
- `arbscanner execute <opportunity_id>` runs the full plan → two-leg placement → partial-fill unwind pipeline as a pure simulation. No real orders, no credentials required.
- `plan_execution` re-fetches current order books, caps sizing by `$100` default per-trade USD notional, rejects stale arbs, and floors to integer contracts (Kalshi constraint).
- `execute_plan` simulates both legs and falls through to a market-order unwind of leg 1 on leg-2 rejection. Exercised via `--simulate-leg2-failure`.
- Every execution attempt is audited in a new `execution_log` SQLite table alongside the opportunity log.
- `arbscanner.execution.EXECUTION_MODE == "dry_run"` is an invariant with a test; flipping it is a conscious Phase A.2 decision.

**Roadmap**
- Phase A.2: live execution (real `pmxt.*.create_order` calls, credentials, kill switch, production safety review)
- Broader exchange coverage (Limitless, PredictIt)
- Per-user alert thresholds and portfolio-aware sizing

---

## License

License: TBD. A formal license will be added before any public release. Until then, treat this repository as "all rights reserved" by the authors.
