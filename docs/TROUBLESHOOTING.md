# arbscanner Troubleshooting Guide

This guide covers the most common problems you will hit when installing,
running, and operating arbscanner. Each entry follows a
**Symptom / Cause / Fix** structure so you can jump straight to whatever is
broken.

If you hit something that is not covered here, re-run the failing command with
the global `-v` flag to get debug-level logs:

```bash
arbscanner -v scan
```

---

## 1. Installation Issues

### 1.1 `uv: command not found`

**Symptom**

```
$ uv sync
bash: uv: command not found
```

**Cause**

arbscanner uses [uv](https://github.com/astral-sh/uv) as its package
manager and virtual-env driver. It is not installed by default on most
systems.

**Fix**

Install uv with the official one-shot installer, then re-open your shell (or
`source` your rc file) so `~/.local/bin` ends up on `PATH`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
exec "$SHELL" -l
uv --version
```

Then from the repo root:

```bash
cd /home/user/arbscanner
uv sync
```

### 1.2 `pmxt` sidecar fails to start on port 3847

**Symptom**

```
pmxt.errors.SidecarError: sidecar failed to start on port 3847
```

…or a Python traceback in `create_exchanges()` when `arbscanner scan` boots.

**Cause**

`pmxt` (the Python package used in `src/arbscanner/exchanges.py`) is a thin
client around a Node.js sidecar named **`pmxtjs`**. If the Node process is
not installed, is too old, or cannot bind to port 3847, the Python side
raises this error immediately.

**Fix**

1. Make sure you have **Node.js >= 18**:

   ```bash
   node --version     # expect v18.x or newer
   ```

   If your Node is older, upgrade via [nvm](https://github.com/nvm-sh/nvm):

   ```bash
   nvm install 20
   nvm use 20
   ```

2. Install the sidecar globally so `pmxt` can spawn it:

   ```bash
   npm install -g pmxtjs
   which pmxtjs       # should print a path
   ```

3. If port 3847 is already in use by a stale sidecar from a previous crash:

   ```bash
   lsof -iTCP:3847 -sTCP:LISTEN
   kill <PID>
   ```

4. Re-run `arbscanner scan` and confirm the sidecar boots.

### 1.3 sentence-transformers model download hangs

**Symptom**

The first call to `arbscanner match` (or the implicit match run inside
`arbscanner scan`) hangs for several minutes at:

```
Encoding N Polymarket + M Kalshi titles
```

…with no progress. Sometimes it eventually errors out with a Hugging Face
hub timeout.

**Cause**

On first use, `sentence-transformers` downloads the
`all-MiniLM-L6-v2` model (~80 MB) from Hugging Face. On slow or flaky
connections the default timeout is too short.

**Fix**

Bump the Hugging Face download timeout and retry:

```bash
export HF_HUB_DOWNLOAD_TIMEOUT=120
arbscanner match
```

If you are behind a corporate proxy, also set:

```bash
export HTTPS_PROXY=http://your-proxy:port
export HTTP_PROXY=http://your-proxy:port
```

If the download still fails, pre-warm the cache manually:

```bash
uv run python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
```

The model is cached under `~/.cache/huggingface/` and subsequent runs are
instant.

### 1.4 Python version too old

**Symptom**

```
error: Python 3.11.5 is not compatible with the project requirement (>=3.12)
```

or runtime errors about `StrEnum`, `typing.Self`, PEP 695 generics, etc.

**Cause**

arbscanner targets Python **3.12+** (see `pyproject.toml`). Older
interpreters do not understand the syntax or stdlib features used in
`src/arbscanner/`.

**Fix**

Let uv install a compatible interpreter for you:

```bash
uv python install 3.12
uv sync
uv run arbscanner --help
```

Alternatively, install 3.12 from [python.org](https://www.python.org/downloads/)
or your package manager (`brew install python@3.12`, etc.).

---

## 2. Runtime Errors

### 2.1 `No matched pairs found`

**Symptom**

```
$ arbscanner scan
No matched pairs found. Running matcher first...
...
No matched pairs found. Cannot scan.
```

…or `arbscanner pairs` prints the yellow warning and exits.

**Cause**

`data/matched_pairs.json` is missing or empty. `cmd_scan` (see
`src/arbscanner/cli.py`) tries to auto-run the matcher, but if that also
produces zero pairs you will see the hard error.

**Fix**

Run the matcher explicitly:

```bash
arbscanner match
```

> Note: the **initial** match run takes a few minutes — it downloads the
> embedding model (if missing), pulls every active binary market from both
> exchanges, encodes thousands of titles, and then sends ambiguous pairs to
> Claude for confirmation. Subsequent runs are incremental and much faster.

Verify afterwards:

```bash
arbscanner pairs
```

If `match` finishes successfully but still shows zero pairs, your
`embedding_threshold` in `config.py` may be too strict — try lowering it
from `0.7` to `0.6` and re-run with `--rematch`.

### 2.2 `Connection refused to port 3847`

**Symptom**

```
ConnectionRefusedError: [Errno 111] Connection refused
```

…in a stack trace that mentions `pmxt` or `exchanges.fetch_markets_paginated`.

**Cause**

The `pmxtjs` sidecar process is not running. Either it was never installed,
it crashed, or the previous `arbscanner` process exited without cleaning up.

**Fix**

See [1.2 pmxt sidecar fails to start on port 3847](#12-pmxt-sidecar-fails-to-start-on-port-3847).
The short version:

```bash
npm install -g pmxtjs
lsof -iTCP:3847 -sTCP:LISTEN   # kill any stragglers
arbscanner -v scan             # verbose shows the sidecar boot
```

### 2.3 Kalshi or Polymarket `429 Too Many Requests`

**Symptom**

Logs are full of:

```
WARNING arbscanner.utils: Retry 1/3 for _fetch_order_book after 0.50s: 429 Too Many Requests
```

and scans eventually drop order books with `None`.

**Cause**

The shared `RateLimiter` in `src/arbscanner/exchanges.py` is tuned to
`rate_limit_per_sec = 10.0` by default. That is aggressive for some
accounts or geographic regions, especially when `max_workers > 8`.

**Fix**

Lower `rate_limit_per_sec` in `src/arbscanner/config.py`:

```python
# src/arbscanner/config.py
rate_limit_per_sec: float = 4.0   # was 10.0
```

Then restart your scanner. You can also reduce concurrency at the CLI:

```bash
arbscanner scan --max-workers 4
```

If the 429s only hit one exchange, the upstream culprit is usually
Polymarket — they throttle more aggressively. Keep `max_workers` low and
the rate limit under 5/sec until the errors clear.

### 2.4 `Invalid API key` from Anthropic

**Symptom**

```
anthropic.AuthenticationError: Error code: 401 - {'error': {'type': 'authentication_error', 'message': 'invalid x-api-key'}}
```

…during `arbscanner match` when it hits the LLM confirmation stage.

**Cause**

`ANTHROPIC_API_KEY` is unset, stale, or whitespace-polluted. `config.py`
reads it from `.env` via `python-dotenv`; if you recently rotated keys,
`matcher.py` will still try to use the old one.

**Fix**

1. Check the env var is actually loaded:

   ```bash
   uv run python -c "from arbscanner.config import settings; print(repr(settings.anthropic_api_key[:10]))"
   ```

2. Update `.env` at the repo root — note the file is **not** the same as
   your shell env:

   ```bash
   # .env
   ANTHROPIC_API_KEY=sk-ant-...your-key-here...
   ```

3. If you intentionally want to skip the LLM step, leave the key empty —
   `confirm_matches_llm()` will log a warning and auto-accept candidates
   above the embedding threshold.

---

## 3. Scanner Shows Zero Opportunities

### 3.1 Empty opportunity table during scan

**Symptom**

The dashboard refreshes every 30s and the opportunity table is always
empty, even though `arbscanner pairs` shows 50+ matched pairs.

**Cause**

In order of likelihood:

1. **Arbs are genuinely rare.** Markets are mostly efficient — real arbs
   are small and last seconds to minutes, not hours.
2. Your `--threshold` is higher than any live edge.
3. `matched_pairs.json` exists but was built from stale/resolved markets,
   so the outcome IDs no longer have live order books.
4. Order book fetches are silently failing and `fetch_order_book_safe` is
   returning `None` (see `src/arbscanner/exchanges.py`).

**Fix**

Walk through the diagnostic ladder:

1. Confirm pairs exist:

   ```bash
   arbscanner pairs
   ```

2. Lower the threshold so you can see sub-1% edges:

   ```bash
   arbscanner scan --threshold 0.005
   ```

3. Run verbose and watch for `Failed to fetch order book` log lines —
   anything beyond a handful means upstream is flaky:

   ```bash
   arbscanner -v scan --threshold 0.005
   ```

4. Manually probe one pair with pmxt to rule out stale data — open a
   Python REPL and try:

   ```python
   import pmxt
   poly = pmxt.Polymarket()
   book = poly.fetch_order_book("<outcome_id_from_matched_pairs.json>")
   print(book)
   ```

5. If outcome IDs are stale, rebuild the cache:

   ```bash
   arbscanner match --rematch
   ```

---

## 4. Scan Cycle Slower Than Expected

### 4.1 A single scan tick takes >60s

**Symptom**

Your refresh interval is 30s but each scan cycle logs for a minute or
more, so the dashboard feels unresponsive.

**Cause**

Each matched pair requires four `fetch_order_book` calls (poly yes, poly
no, kalshi yes, kalshi no). With a strict rate limit and low worker
count, 200 pairs × 4 calls = 800 calls, which at 10/sec is already 80s
best-case.

**Fix**

1. Bump worker concurrency:

   ```bash
   arbscanner scan --max-workers 16
   ```

2. Raise `rate_limit_per_sec` in `src/arbscanner/config.py` **only if**
   you are not already seeing 429s. Start with 15 or 20:

   ```python
   rate_limit_per_sec: float = 15.0
   ```

3. Check the logs for retry loops. `retry_with_backoff` in
   `src/arbscanner/utils.py` uses exponential backoff
   (`0.5s → 1.0s → 2.0s`) per attempt. A few retries are normal; if every
   call is retrying, fix the upstream issue first (see 2.3).

4. If your matched pair count is in the thousands, consider trimming
   `matched_pairs.json` down to the pairs you actually care about — there
   is no built-in filter, but the file is plain JSON and trivial to edit.

---

## 5. Web Dashboard Issues

### 5.1 `Address already in use` on port 8000

**Symptom**

```
$ arbscanner serve
ERROR:    [Errno 98] Address already in use
```

**Cause**

Another process (a zombie uvicorn, a different dev server, etc.) is bound
to port 8000.

**Fix**

Either find and kill the offender:

```bash
lsof -iTCP:8000 -sTCP:LISTEN
kill <PID>
```

…or bind to a different port:

```bash
arbscanner serve --port 8080
```

### 5.2 `/api/opportunities` returns an empty list

**Symptom**

`curl http://localhost:8000/api/opportunities` returns `[]` even though
you expect data.

**Cause**

One of:

1. No scan has run yet, so `arbscanner.db` has no rows logged by
   `log_opportunities()`.
2. The endpoint defaults to `hours=1`, meaning it only returns
   opportunities logged in the last hour. If your last scan was yesterday,
   you will see an empty list.
3. Your scan is running but finds nothing above `--threshold` (see
   section 3).

**Fix**

1. Run a scan in a second terminal, leave it up, then hit the API again:

   ```bash
   arbscanner scan --threshold 0.005
   ```

2. Widen the time window on the API call (if the endpoint accepts a
   query param — the default is `hours=1`):

   ```bash
   curl 'http://localhost:8000/api/opportunities?hours=24'
   ```

3. Verify the DB actually has rows:

   ```bash
   sqlite3 arbscanner.db 'SELECT COUNT(*) FROM opportunities;'
   ```

### 5.3 Jinja2 / HTML template errors

**Symptom**

```
jinja2.exceptions.TemplateNotFound: dashboard.html
```

or

```
ModuleNotFoundError: No module named 'jinja2'
```

**Cause**

Either Jinja2 is missing from the installed dependencies (likely if you
`pip install`-ed instead of `uv sync`-ing), or the `templates/` directory
next to `src/arbscanner/web.py` is missing after a bad checkout.

**Fix**

1. Re-run `uv sync` to make sure every dependency is installed:

   ```bash
   uv sync
   ```

2. Verify templates are present:

   ```bash
   ls src/arbscanner/templates/
   ```

3. If the directory is missing, restore it from git:

   ```bash
   git checkout -- src/arbscanner/templates/
   ```

---

## 6. Alerts Not Firing

### 6.1 Telegram alerts never arrive

**Symptom**

Scanner logs `Alerts enabled (threshold: 2.0%)` and you see qualifying
opportunities, but no Telegram messages land.

**Cause**

The most common reasons:

1. `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` in `.env` is wrong.
2. You never started a chat with your bot, so it cannot message you
   (Telegram does not allow bots to DM users who have not opened the
   conversation first).
3. `alert_threshold` (default `0.02`) is above the actual edge size.

**Fix**

1. Fetch your chat ID via [@userinfobot](https://t.me/userinfobot) — open
   the chat, send any message, and copy the numeric ID it returns.

2. Put both values in `.env`:

   ```bash
   # .env
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   TELEGRAM_CHAT_ID=987654321
   ```

3. Start a DM with your bot (just send `/start`).

4. Smoke-test the credentials with curl before blaming arbscanner:

   ```bash
   curl -X POST \
     "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
     -d "chat_id=${TELEGRAM_CHAT_ID}" \
     -d "text=arbscanner test"
   ```

   A `{"ok":true,...}` response means the credentials work.

5. Lower `alert_threshold` in `src/arbscanner/config.py` if the real
   edges are smaller than 2%:

   ```python
   alert_threshold: float = 0.01
   ```

### 6.2 Discord webhook alerts silently fail

**Symptom**

No errors, but no Discord messages either.

**Cause**

Discord webhooks can be deleted server-side (by channel admins or by the
server owner rotating integrations). A stale URL returns 404 but
arbscanner's alert sender may only log it at DEBUG level.

**Fix**

1. Re-generate the webhook: Discord → Server Settings → Integrations →
   Webhooks → New Webhook → copy URL.

2. Put the fresh URL in `.env`:

   ```bash
   # .env
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/.../...
   ```

3. Smoke-test with curl:

   ```bash
   curl -H "Content-Type: application/json" \
     -d '{"content":"arbscanner test"}' \
     "${DISCORD_WEBHOOK_URL}"
   ```

   HTTP 204 means success.

4. Re-run the scanner with `-v` and look for alert-sender log lines to
   confirm the webhook is being hit at all.

---

## 7. Database Issues

### 7.1 `database is locked`

**Symptom**

```
sqlite3.OperationalError: database is locked
```

…usually during `log_opportunities()` at the end of a scan cycle.

**Cause**

SQLite allows only one writer at a time. If you have two `arbscanner
scan` processes running (or a scanner plus a `serve` with hot-reload that
also writes), they will fight for the write lock.

**Fix**

1. Stop all duplicate scanners:

   ```bash
   pgrep -fa 'arbscanner scan'
   kill <PID>
   ```

2. If you need multiple scanners simultaneously (you usually do not),
   point them at separate DB files by adjusting `DB_PATH` in
   `src/arbscanner/config.py` per process.

3. Once untangled, retry the scan. The lock clears as soon as the stray
   process dies.

### 7.2 Growing database file

**Symptom**

`arbscanner.db` grows into the hundreds of megabytes after weeks of
continuous scanning.

**Cause**

Every scan tick appends rows via `log_opportunities()` and nothing prunes
old data by default.

**Fix**

Add a periodic prune + VACUUM. A cron entry along these lines works:

```bash
# crontab -e
0 4 * * * cd /home/user/arbscanner && sqlite3 arbscanner.db "DELETE FROM opportunities WHERE ts < strftime('%s','now','-30 days'); VACUUM;"
```

Adjust the column name (`ts`, `created_at`, etc.) to match your schema —
check with:

```bash
sqlite3 arbscanner.db '.schema opportunities'
```

### 7.3 Schema drift after upgrading arbscanner

**Symptom**

```
sqlite3.OperationalError: no such column: net_edge
```

or similar, after pulling a new version of arbscanner.

**Cause**

arbscanner does not currently ship automatic schema migrations. If a
columns was added or renamed in `src/arbscanner/db.py`, old DB files
remain on the old schema.

**Fix**

Back up, then drop and recreate the `opportunities` table. You will lose
historical rows:

```bash
cp arbscanner.db arbscanner.db.bak
sqlite3 arbscanner.db 'DROP TABLE opportunities;'
arbscanner scan   # table is re-created on next write
```

If you need to preserve history, export to CSV first:

```bash
sqlite3 -header -csv arbscanner.db 'SELECT * FROM opportunities;' > opportunities_backup.csv
```

---

## 8. Matcher Produces Bad Matches

### 8.1 Obvious false positives in `matched_pairs.json`

**Symptom**

`arbscanner pairs` shows nonsense pairings, e.g. a Polymarket "Trump
wins 2024" linked to a Kalshi "Biden approval > 50%", because their
title embeddings happened to be close.

**Cause**

Two failure modes:

1. `embedding_threshold` is too low, so too many candidates pass through.
2. `ANTHROPIC_API_KEY` is unset, so `confirm_matches_llm()` short-circuits
   and **auto-accepts** everything above the embedding threshold (see the
   warning in `matcher.py`).

**Fix**

1. Set `ANTHROPIC_API_KEY` in `.env` so the LLM confirmation stage
   actually runs.

2. Tighten `embedding_threshold` in `src/arbscanner/config.py`:

   ```python
   embedding_threshold: float = 0.8   # was 0.7
   ```

3. Manually purge the bad pairs — the cache is plain JSON at
   `data/matched_pairs.json`. Delete the offending entries under `pairs`,
   then add their composite key to `rejected` so they are not re-matched:

   ```json
   {
     "rejected": [
       "<poly_market_id>::<kalshi_market_id>"
     ]
   }
   ```

4. Re-run matching with the `--rematch` flag so the corrected cache is
   the new baseline:

   ```bash
   arbscanner match --rematch
   ```

### 8.2 Matcher misses real pairs

**Symptom**

You know two markets are the same event (e.g. Fed rate cut in June), but
`arbscanner pairs` does not list them.

**Cause**

Either the embedding similarity fell below `embedding_threshold`, or one
of the markets was filtered out upstream because it is missing a `.yes`
or `.no` outcome (see `fetch_all_markets()` in
`src/arbscanner/exchanges.py`).

**Fix**

1. Loosen the embedding threshold temporarily to see if the pair shows up
   as a candidate:

   ```python
   embedding_threshold: float = 0.6
   ```

2. Re-run with a forced rematch:

   ```bash
   arbscanner match --rematch
   ```

3. If the pair still does not appear, probe the raw markets in a REPL and
   confirm both have `yes`/`no` outcomes. If Kalshi only exposes the YES
   side as a standalone contract, it will be filtered out at fetch time.

4. As a last resort, add the pair manually to `data/matched_pairs.json`
   with `"source": "manual"` and a confidence of `1.0` — the rest of the
   pipeline treats manual entries the same as auto-matched ones.

---

## Where to look next

- `src/arbscanner/cli.py` — command dispatch and CLI flags.
- `src/arbscanner/config.py` — every tunable lives here.
- `src/arbscanner/exchanges.py` — rate limiter, retry wrapper, pmxt calls.
- `src/arbscanner/matcher.py` — embedding + LLM matching pipeline.
- `src/arbscanner/utils.py` — retry/backoff primitive used everywhere.

When filing a bug, include the output of `arbscanner -v <command>` and
the relevant section of `data/matched_pairs.json` if matching is
involved.
