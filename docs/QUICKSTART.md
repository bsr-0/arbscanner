# Quickstart

**arbscanner** is a cross-platform prediction market arbitrage scanner that continuously
monitors overlapping markets on Polymarket and Kalshi and surfaces the best net-of-fees
opportunities. In this 5-minute guide you will install arbscanner, match overlapping
markets between the two exchanges, and stream live arbitrage opportunities in a terminal
dashboard (with an optional web UI and Telegram alerts).

---

## Prerequisites

Before you start, make sure you have the following installed:

- [ ] **Python 3.12 or newer** (`python3 --version`)
- [ ] **Node.js 18 or newer** (`node --version`) — required for `pmxtjs`, the exchange
      connector arbscanner shells out to
- [ ] **uv** package manager (`uv --version`) — install with
      `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [ ] **~2 GB of free disk space** — the matcher downloads a `sentence-transformers`
      embedding model (~400 MB) plus PyTorch weights on first run
- [ ] **An Anthropic API key** (optional but recommended) for LLM-assisted disambiguation
      of fuzzy market matches

> You do **not** need Polymarket or Kalshi API keys to run the scanner. Market data and
> order books are public; arbscanner is read-only by default.

---

## Step 1: Clone and install

```bash
# 1a. Clone the repository
git clone https://github.com/yourorg/arbscanner.git
cd arbscanner

# 1b. Install Python dependencies into a uv-managed virtualenv
uv sync

# 1c. Install the pmxt CLI globally (Polymarket + Kalshi connector)
npm install -g pmxtjs

# 1d. Sanity check that the CLI is on your PATH
pmxt --version
uv run arbscanner --help
```

If `uv run arbscanner --help` prints the subcommand list (`scan`, `match`, `pairs`,
`serve`, `calibrate`) you are good to go.

---

## Step 2: Configure environment

Copy the example environment file and fill in only the keys you need.

```bash
cp .env.example .env
```

Open `.env` in your editor. For a read-only scanning session, the only key that actually
matters is `ANTHROPIC_API_KEY` (used by the market matcher to confirm ambiguous pairs).

| Variable                  | Required?                                | Notes                                                 |
| ------------------------- | ---------------------------------------- | ----------------------------------------------------- |
| `ANTHROPIC_API_KEY`       | Recommended                              | LLM-assisted match confirmation. Free tier works.     |
| `POLYMARKET_PRIVATE_KEY`  | Optional (trading only)                  | Leave blank — scanning is read-only.                  |
| `KALSHI_API_KEY`          | Optional (trading only)                  | Leave blank — scanning is read-only.                  |
| `KALSHI_PRIVATE_KEY`      | Optional (trading only)                  | Leave blank — scanning is read-only.                  |
| `TELEGRAM_BOT_TOKEN`      | Optional                                 | Enables Telegram alerts (see Step 6).                 |
| `TELEGRAM_CHAT_ID`        | Optional                                 | Chat to deliver alerts to.                            |
| `DISCORD_WEBHOOK_URL`     | Optional                                 | Enables Discord alerts.                               |
| `ARBSCANNER_SECRET_KEY`   | Required if you run `serve`              | Any random string; used by the FastAPI session layer. |
| `STRIPE_*`                | Optional                                 | Only needed if you are monetising a hosted deployment.|

A minimal `.env` for your first run looks like this:

```dotenv
ANTHROPIC_API_KEY=sk-ant-api03-...
ARBSCANNER_SECRET_KEY=please-change-me
```

---

## Step 3: Run the market matcher

The matcher is the heart of arbscanner. It pulls every active binary market from both
exchanges, normalizes the titles, uses sentence embeddings to find candidate pairs, and
then asks Claude to confirm or reject the ambiguous ones. The results are cached in
`data/matched_pairs.json` so you never have to pay for re-matching.

```bash
uv run arbscanner match
```

**What to expect on the first run:**

- ~30 seconds to download the `all-MiniLM-L6-v2` embedding model (one-time)
- ~20–60 seconds to fetch all markets via `pmxt` (roughly 4,000 Polymarket + 800 Kalshi
  binary markets at the time of writing)
- ~1–3 minutes to compute embeddings and run similarity search
- ~30–90 seconds of LLM calls for borderline candidates
- Total: **~3–5 minutes cold, ~30 seconds warm** (subsequent runs reuse the cache)

Example output:

```text
Initializing exchanges...
Fetching markets from Polymarket...
  Found 4127 binary markets
