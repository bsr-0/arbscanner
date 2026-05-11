# ArbScanner Quality Gates

This document defines the minimum success criteria for the opportunity
snapshot / prediction-persistence work. It is intentionally lean: one checklist
for what must be true before we call the work "better", and one list of
failure conditions that should block rollout even if the feature looks good in
the UI.

The goal is concrete improvement in data quality and operator trust, not
process for its own sake.

---

## Principles

- Define metrics before implementation when possible.
- Measure on fixed datasets, not hand-picked examples.
- Separate `original scan-time snapshot` rows from `reconstructed/backfilled`
  rows in every report.
- Prefer exact round-trip assertions over visual inspection.
- Do not present reconstructed values as if they were original model outputs.

---

## Scope

These gates apply to:

- schema changes to `opportunities`
- migration wiring in `db.py`
- persisted prediction / calibration snapshots
- `/api/opportunities` contract changes
- backfill of legacy rows
- dashboard and static export consumers

---

## Gate 1: Schema + Migration Safety

### Success

- A fresh database boots directly to the latest schema version.
- A pre-change database migrates to the latest schema without losing rows.
- Startup uses `apply_migrations()` as the single schema-upgrade path.
- Re-running startup on an already-current database is a no-op.

### Failure

- Fresh and migrated databases end up with different schemas.
- Any migration drops or corrupts existing `opportunities` rows.
- Startup can leave the database partially upgraded.
- New schema fields require manual SQL outside the migration system.

### Evidence

- Tests covering:
  - `fresh -> latest`
  - `legacy -> latest`
  - `latest -> latest`
- Row-count equality before and after migration on legacy fixtures.
- Schema diff or `PRAGMA table_info(opportunities)` snapshot in tests.

---

## Gate 2: Scan-Time Snapshot Persistence

### Success

- Every newly logged opportunity persists the exact scan-time snapshot fields
  available at detection time.
- Prediction source is explicit for every persisted prediction.
- Newly logged rows no longer depend on the mutable matched-pair cache to show
  prediction or calibration context.

### Failure

- A newly logged row shows different prediction values after a restart or cache
  change.
- Snapshot fields are null even though the engine had those values at scan time.
- The API must recompute prediction/calibration for new rows from external or
  mutable state.

### Evidence

- Round-trip tests: `scan -> log -> fetch -> API` preserves the same values.
- Fixture rows where engine output is known ahead of time.
- Coverage metric:
  - `snapshot_prediction_coverage = rows_with_prediction / new_rows`
  - `snapshot_source_coverage = rows_with_prediction_source / new_rows`

Target:

- `snapshot_prediction_coverage = 100%` when prediction input existed at scan time.
- `snapshot_source_coverage = 100%` for rows with any prediction.

---

## Gate 3: Stable API Contract

### Success

- `/api/opportunities` returns first-class prediction fields, not just raw leg
  prices.
- API consumers can distinguish `original`, `reconstructed`, and `derived`
  prediction values.
- Old rows degrade gracefully through explicit fallback behavior.

### Failure

- Frontends still have to invent core prediction fields from scratch.
- The same row returns different prediction data solely because the pair cache
  changed later.
- New fields break free/pro tier behavior or legacy-row reads.

### Evidence

- Contract tests for:
  - new persisted rows
  - legacy rows
  - rows without calibration
  - rows with fair-value enrichment
- Golden JSON fixtures for representative responses.

Recommended response fields:

- `prediction_yes`
- `prediction_yes_low`
- `prediction_yes_high`
- `prediction_source`
- `prediction_origin` (`original`, `reconstructed`, `derived`)

---

## Gate 4: Source Transparency and Uncertainty

### Success

- Every non-null prediction is source-labeled.
- When only a price-implied range exists, the API/UI preserves the uncertainty
  band rather than pretending there is a precise model output.
- Model-backed predictions remain distinguishable from arithmetic derivations.

### Failure

- Derived midpoint is shown as authoritative without source labeling.
- A single scalar is shown where only a band is justified.
- Different prediction types are mixed together in reports with no attribution.

### Evidence

- UI/API snapshots for:
  - implied-band only
  - sportsbook fair value
  - polling fair value
  - crypto fair value
- Manual review of a blinded sample followed by source reveal.

Primary metric:

- `source_labeled_prediction_rate = predictions_with_source / rows_with_prediction`

Target:

- `source_labeled_prediction_rate = 100%`

---

## Gate 5: Backfill Integrity

### Success

- Legacy rows gain at least reconstructed implied probability bands from stored
  prices.
- Reconstructed values are explicitly tagged as reconstructed.
- Backfill is safe to rerun and does not overwrite higher-quality original
  snapshot data.

### Failure

- Backfilled rows are indistinguishable from original scan-time snapshots.
- Backfill can overwrite non-null original prediction fields.
- Backfill depends on current external state without provenance tagging.

### Evidence

- Before/after counts:
  - `% rows with prediction`
  - `% rows with prediction_source`
  - `% rows marked reconstructed`
- Idempotency test: running backfill twice makes no second change.
- Sample audit against raw `poly_price` / `kalshi_price`.

---

## Gate 6: Consumer Consistency

### Success

- The dashboard and static export consume the same prediction semantics.
- The browser is a presentation layer, not the source of truth for prediction.
- The same opportunity ID renders the same prediction values across API and
  static export when sourced from the same row.

### Failure

- Dashboard and export disagree for the same row.
- Prediction logic exists only in browser JS for a core field.
- A consumer silently drops source/provenance fields.

### Evidence

- Tests comparing API payload rows to export payload rows.
- UI fixture tests for representative rows.

Primary metric:

- `consumer_prediction_mismatch_rate = mismatched_rows / compared_rows`

Target:

- `consumer_prediction_mismatch_rate = 0%`

---

## Evaluation Protocol

Use the same protocol for every major iteration:

1. Freeze a holdout database snapshot.
2. Record pre-change baseline metrics on that snapshot.
3. Run migrated code against the same snapshot.
4. Report results separately for:
   - original persisted rows
   - reconstructed/backfilled rows
   - source classes (`implied_band`, `odds_api`, `538_approval`, `crypto_model`, etc.)
5. Reject results that only improve via denominator changes or sampling changes.

### Baseline Metrics

- row count
- null prediction rate
- null prediction-source rate
- source-labeled prediction rate
- consumer prediction mismatch rate
- migration test pass rate
- API latency p50 / p95 for `/api/opportunities`

---

## Minimal Rollout Bar

Do not call the work complete until all of the following are true:

- migration tests pass
- snapshot round-trip tests pass
- legacy fallback tests pass
- source-labeled prediction rate is 100% for non-null predictions
- consumer prediction mismatch rate is 0% on test fixtures
- backfill is idempotent

If any of those fail, the work may still be useful in development, but it is
not ready to be treated as a trustworthy historical record.
