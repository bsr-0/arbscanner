# ArbScanner Architecture

## System Overview

ArbScanner is a Python 3.12 pipeline that monitors Polymarket and Kalshi simultaneously for cross-platform arbitrage opportunities in real time. It fetches every active market from both exchanges, matches semantically equivalent markets across platforms using sentence-transformer embeddings plus Claude LLM confirmation, and continuously scans matched pairs for price discrepancies where `yes_A + no_B < $1.00` (after fees). Results are surfaced via a Rich terminal dashboard, a FastAPI web UI, and Telegram/Discord alerts, and every opportunity is logged to SQLite for backtesting and calibration analysis.

The pipeline is sync end-to-end (pmxt's Python SDK is synchronous, communicating with a Node.js sidecar). Parallelism comes from a `ThreadPoolExecutor` in the engine that fetches order books concurrently, coordinated by a shared token-bucket `RateLimiter` that enforces a per-second call cap across all workers.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│              Data Layer (exchanges.py)                  │
│  pmxt.Polymarket · pmxt.Kalshi                          │
│  fetch_all_markets()    fetch_order_book_safe()         │
│  RateLimiter(10/s) + retry_with_backoff(3 attempts)     │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│              Matcher (matcher.py)                        │
│  1. normalize_title() — strip question words, expand    │
│     abbreviations, collapse whitespace                   │
│  2. sentence-transformers embeddings (MiniLM-L6-v2)      │
│     + cosine similarity > 0.7 → candidate pairs          │
│  3. Claude API confirms ambiguous matches (0.7-0.9)      │
│  4. Cache confirmed pairs → data/matched_pairs.json      │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│              Engine (engine.py)                          │
│  _fetch_all_books() → ThreadPoolExecutor (max_workers=8) │
│       Fetches N × 4 order books in parallel             │
│  calculate_arb(pair, books) — pure function:            │
│       direction 1: poly_yes + kalshi_no < $1 ?          │
│       direction 2: poly_no + kalshi_yes < $1 ?          │
│  Subtract exact fees (kalshi_fee bracket, poly_fee 0.1%)│
│  Rank by expected_profit = net_edge × available_size    │
└───────────────────────┬─────────────────────────────────┘
                        │
         ┌──────────────┼──────────────┐
         │              │              │
┌────────▼─────┐ ┌──────▼──────┐ ┌────▼────────┐
│  Dashboard   │ │    Web      │ │   Alerts    │
│  (Rich CLI)  │ │  (FastAPI)  │ │ (TG/Discord)│
│ 30s refresh  │ │   /api/*    │ │ AlertDeduper│
└────────┬─────┘ └──────┬──────┘ └─────────────┘
         │              │
         ▼              ▼
    ┌────────────────────────┐
    │   SQLite (db.py)       │
    │  opportunities table   │
    │  + 3 indexes           │
    └────────────────────────┘
```

---

## Module Breakdown

### Data Layer

**`src/arbscanner/exchanges.py`** — pmxt wrapper.
- `create_exchanges() -> (poly, kalshi)` — instantiates `pmxt.Polymarket()` and `pmxt.Kalshi()`
- `fetch_all_markets(exchange, name)` — paginates via `fetch_markets_paginated`, filters to binary markets (`.yes` and `.no` present), with retry + rate limiting
- `fetch_order_book_safe(exchange, outcome_id)` — returns `None` on failure after 3 retries
- Module-level `_rate_limiter = RateLimiter(settings.rate_limit_per_sec)` shared across all threads

**`src/arbscanner/utils.py`** — shared concurrency primitives.
- `retry_with_backoff(max_attempts, base_delay, exceptions)` — decorator with exponential backoff
- `class RateLimiter(calls_per_sec)` — thread-safe token bucket; `.acquire()` blocks until next slot

### Matcher

**`src/arbscanner/matcher.py`** — three-stage matching pipeline.
- `normalize_title(title)` — lowercase, strip leading question words, expand abbreviations (`fed→federal reserve`, `cpi→consumer price index`, etc.), collapse whitespace
- `compute_candidate_pairs(poly, kalshi, threshold=0.7)` — uses `SentenceTransformer("all-MiniLM-L6-v2")` to encode normalized titles, computes cosine similarity matrix, returns pairs above threshold
- `confirm_matches_llm(candidates)` — auto-accepts pairs with similarity ≥ 0.9, sends borderline pairs (0.7-0.9) to Claude for confirmation
- `run_matching(poly_markets, kalshi_markets, rematch=False)` — full pipeline, incrementally updates cache
- `load_cache()` / `save_cache()` — JSON persistence at `data/matched_pairs.json`

### Engine

**`src/arbscanner/engine.py`** — arb calculation.
- `_fetch_all_books(poly, kalshi, pairs, max_workers)` — flat `ThreadPoolExecutor` that submits all `N × 4` outcome IDs at once
- `calculate_arb(pair, books)` — pure function; checks both directions, applies fees, returns opportunities with `net_edge > 0`
- `_best_ask(order_book)` — extracts top-of-book ask price and size
- `scan_all_pairs(poly, kalshi, pairs, threshold, max_workers)` — orchestrates fetch→compute→filter→sort, wrapped in `timing_block(scan_cycle_seconds)`; increments metrics counters

### Delivery

**`src/arbscanner/dashboard.py`** — Rich terminal dashboard with `Live` display, 30s refresh, color-coded edge thresholds.

**`src/arbscanner/web.py`** — FastAPI app with:
- Landing page (`/`), dashboard (`/dashboard`) — Jinja2 templates
- JSON API: `/api/opportunities`, `/api/pairs`, `/api/stats`, `/api/calibration`
- Stripe: `/api/stripe/checkout`, `/api/stripe/webhook`
- Health probes from `health.router`: `/health`, `/live`, `/ready`

**`src/arbscanner/alerts.py`** — Telegram Bot API + Discord webhook delivery, deduplicated by `AlertDeduper`.

**`src/arbscanner/db.py`** — SQLite schema + logging.

### Supporting modules

**`src/arbscanner/models.py`** — all dataclasses: `MatchedPair`, `MatchedPairsCache`, `CandidatePair`, `ArbOpportunity`.

**`src/arbscanner/config.py`** — `Settings` dataclass loaded from `.env`, Kalshi bracket fee schedule, data directory paths.

**`src/arbscanner/calibration.py`** — category × time-to-resolution calibration curves with default profiles fallback.

**`src/arbscanner/metrics.py`** — stdlib Prometheus-style counters/gauges/histograms.

**`src/arbscanner/health.py`** — FastAPI router for liveness and readiness probes.

**`src/arbscanner/alerts_dedup.py`** — TTL-based alert deduplication.

**`src/arbscanner/backup.py`** — online SQLite backup/restore/prune.

**`src/arbscanner/migrations.py`** — versioned schema migrations.

**`src/arbscanner/paper_trading.py`** — simulated execution engine for expected-vs-realized edge tracking.

**`src/arbscanner/logging_config.py`** — stdlib-only pretty/JSON formatters with env var overrides.

**`src/arbscanner/cli.py`** — argparse entry point dispatching to `cmd_scan`, `cmd_match`, `cmd_pairs`, `cmd_serve`, `cmd_calibrate`, `cmd_backup`.

---

## Data Flow

### Hot path (scan loop)

```
every 30s:
  1. engine.scan_all_pairs(poly, kalshi, matched_pairs)
     ├─ _fetch_all_books()
     │  └─ ThreadPoolExecutor.submit × (N × 4) outcome_ids
     │     └─ fetch_order_book_safe() ← rate-limited + retried
     ├─ for each pair: calculate_arb(pair, books)
     └─ filter net_edge ≥ threshold, sort by expected_profit desc
  2. db.log_opportunities(conn, results)
  3. alerts.send_alerts(results)  [if enabled]
     └─ AlertDeduper filters out repeats within TTL
  4. dashboard.update(results)
```

### Cold path (market matching)

```
on CLI: arbscanner match
  1. exchanges.fetch_all_markets(poly), fetch_all_markets(kalshi)
  2. matcher.load_cache() → known matches + rejections
  3. filter to NEW markets not yet in cache
  4. compute_candidate_pairs() → SentenceTransformer encode + cosine sim
  5. confirm_matches_llm() → Claude API for ambiguous pairs
  6. matcher.save_cache() → data/matched_pairs.json
```

---

## Data Models

All models live in `src/arbscanner/models.py` as `@dataclass` types.

**`MatchedPair`** — a confirmed cross-platform match.
```python
poly_market_id: str
poly_title: str
kalshi_market_id: str
kalshi_title: str
confidence: float
source: str                       # "embedding", "embedding+llm", "manual"
matched_at: str                   # ISO 8601
poly_yes_outcome_id: str
poly_no_outcome_id: str
kalshi_yes_outcome_id: str
kalshi_no_outcome_id: str
```

**`MatchedPairsCache`** — persistent cache of matches and rejected pairs.

**`CandidatePair`** — pre-LLM candidate with similarity score, full market context for LLM prompt.

**`ArbOpportunity`** — detected arbitrage.
```python
poly_title, kalshi_title: str
poly_market_id, kalshi_market_id: str
direction: str                    # "poly_yes_kalshi_no" | "poly_no_kalshi_yes"
poly_price, kalshi_price: float   # 0.0-1.0
gross_edge, net_edge: float       # 1.0 - (poly + kalshi); after fees
available_size: float             # min liquidity, in contracts
expected_profit: float            # net_edge × available_size
timestamp: datetime
```

---

## Concurrency Model

**Why sync, not asyncio?** pmxt's Python SDK is synchronous (it talks to a Node.js sidecar over localhost HTTP). Wrapping every call in `asyncio.to_thread` would add complexity without meaningful benefit since the sidecar itself serializes requests.

**What we do instead:** `concurrent.futures.ThreadPoolExecutor` in `engine._fetch_all_books`. We flatten all `(exchange, outcome_id)` fetch tasks across every matched pair into a single pool submission. Threads are I/O-bound (waiting on the sidecar), so GIL contention is minimal.

**Rate limiting:** A single module-level `RateLimiter` instance in `exchanges.py` is shared across all worker threads. It uses a monotonic clock and a mutex to enforce a global per-second call cap (default 10/s, configurable via `settings.rate_limit_per_sec`). Each worker must call `_rate_limiter.acquire()` before making a pmxt request.

**Retry:** `retry_with_backoff(max_attempts=3, base_delay=0.5)` wraps both `_fetch_markets_page` and `_fetch_order_book`. Backoff is exponential: 0.5s, 1.0s, 2.0s.

**Performance:** Before parallelization, a 50-pair scan took ~40s (4 sequential calls per pair × 200ms latency). After: ~5s at default `max_workers=8`.

---

## Database Schema

Single-file SQLite at `arbscanner.db` (path from `config.DB_PATH`).

```sql
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,            -- ISO 8601 UTC
    poly_market_id TEXT NOT NULL,
    kalshi_market_id TEXT NOT NULL,
    market_title TEXT NOT NULL,
    direction TEXT NOT NULL,
    gross_edge REAL NOT NULL,
    net_edge REAL NOT NULL,
    available_size REAL NOT NULL,
    expected_profit REAL NOT NULL,
    poly_price REAL NOT NULL,
    kalshi_price REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_opportunities_timestamp ON opportunities(timestamp);
CREATE INDEX IF NOT EXISTS idx_opportunities_net_edge ON opportunities(net_edge);
CREATE INDEX IF NOT EXISTS idx_opportunities_profit ON opportunities(expected_profit);
```

ISO 8601 strings sort lexicographically, so range queries on `timestamp` use the index correctly. Indexes on `net_edge` and `expected_profit` support the web API's filtering and sorting.

Connections are opened with `check_same_thread=False` so the FastAPI worker pool can share a single connection.

---

## Caching Strategy

**`data/matched_pairs.json`** — the expensive matching pipeline output. Schema:
```json
{
  "version": 1,
  "updated_at": "2026-04-10T12:00:00+00:00",
  "pairs": [...],
  "rejected": ["poly_id::kalshi_id", ...]
}
```
Rejected pairs are cached so the LLM doesn't re-confirm them on subsequent runs. `arbscanner match` is incremental by default; `--rematch` forces a full rebuild.

**`data/calibration/calibration_curves.parquet`** — computed category × time-bucket mispricing curves. Produced by `compute_calibration_curves()` from a historical resolution dataset. Read at runtime by `_lookup_calibration()` with fallback to hardcoded `DEFAULT_PROFILES`.

**In-memory deduper state** — `AlertDeduper._entries` dict in `alerts_dedup.py` holds recent alert fingerprints with TTL expiry. Reset on process restart; not persisted.

---

## Fee Model

**Polymarket** — linear 0.1% taker fee: `poly_fee(price) = price × 0.001`.

**Kalshi** — exact bracket-based schedule (symmetric around 50c):

| Price range | Fee (cents per contract) |
|-------------|-------------------------|
| 0-10c  or 90-100c | 1.5 |
| 10-25c or 75-90c  | 2.5 |
| 25-50c or 50-75c  | 3.5 |

Implemented as `kalshi_fee(price) -> float` in `config.py`.

---

## Error Handling Philosophy

- **Transient failures retry silently** with exponential backoff. After 3 attempts, `fetch_order_book_safe` returns `None` instead of raising, so one flaky outcome doesn't break a whole scan cycle.
- **Missing data is logged, not fatal.** `calculate_arb` checks for `None` books and empty ask sides before computing.
- **User-facing errors fail loud.** Config validation, missing matched pairs, and CLI argument errors produce clear messages and non-zero exit codes.
- **Logging uses `logging.getLogger(__name__)`** consistently; see `logging_config.py` for the pretty/JSON formatters and chatty-logger suppression.

---

## Why These Choices?

- **sentence-transformers over fuzzy matching.** Kalshi's structured tickers (`KXFEDCUT-26JUN`) look nothing like Polymarket's prose titles (`"Will the Fed cut rates at the June 2026 meeting?"`). Semantic embeddings handle this; rapidfuzz wouldn't.
- **SQLite over Postgres.** Single-user, single-machine, lightweight. When we need multi-user, migration is straightforward (schema is portable).
- **Rich over Textual.** The terminal dashboard is a static refreshing table — no need for an interactive TUI framework.
- **FastAPI + Jinja2 HTML over React.** Same observation: the web UI is a read-only table. Adding a JS build toolchain would dwarf the server code.
- **Sync + ThreadPoolExecutor over asyncio.** pmxt is sync. Threads are the minimum-complexity path to I/O parallelism.
- **Exact Kalshi fee brackets.** A flat approximation would be cheaper to compute but systematically mis-price every opportunity. The bracket lookup is O(1).

---

## Limitations and Trade-Offs

- **Binary markets only.** `fetch_all_markets` filters to markets with both `.yes` and `.no`. Multi-outcome markets (e.g. "Which party wins?" with N candidates) are out of scope for v1.
- **Top-of-book sizing.** `calculate_arb` uses only the best ask and its size. Deeper opportunities (walking the book) are TODO.
- **Polling-only.** No WebSocket subscription; we refetch books every 30s. Real execution would need sub-second market data.
- **Single-instance SQLite.** One process writes. Multiple scanners writing to the same DB will hit `database is locked`. Horizontal scaling would require Postgres.
- **No execution.** The scanner detects arbs but does not place trades. Paper trading simulation exists in `paper_trading.py`; live execution is a separate module.
- **In-memory alert dedup.** State resets on restart. For production, persist to Redis or the SQLite DB.
- **Calibration data ingestion is a stub.** `ingest_from_exchange` scrapes resolved markets but the quality depends on pmxt exposing `final_price` and `resolution_date`. A curated dataset (e.g. Jon Becker's) is the better long-term source.