Fetching markets from Kalshi...
  Found 812 binary markets
Running matcher...
  Building embeddings for 4939 markets...
  Nearest-neighbor search (top-5)...
  LLM disambiguation on 64 borderline candidates...

Matching complete:
  Matched pairs: 143
  Rejected pairs: 27
```

Inspect what was matched:

```bash
uv run arbscanner pairs
```

```text
143 matched pairs (updated: 2026-04-10T14:22:01Z)

    1. Will the Fed cut rates in June 2026?
       ↔ KXFEDCUT-26JUN
       Confidence: 98.2% | Source: embedding+llm
    2. Will Bitcoin close above $150k on Dec 31?
       ↔ KXBTCD-26DEC31-150K
       Confidence: 96.4% | Source: embedding
    3. Will Taylor Swift win Album of the Year at the 2027 Grammys?
       ↔ KXGRAMMY-27-AOTY-TSWIFT
       Confidence: 94.1% | Source: embedding+llm
    ...
```

If you ever want to force a full rebuild (e.g. after new markets are listed), run
`uv run arbscanner match --rematch`.

---

## Step 4: Launch the terminal scanner

With pairs cached, start the live scanner:

```bash
uv run arbscanner scan
```

By default it refreshes every **30 seconds**, uses a **1% net edge threshold**, and
fetches order books in parallel across 8 workers. Override with flags:

```bash
uv run arbscanner scan --interval 15 --threshold 0.005 --max-workers 16
```

The dashboard clears the screen on each tick and redraws a Rich table:

```text
 arbscanner — 143 pairs tracked — refreshed 14:27:03
 +-------------------------------+------------+------------+--------+--------+-----------+------------+
 | Market                        | Poly YES   | Kalshi NO  | Gross  | Net    | Liquidity | $ Opp      |
 +-------------------------------+------------+------------+--------+--------+-----------+------------+
 | Fed cut rates in June 2026    | 0.41 ask   | 0.55 ask   |  4.0%  |  2.7%  |   $2,400  |   $64.80   |
 | BTC close > $150k Dec 31      | 0.18 ask   | 0.79 ask   |  3.0%  |  1.6%  |   $1,100  |   $17.60   |
 | Taylor Swift AOTY 2027        | 0.62 ask   | 0.36 ask   |  2.0%  |  0.7%  |     $480  |    $3.36   |
 | Senate flips GOP 2026         | 0.48 ask   | 0.51 ask   |  1.0%  | -0.2%  |   $5,200  |       —    |
 +-------------------------------+------------+------------+--------+--------+-----------+------------+
 Rows highlighted in green have net edge > 1.0%.  Press Ctrl+C to exit.
```

Column reference:

- **Market** — canonical title from the matched pair
- **Poly YES / Kalshi NO** — best ask price and side being hit on each exchange
- **Gross** — raw edge before fees (`1 - yes_ask - no_ask`)
- **Net** — edge after Polymarket taker (0.1%) and Kalshi taker (~1%) fees
- **Liquidity** — minimum dollar size available at the quoted prices on both legs
- **$ Opp** — expected profit in dollars (net edge × liquidity)

Every scan tick is logged to a local SQLite file at `data/opportunities.db` for later
backtesting or calibration analysis.

Press **Ctrl+C** to exit cleanly.

---

## Step 5 (optional): Launch the web dashboard

If you prefer a browser UI, arbscanner ships with a FastAPI server that exposes the same
scan data over HTTP and a simple dashboard page.

```bash
uv run arbscanner serve
```

```text
Starting web server on port 8000...
  Dashboard: http://localhost:8000/dashboard
  API:       http://localhost:8000/api/opportunities
  Landing:   http://localhost:8000/
