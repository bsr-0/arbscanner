# ArbScanner REST API Reference

Complete reference for the ArbScanner HTTP API. ArbScanner is a cross-platform
prediction-market arbitrage scanner that monitors matched markets on Polymarket
and Kalshi and surfaces opportunities where the net-of-fees edge exceeds a
configurable threshold.

This document covers every endpoint served by the FastAPI application defined
in `src/arbscanner/web.py` (version `0.2.0`).

---

## Table of Contents

1. [Overview](#overview)
2. [Base URL](#base-url)
3. [Authentication](#authentication)
4. [Content Types](#content-types)
5. [Common Error Format](#common-error-format)
6. [Rate Limiting](#rate-limiting)
7. [Endpoints](#endpoints)
   - [HTML Pages](#html-pages)
     - [`GET /`](#get-)
     - [`GET /dashboard`](#get-dashboard)
   - [JSON API](#json-api)
     - [`GET /api/opportunities`](#get-apiopportunities)
     - [`GET /api/pairs`](#get-apipairs)
     - [`GET /api/stats`](#get-apistats)
     - [`GET /api/calibration`](#get-apicalibration)
   - [Stripe Integration](#stripe-integration)
     - [`GET /api/stripe/checkout`](#get-apistripecheckout)
     - [`POST /api/stripe/webhook`](#post-apistripewebhook)
8. [Data Models](#data-models)
9. [Changelog](#changelog)

---

## Overview

The ArbScanner backend is a FastAPI application that exposes:

- **HTML pages** for end-user browsing (landing page + live dashboard).
- **JSON endpoints** under `/api/*` that back the dashboard and are suitable
  for programmatic consumption.
- **Stripe endpoints** for subscription checkout and webhook event handling.

All JSON responses are returned as `application/json` with UTF-8 encoding.
All timestamps are ISO 8601 strings in UTC unless otherwise noted.

## Base URL

During local development the server listens on:

```
http://localhost:8000
```

In production the base URL is whatever host the application is deployed to.
For the examples below we use `http://localhost:8000`; replace this with your
actual host when invoking the API remotely.

## Authentication

**None.** There is currently no authentication layer on any endpoint. The
API is intended to be run behind a reverse proxy or on a private network.

A token-based auth scheme (likely scoped API keys keyed to Stripe customer IDs
for paid tier feature gating) is planned; see the [Changelog](#changelog).

## Content Types

| Direction | Content Type                        |
| --------- | ----------------------------------- |
| Request   | `application/json` (where relevant) |
| Response  | `application/json`                  |
| HTML pgs  | `text/html; charset=utf-8`          |

The Stripe webhook endpoint reads the raw request body in order to verify the
`stripe-signature` header, so clients posting to it must send the exact bytes
Stripe delivered (do not re-serialize).

## Common Error Format

Errors raised via `fastapi.HTTPException` follow FastAPI's default shape:

```json
{
  "detail": "Human-readable error message"
}
```

Validation errors raised by FastAPI/Pydantic on query parameters (e.g. out-of-
range `limit`) return HTTP 422 with the standard FastAPI validation payload:

```json
{
  "detail": [
    {
      "loc": ["query", "limit"],
      "msg": "ensure this value is less than or equal to 500",
      "type": "value_error.number.not_le"
    }
  ]
}
```

## Rate Limiting

There is currently **no server-side rate limiting** on any endpoint. The
service is expected to be deployed behind a CDN or reverse proxy (e.g.
Cloudflare, nginx with `limit_req`) that applies request limits. A first-party
rate limiter — likely IP-based for anonymous callers and key-based for
authenticated ones — is on the roadmap.

Please be considerate: the dashboard polls `/api/opportunities` every few
seconds and the matched pairs cache is cheap to read, but please avoid
hammering `/api/stats` at more than ~1 Hz per client.

---

## Endpoints

### HTML Pages

#### `GET /`

Render the marketing / landing page.

- **Method:** `GET`
- **Path:** `/`
- **Response:** `200 OK` — `text/html`
- **Query parameters:** none

The template is rendered with the following context, pulled from the
matched-pairs cache and historical opportunity stats:

| Context key         | Type   | Description                                   |
| ------------------- | ------ | --------------------------------------------- |
| `matched_pairs`     | `int`  | Current count of confirmed matched pairs.     |
| `stats`             | `dict` | Historical edge stats (see `/api/stats`).     |
| `stripe_configured` | `bool` | Whether Stripe env vars are present.          |

##### Example

```bash
curl -sS http://localhost:8000/ -o landing.html
```

---

#### `GET /dashboard`

Render the live arbitrage dashboard (HTML version of the terminal UI). The
page fetches data from the JSON endpoints via client-side JavaScript.

- **Method:** `GET`
- **Path:** `/dashboard`
- **Response:** `200 OK` — `text/html`
- **Query parameters:** none

##### Example

```bash
curl -sS http://localhost:8000/dashboard -o dashboard.html
```

---

### JSON API

#### `GET /api/opportunities`

Return recent arbitrage opportunities logged to the local SQLite database,
filtered by minimum net edge and a rolling time window, sorted by expected
dollar profit descending.

- **Method:** `GET`
- **Path:** `/api/opportunities`

##### Query parameters

| Name       | Type    | Default | Constraints          | Description                                                                                                  |
| ---------- | ------- | ------- | -------------------- | ------------------------------------------------------------------------------------------------------------ |
| `limit`    | `int`   | `50`    | `1 <= limit <= 500`  | Maximum number of rows to return.                                                                            |
| `min_edge` | `float` | `0.0`   | `>= 0.0`             | Minimum net edge (as a decimal, e.g. `0.02` for 2%). Rows below this threshold are excluded.                 |
| `hours`    | `int`   | `24`    | `1 <= hours <= 168`  | Time window for the query. Only opportunities logged in the last `hours` hours are returned (max = 1 week). |

##### Response

`200 OK` — a JSON array of opportunity objects sorted by `expected_profit`
descending.

```json
[
  {
    "timestamp": "2026-04-10T14:32:11.482913+00:00",
    "poly_market_id": "0xabc123...",
    "kalshi_market_id": "KXFEDCUT-26JUN",
    "market_title": "Will the Fed cut rates in June 2026?",
    "direction": "poly_yes_kalshi_no",
    "gross_edge": 0.034,
    "net_edge": 0.021,
    "available_size": 1250.0,
    "expected_profit": 26.25,
    "poly_price": 0.48,
    "kalshi_price": 0.50
  },
  {
    "timestamp": "2026-04-10T14:31:58.102004+00:00",
    "poly_market_id": "0xdef456...",
    "kalshi_market_id": "KXBTCMAX-26",
    "market_title": "Will Bitcoin hit $150k in 2026?",
    "direction": "poly_no_kalshi_yes",
    "gross_edge": 0.028,
    "net_edge": 0.017,
    "available_size": 800.0,
    "expected_profit": 13.60,
    "poly_price": 0.43,
    "kalshi_price": 0.42
  }
]
```

##### Field reference

| Field              | Type     | Description                                                                                 |
| ------------------ | -------- | ------------------------------------------------------------------------------------------- |
| `timestamp`        | `string` | ISO 8601 UTC time the opportunity was recorded.                                              |
| `poly_market_id`   | `string` | Polymarket market identifier.                                                                |
| `kalshi_market_id` | `string` | Kalshi market ticker.                                                                        |
| `market_title`     | `string` | Human-readable title of the matched market.                                                  |
| `direction`        | `string` | One of `poly_yes_kalshi_no` or `poly_no_kalshi_yes`. Indicates which side to buy on each venue. |
| `gross_edge`       | `float`  | Pre-fee arbitrage edge as a decimal (e.g. `0.034` = 3.4 points).                             |
| `net_edge`         | `float`  | Post-fee arbitrage edge as a decimal.                                                        |
| `available_size`   | `float`  | Minimum liquidity across both legs (in contracts).                                           |
| `expected_profit`  | `float`  | `net_edge * available_size` in dollars.                                                      |
| `poly_price`       | `float`  | Best executable price on Polymarket for the chosen leg (0.0–1.0).                            |
| `kalshi_price`     | `float`  | Best executable price on Kalshi for the chosen leg (0.0–1.0).                                |

##### Status codes

| Code  | Meaning                                                   |
| ----- | --------------------------------------------------------- |
| `200` | Success. An empty array is returned if nothing matches.   |
| `422` | Query parameter validation failure.                       |
| `500` | Unexpected server error (e.g. database unavailable).      |

##### Example

```bash
curl -sS 'http://localhost:8000/api/opportunities?limit=10&min_edge=0.01&hours=6'
```

---

#### `GET /api/pairs`

Return the current list of confirmed matched market pairs from the on-disk
matcher cache. This is what drives the scanner's core "which markets to
compare" question.

- **Method:** `GET`
- **Path:** `/api/pairs`
- **Query parameters:** none

##### Response

`200 OK` — a single object describing the cache.

```json
{
  "updated_at": "2026-04-10T13:00:00+00:00",
  "count": 2,
  "pairs": [
    {
      "poly_market_id": "0xabc123...",
      "poly_title": "Will the Fed cut rates in June 2026?",
      "kalshi_market_id": "KXFEDCUT-26JUN",
      "kalshi_title": "Fed rate cut June 2026",
      "confidence": 0.94,
      "source": "embedding+llm"
    },
    {
      "poly_market_id": "0xdef456...",
      "poly_title": "Will Bitcoin hit $150k in 2026?",
      "kalshi_market_id": "KXBTCMAX-26",
      "kalshi_title": "BTC above $150,000 by end of 2026",
      "confidence": 1.0,
      "source": "manual"
    }
  ]
}
```

##### Field reference

| Field               | Type              | Description                                                          |
| ------------------- | ----------------- | -------------------------------------------------------------------- |
| `updated_at`        | `string`          | ISO 8601 timestamp of the last cache write.                          |
| `count`             | `int`             | Number of pairs in the cache.                                        |
| `pairs[]`           | `array[object]`   | List of matched pair objects.                                        |
| `.poly_market_id`   | `string`          | Polymarket market identifier.                                        |
| `.poly_title`       | `string`          | Human-readable Polymarket title.                                     |
| `.kalshi_market_id` | `string`          | Kalshi market ticker.                                                |
| `.kalshi_title`     | `string`          | Human-readable Kalshi title.                                         |
| `.confidence`       | `float`           | Matcher confidence in `[0.0, 1.0]`.                                  |
| `.source`           | `string`          | How the match was made: `"embedding"`, `"embedding+llm"`, `"manual"`. |

##### Status codes

| Code  | Meaning                                             |
| ----- | --------------------------------------------------- |
| `200` | Success. Empty `pairs` array if the cache is empty. |
| `500` | Cache file unreadable.                              |

##### Example

```bash
curl -sS http://localhost:8000/api/pairs
```

---

#### `GET /api/stats`

Return summary statistics combining the live matched-pairs cache with
aggregate metrics over all logged opportunities in the SQLite database.

- **Method:** `GET`
- **Path:** `/api/stats`
- **Query parameters:** none

##### Response

`200 OK` — a single object. If there are no logged opportunities yet, the
historical fields are omitted (only `matched_pairs` and `uptime_seconds` will
be present).

```json
{
  "matched_pairs": 137,
  "uptime_seconds": 43221,
  "total_opportunities": 8421,
  "avg_net_edge": 0.0143,
  "max_net_edge": 0.0612,
  "avg_profit": 18.72,
  "total_profit": 157679.11
}
```

##### Field reference

| Field                 | Type    | Description                                                                      |
| --------------------- | ------- | -------------------------------------------------------------------------------- |
| `matched_pairs`       | `int`   | Current count of confirmed matched pairs in the matcher cache.                   |
| `uptime_seconds`      | `int`   | Seconds since the FastAPI process started.                                       |
| `total_opportunities` | `int`   | Lifetime count of logged arb opportunities.                                      |
| `avg_net_edge`        | `float` | Average net-of-fees edge across all logged opportunities.                        |
| `max_net_edge`        | `float` | Largest net edge ever observed.                                                  |
| `avg_profit`          | `float` | Average expected dollar profit across all logged opportunities.                  |
| `total_profit`        | `float` | Sum of expected dollar profit across all logged opportunities.                   |

##### Status codes

| Code  | Meaning |
| ----- | ------- |
| `200` | Success. |

##### Example

```bash
curl -sS http://localhost:8000/api/stats
```

---

#### `GET /api/calibration`

Compute the calibration context for a hypothetical opportunity. This is the
"is this edge real?" endpoint — it looks up the historical mispricing for a
given category/time-bucket combination and tells the caller whether their
candidate edge is likely to represent true alpha or just noise / execution
risk.

- **Method:** `GET`
- **Path:** `/api/calibration`

##### Query parameters

| Name                 | Type          | Default      | Constraints | Description                                                                                                                       |
| -------------------- | ------------- | ------------ | ----------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `category`           | `string`      | `"politics"` | —           | Market category. Normalized to one of `politics`, `economics`, `sports`, `entertainment`, `crypto`, `other`. Aliases accepted.    |
| `days_to_resolution` | `int \| null` | `null`       | —           | Days until the market resolves. When omitted, the resolution date is treated as unknown and the `90+` bucket is used.             |
| `net_edge`           | `float`       | `0.01`       | —           | Candidate net edge as a decimal (e.g. `0.025` = 2.5 points).                                                                      |

##### Response

`200 OK` — a single calibration context object.

```json
{
  "category": "economics",
  "days_to_resolution": 45,
  "time_bucket": "30-90",
  "avg_mispricing": 4.5,
  "edge_likely_real": false,
  "confidence_note": "This is a economics market with 45 days to resolution. Historically, these are mispriced by ~4.5 points. Your edge of 2.5 points is within normal range — may be noise or execution risk."
}
```

##### Field reference

| Field                | Type         | Description                                                                                        |
| -------------------- | ------------ | -------------------------------------------------------------------------------------------------- |
| `category`           | `string`     | Normalized category name.                                                                          |
| `days_to_resolution` | `int\|null`  | Echo of the `days_to_resolution` parameter (may be `null`).                                        |
| `time_bucket`        | `string`     | One of `"0-7"`, `"7-30"`, `"30-90"`, `"90+"`.                                                      |
| `avg_mispricing`     | `float`      | Average historical mispricing for the `(category, time_bucket)` cell, in percentage points (0–100). |
| `edge_likely_real`   | `bool`       | `true` if `net_edge * 100 > avg_mispricing`.                                                       |
| `confidence_note`    | `string`     | Human-readable explanation suitable for surfacing in a dashboard tooltip.                          |

##### Status codes

| Code  | Meaning                                             |
| ----- | --------------------------------------------------- |
| `200` | Success.                                            |
| `422` | Query parameter validation failure.                 |

##### Example

```bash
curl -sS 'http://localhost:8000/api/calibration?category=entertainment&days_to_resolution=30&net_edge=0.02'
```

---

### Stripe Integration

Both Stripe endpoints require the server to be configured with the following
environment variables:

- `STRIPE_SECRET_KEY` — Stripe API secret key.
- `STRIPE_PRICE_ID` — Price ID for the subscription product (checkout only).
- `STRIPE_WEBHOOK_SECRET` — Signing secret for webhook verification.

If these are not configured the endpoints return `503 Service Unavailable`
with `{"detail": "Stripe not configured"}`.

---

#### `GET /api/stripe/checkout`

Create a Stripe Checkout Session for the paid subscription plan and return
its redirect URL. The frontend is expected to redirect the user to
`checkout_url` immediately.

- **Method:** `GET`
- **Path:** `/api/stripe/checkout`
- **Query parameters:** none

The session is created with the following parameters:

- `mode = "subscription"`
- `line_items = [{price: STRIPE_PRICE_ID, quantity: 1}]`
- `success_url = "/?payment=success"`
- `cancel_url = "/?payment=cancelled"`

##### Response

`200 OK`:

```json
{
  "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_a1b2c3..."
}
```

##### Status codes

| Code  | Meaning                                                                                 |
| ----- | --------------------------------------------------------------------------------------- |
| `200` | Checkout session created successfully.                                                   |
| `503` | `STRIPE_SECRET_KEY` or `STRIPE_PRICE_ID` is not configured on the server.               |
| `500` | Upstream Stripe API error (raised as an unhandled `stripe.error.StripeError`).          |

##### Error response — not configured

```json
{
  "detail": "Stripe not configured"
}
```

##### Example

```bash
curl -sS http://localhost:8000/api/stripe/checkout
```

---

#### `POST /api/stripe/webhook`

Receive Stripe webhook events for subscription lifecycle management. The
handler verifies the `stripe-signature` header against
`STRIPE_WEBHOOK_SECRET` before processing the event.

- **Method:** `POST`
- **Path:** `/api/stripe/webhook`
- **Content-Type:** `application/json` (raw body as delivered by Stripe)
- **Required headers:**
  - `stripe-signature` — signature header produced by Stripe. Required.

##### Request body

The raw JSON payload from Stripe. Do **not** re-serialize; forward the exact
bytes Stripe delivered, otherwise the signature check will fail.

Currently handled event types:

| Event type                       | Behavior                                                                        |
| -------------------------------- | ------------------------------------------------------------------------------- |
| `checkout.session.completed`     | Logs `customer_email` from the session at INFO level.                           |
| `customer.subscription.deleted`  | Logs the subscription ID at INFO level.                                         |
| _any other event_                | Silently accepted (no-op); the handler still returns `200 OK` with `status=ok`. |

##### Example request body (Stripe-delivered)

```json
{
  "id": "evt_1NsxX82eZvKYlo2C...",
  "object": "event",
  "type": "checkout.session.completed",
  "data": {
    "object": {
      "id": "cs_test_a1b2c3...",
      "object": "checkout.session",
      "customer_email": "user@example.com",
      "mode": "subscription"
    }
  }
}
```

##### Response

`200 OK`:

```json
{
  "status": "ok"
}
```

##### Status codes

| Code  | Meaning                                                                                         |
| ----- | ----------------------------------------------------------------------------------------------- |
| `200` | Event received and processed (or intentionally ignored).                                         |
| `400` | Invalid or missing signature, or malformed payload (`Invalid webhook signature`).               |
| `503` | `STRIPE_SECRET_KEY` not configured on the server (`Stripe not configured`).                      |

##### Error response — invalid signature

```json
{
  "detail": "Invalid webhook signature"
}
```

##### Example

```bash
# Typical local testing via the Stripe CLI:
stripe listen --forward-to http://localhost:8000/api/stripe/webhook

# Manually triggering an event (will be signed by the CLI):
stripe trigger checkout.session.completed
```

A raw `curl` example is shown below, but note that unless the signature in
the `stripe-signature` header matches the body + webhook secret the server
will return 400. Use the Stripe CLI for realistic testing.

```bash
curl -sS -X POST http://localhost:8000/api/stripe/webhook \
  -H 'Content-Type: application/json' \
  -H 'stripe-signature: t=1712760000,v1=deadbeef...' \
  --data-binary @stripe_event.json
```

---

## Data Models

The JSON shapes returned by the API map 1:1 onto dataclasses defined in
`src/arbscanner/models.py` and `src/arbscanner/calibration.py`. The most
relevant source-of-truth types are:

### `MatchedPair`

Backs entries in `GET /api/pairs`.

```python
@dataclass
class MatchedPair:
    poly_market_id: str
    poly_title: str
    kalshi_market_id: str
    kalshi_title: str
    confidence: float
    source: str          # "embedding" | "embedding+llm" | "manual"
    matched_at: str      # ISO 8601
    poly_yes_outcome_id: str = ""
    poly_no_outcome_id: str = ""
    kalshi_yes_outcome_id: str = ""
    kalshi_no_outcome_id: str = ""
```

Note: the `matched_at` and the four outcome-ID fields exist on the dataclass
but are **not** included in the `/api/pairs` response payload.

### `MatchedPairsCache`

Backs the top-level envelope of `GET /api/pairs`.

```python
@dataclass
class MatchedPairsCache:
    version: int = 1
    updated_at: str = ""
    pairs: list[MatchedPair] = []
    rejected: list[str] = []  # "poly_id::kalshi_id"
```

The `version` and `rejected` fields are not exposed via the HTTP API.

### `ArbOpportunity`

Represents rows in the `opportunities` SQLite table that back
`GET /api/opportunities`.

```python
@dataclass
class ArbOpportunity:
    poly_title: str
    kalshi_title: str
    poly_market_id: str
    kalshi_market_id: str
    direction: str       # "poly_yes_kalshi_no" | "poly_no_kalshi_yes"
    poly_price: float
    kalshi_price: float
    gross_edge: float
    net_edge: float
    available_size: float
    expected_profit: float
    timestamp: datetime
```

The API response collapses `poly_title` and `kalshi_title` into a single
`market_title` field (pulled directly from the SQLite row).

### `CalibrationContext`

Backs `GET /api/calibration`.

```python
@dataclass
class CalibrationContext:
    category: str
    days_to_resolution: int | None
    time_bucket: str        # "0-7" | "7-30" | "30-90" | "90+"
    avg_mispricing: float   # 0-100 points
    edge_likely_real: bool
    confidence_note: str
```

---

## Changelog

### v0.2.0 — Current

- Initial public-facing JSON API covering opportunities, pairs, stats, and
  calibration endpoints.
- Stripe Checkout + webhook endpoints added for the paid tier.
- Landing page and live HTML dashboard pages exposed.

### Planned

- **v0.3.0** — First-party API key auth for paid-tier feature gating.
- **v0.3.0** — Server-side rate limiting (per-IP and per-key).
- **v0.4.0** — WebSocket endpoint for real-time opportunity streaming.
- **v0.4.0** — `GET /api/markets` to expose raw per-platform market metadata.
