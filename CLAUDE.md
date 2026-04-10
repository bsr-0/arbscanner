Arb Scanner MVP — Technical Plan
The core problem you’re solving: Scanning the same market across Polymarket and Kalshi simultaneously for price discrepancies. ￼ An existing open-source arb bot exists (realfishsam’s repo), but it’s a toy — it requires you to hardcode specific market URLs and only watches one pair at a time. ￼ Nobody has built a scanner that monitors all overlapping markets continuously and surfaces the best opportunities with context.
What makes this hard (and therefore valuable): The #1 unsolved problem is market matching. Polymarket calls it “Will the Fed cut rates in June?” and Kalshi calls it “KXFEDCUT-26JUN” — same event, different names, different IDs, different schemas. Automating this matching at scale is the moat. Everything else is plumbing.
Architecture

┌─────────────────────────────────────────┐
│            Data Layer (pmxt)            │
│  Polymarket · Kalshi · Limitless        │
│  fetch_markets() · fetch_order_book()   │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│         Market Matcher                   │
│  LLM-assisted fuzzy match + manual map  │
│  "Fed rate cut June" ↔ KXFEDCUT-26JUN  │
│  Output: matched_pairs.json             │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│         Arb Engine                       │
│  For each matched pair:                  │
│    - Pull best bid/ask from both books   │
│    - Compute: yes_A + no_B < $1.00?     │
│    - Compute: yes_B + no_A < $1.00?     │
│    - Subtract fees (Kalshi ~1%, Poly     │
│      0.1% taker)                         │
│    - Net edge > threshold? → ALERT       │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│         Delivery                         │
│  v1: Terminal dashboard (rich/textual)   │
│  v2: Web dashboard + email/Telegram      │
│  v3: One-click execution via pmxt        │
└─────────────────────────────────────────┘


Week 1: Core scanner (CLI)
Day 1-2: Data ingestion + market matching
	•	Use pmxt to pull all active markets from Polymarket + Kalshi
	•	Build the matcher: normalize titles (lowercase, strip punctuation, expand abbreviations), then use embedding similarity (sentence-transformers) to find candidate pairs. LLM pass to confirm/reject ambiguous matches. Cache confirmed pairs in a JSON file so you’re not re-matching every run.
	•	This is the hardest part and where your scraping instincts matter — Kalshi’s ticker naming conventions are structured (KXFEDCUT, KXINX, etc.) which helps.
Day 3-4: Arb calculation engine
	•	For each matched pair, pull order books from both platforms
	•	Calculate cross-platform arb: best_ask_poly_yes + best_ask_kalshi_no and vice versa
	•	Subtract realistic fees: Kalshi taker ~1-2% depending on price, Polymarket 0.1% taker
	•	Calculate net edge, size available at that edge (min liquidity on both sides), and expected profit in dollars
	•	Sort by edge × available size = expected dollar value
Day 5: CLI dashboard
	•	Rich/Textual terminal UI that refreshes every 30s
	•	Table: Market | Poly YES | Kalshi NO | Gross Edge | Net Edge | Liquidity | $ Opportunity
	•	Highlight anything with net edge > 1%
	•	Log historical opportunities to SQLite for backtesting later
Week 2: Productize
Day 6-7: Web dashboard
	•	FastAPI backend serving the scanner data
	•	Simple React frontend (or even just an HTML artifact) showing live arb table
	•	Add Telegram/Discord webhook for alerts when edge > threshold
Day 8-9: Calibration layer (your differentiator)
	•	Pull Jon Becker’s historical dataset (Parquet, publicly available)
	•	Compute category-level calibration curves: how accurate are markets by category × time-to-resolution?
	•	For each arb opportunity, show: “This is a Pop Culture market 30 days from resolution — historically these are mispriced by ~8 points. Edge is likely real.” vs. “This is a Politics market 2 days from resolution — historically very efficient. Edge probably reflects execution risk.”
	•	This is where your prediction-core calibration work directly applies
Day 10: Landing page + payment
	•	Stripe checkout, $29/mo
	•	Free tier: delayed alerts (5 min lag), top 3 opportunities only
	•	Paid: real-time, full table, calibration context, Telegram alerts
Tech stack
	•	Python 3.12 + uv (you’re already using this)
	•	pmxt for data (don’t reinvent the exchange connectors)
	•	sentence-transformers for market matching embeddings
	•	Claude API for ambiguous match confirmation
	•	FastAPI for the web backend
	•	SQLite/DuckDB for opportunity logging (ironic, but fine at this scale)
	•	React or plain HTML for the dashboard
	•	Telegram Bot API for alerts
Why this wins vs. what exists
PolymarketAnalytics already lets you search across both platforms and find price differences ￼, but it’s a manual tool — you browse, you compare. The open-source arb bot watches one hardcoded pair. What nobody has built is: automated matching across all overlapping markets, continuous monitoring, net-of-fees calculation, and — critically — calibration context that tells you whether the edge is real or just noise.
The calibration layer is the long-term moat. Anyone can build a price comparator. Nobody else has the calibration-aware scoring that says “this arb in a low-liquidity entertainment market is probably real edge, but this arb in a high-liquidity politics market is probably a stale quote.”