```

Open [http://localhost:8000/dashboard](http://localhost:8000/dashboard) in your browser.
The dashboard polls `/api/opportunities` every 30 seconds and renders the same table as
the terminal scanner, plus a sparkline of historical edge per pair.

Bind to a different host/port or enable hot-reload for development:

```bash
uv run arbscanner serve --host 127.0.0.1 --port 8080 --reload
```

> The `serve` command requires `ARBSCANNER_SECRET_KEY` to be set in `.env`.

---

## Step 6 (optional): Enable Telegram alerts

To get a push notification whenever an opportunity crosses your alert threshold:

1. **Create a bot** — open a chat with [@BotFather](https://t.me/BotFather) on Telegram,
   send `/newbot`, and follow the prompts. Copy the bot token it gives you.
2. **Get your chat ID** — send any message to your new bot, then visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser. Look for
   `"chat":{"id":123456789,...}` in the JSON response. That number is your chat ID.
3. **Add both values to `.env`:**

   ```dotenv
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
   TELEGRAM_CHAT_ID=123456789
   ```

4. **Restart the scanner.** On startup you should see:

   ```text
   Alerts enabled (threshold: 1.5%)
   ```

Discord works the same way — drop a webhook URL into `DISCORD_WEBHOOK_URL` and restart.

---

## Troubleshooting

<details>
<summary><strong><code>pmxtjs</code> command not found after <code>npm install -g</code></strong></summary>

Your global `npm` prefix may not be on your `PATH`. Run `npm config get prefix` and add
`<prefix>/bin` to your `PATH`. On macOS with Homebrew Node this is usually
`/opt/homebrew/bin`; on Linux it might be `~/.npm-global/bin`.
</details>

<details>
<summary><strong>Sentence-transformers model download stalls</strong></summary>

The first `arbscanner match` run downloads ~400 MB of model weights from Hugging Face. If
you are behind a corporate proxy, set `HF_HUB_ENABLE_HF_TRANSFER=1` and configure
`HTTPS_PROXY`. You can also pre-download manually with
`uv run python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"`.
</details>

<details>
<summary><strong>No matched pairs found</strong></summary>

This almost always means the `pmxt` fetch returned empty. Run
`pmxt polymarket markets --limit 5` and `pmxt kalshi markets --limit 5` directly to
confirm the connector works. If the Kalshi side is empty, check that your system clock is
accurate — Kalshi rejects requests with drifted timestamps.
</details>

<details>
<summary><strong><code>ANTHROPIC_API_KEY</code> missing warning</strong></summary>

The matcher will still run without it — borderline candidates simply fall back to the
embedding similarity threshold. Matches will be slightly noisier but the scanner works.
Set the key whenever you want higher-precision matching.
</details>

<details>
<summary><strong>Scanner shows 0 opportunities forever</strong></summary>

Try lowering the threshold: `uv run arbscanner scan --threshold 0.0`. Most matched
markets sit at 0% edge most of the time — that is normal. If you also see no prices at
all, the order-book fetch is probably failing; re-run with `-v` for verbose logs.
</details>

---

## Next steps

- **[ARCHITECTURE.md](./ARCHITECTURE.md)** — how the data, matcher, engine, and delivery
  layers fit together, and where to extend the system
- **[DEPLOYMENT.md](./DEPLOYMENT.md)** — running arbscanner in Docker, systemd, or Fly.io
  for continuous background scanning
- **[API.md](./API.md)** — reference for the FastAPI endpoints powering the web dashboard
  and any downstream integrations you want to build

Happy hunting.
