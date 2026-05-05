#!/usr/bin/env python3
"""Transform Jon Becker's prediction-market-analysis Parquet files into
the calibration schema expected by arbscanner.

Becker's dataset: https://github.com/Jon-Becker/prediction-market-analysis
Download: https://s3.jbecker.dev/data.tar.zst  (extract to get data/ directory)

Usage:
    # Transform from extracted Becker data directory:
    python scripts/transform_becker.py /path/to/becker-prediction-data/data

    # Specify output path (default: data/calibration/historical_becker.parquet):
    python scripts/transform_becker.py /path/to/data -o data/calibration/custom.parquet

    # Then compute calibration curves:
    uv run arbscanner calibrate --data-file data/calibration/historical_becker.parquet
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

# Add project root to path so we can import arbscanner
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from arbscanner.calibration import normalize_category


def transform_kalshi(data_dir: Path) -> pd.DataFrame:
    """Transform Kalshi markets Parquet files into calibration schema.

    Becker schema:
        ticker, event_ticker, market_type, title, yes_sub_title, no_sub_title,
        status, yes_bid, yes_ask, no_bid, no_ask, last_price (cents 1-99),
        volume, volume_24h, open_interest, result, created_time, open_time,
        close_time, _fetched_at
    """
    kalshi_dir = data_dir / "kalshi" / "markets"
    if not kalshi_dir.exists():
        print(f"  Kalshi markets directory not found: {kalshi_dir}")
        return pd.DataFrame()

    parquet_files = list(kalshi_dir.glob("*.parquet"))
    if not parquet_files:
        print(f"  No Parquet files found in {kalshi_dir}")
        return pd.DataFrame()

    print(f"  Reading {len(parquet_files)} Kalshi market file(s)...")
    dfs = [pd.read_parquet(f) for f in parquet_files]
    df = pd.concat(dfs, ignore_index=True)
    print(f"  Raw Kalshi markets: {len(df)}")

    # Filter to resolved/settled markets with a known result
    df = df[df["status"].isin(["settled", "closed", "finalized"])].copy()
    df = df[df["result"].isin(["yes", "no"])].copy()
    print(f"  Resolved Kalshi markets: {len(df)}")

    if df.empty:
        return pd.DataFrame()

    # Map to calibration schema
    out = pd.DataFrame()
    out["market_id"] = df["ticker"].values
    out["category"] = df["event_ticker"].apply(_kalshi_ticker_to_category)
    out["resolution_date"] = pd.to_datetime(df["close_time"], utc=True, errors="coerce")
    out["created_date"] = pd.to_datetime(df["created_time"], utc=True, errors="coerce")
    # last_price is in cents (1-99) → convert to 0-1
    out["final_price"] = df["last_price"].astype(float) / 100.0
    out["resolved_yes"] = df["result"] == "yes"
    out["exchange"] = "kalshi"
    out["title"] = df["title"]

    return out.dropna(subset=["resolution_date", "created_date", "final_price"])


def transform_polymarket(data_dir: Path) -> pd.DataFrame:
    """Transform Polymarket markets Parquet files into calibration schema.

    Becker schema:
        id, condition_id, question, slug, outcomes (JSON), outcome_prices (JSON),
        volume, liquidity, active, closed, end_date, created_at, _fetched_at
    """
    poly_dir = data_dir / "polymarket" / "markets"
    if not poly_dir.exists():
        print(f"  Polymarket markets directory not found: {poly_dir}")
        return pd.DataFrame()

    parquet_files = list(poly_dir.glob("*.parquet"))
    if not parquet_files:
        print(f"  No Parquet files found in {poly_dir}")
        return pd.DataFrame()

    print(f"  Reading {len(parquet_files)} Polymarket market file(s)...")
    dfs = [pd.read_parquet(f) for f in parquet_files]
    df = pd.concat(dfs, ignore_index=True)
    print(f"  Raw Polymarket markets: {len(df)}")

    # Filter to closed markets
    df = df[df["closed"] == True].copy()  # noqa: E712
    print(f"  Closed Polymarket markets: {len(df)}")

    if df.empty:
        return pd.DataFrame()

    # Parse outcome_prices to get final YES price
    df["final_price"] = df["outcome_prices"].apply(_parse_poly_yes_price)

    # Determine resolution: if final_price > 0.5, resolved YES
    # (Polymarket doesn't have a direct "result" field in Becker's schema,
    # but resolved markets snap to 0 or 1)
    df["resolved_yes"] = df["final_price"] > 0.5

    # Map to calibration schema
    out = pd.DataFrame()
    out["market_id"] = df["id"].values
    out["category"] = df["question"].apply(_poly_question_to_category)
    out["resolution_date"] = pd.to_datetime(df["end_date"], utc=True, errors="coerce")
    out["created_date"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
    out["final_price"] = df["final_price"].astype(float)
    out["resolved_yes"] = df["resolved_yes"]
    out["exchange"] = "polymarket"
    out["title"] = df["question"]

    return out.dropna(subset=["resolution_date", "created_date", "final_price"])


def _kalshi_ticker_to_category(event_ticker: str) -> str:
    """Map Kalshi event tickers to categories.

    Kalshi tickers have structured prefixes:
    KXFEDCUT → economics, KXINX → economics, KXELECTION → politics, etc.
    """
    if not isinstance(event_ticker, str):
        return "other"
    t = event_ticker.upper()

    economics_prefixes = (
        "KXFED", "KXINX", "KXCPI", "KXGDP", "KXJOBS", "KXUNEMPLOY",
        "KXRATES", "KXINFLAT", "KXSP500", "KXNASDAQ", "KXDOW",
        "KXFTSE", "KXOIL", "KXGOLD", "KXGAS", "KXBITCOIN", "KXETH",
        "FED", "INX", "CPI", "GDP", "JOBS",
    )
    politics_prefixes = (
        "KXELECT", "KXPRES", "KXSENATE", "KXHOUSE", "KXGOV",
        "KXTRUMP", "KXBIDEN", "KXHARRIS", "KXPOLL",
        "ELECT", "PRES", "SENATE", "HOUSE",
    )
    sports_prefixes = (
        "KXNFL", "KXNBA", "KXMLB", "KXNHL", "KXSOCCER", "KXMMA",
        "KXTENNIS", "KXGOLF", "KXF1",
        "NFL", "NBA", "MLB", "NHL",
    )
    entertainment_prefixes = (
        "KXOSCAR", "KXEMMY", "KXGRAMMY", "KXMOVIE", "KXTV",
        "OSCAR", "EMMY", "GRAMMY",
    )
    crypto_prefixes = (
        "KXBTC", "KXETH", "KXSOL", "KXCRYPTO",
        "BTC", "ETH", "SOL", "CRYPTO",
    )

    for prefix in economics_prefixes:
        if t.startswith(prefix):
            return "economics"
    for prefix in politics_prefixes:
        if t.startswith(prefix):
            return "politics"
    for prefix in sports_prefixes:
        if t.startswith(prefix):
            return "sports"
    for prefix in entertainment_prefixes:
        if t.startswith(prefix):
            return "entertainment"
    for prefix in crypto_prefixes:
        if t.startswith(prefix):
            return "crypto"
    return "other"


def _poly_question_to_category(question: str) -> str:
    """Categorize a Polymarket question using the existing normalize_category logic."""
    if not isinstance(question, str):
        return "other"
    return normalize_category(question)


def _parse_poly_yes_price(outcome_prices: str) -> float | None:
    """Parse the YES price from Polymarket's outcome_prices JSON string.

    Format is typically: '[\"0.95\", \"0.05\"]' where first element is YES price.
    """
    import json

    if not isinstance(outcome_prices, str):
        return None
    try:
        prices = json.loads(outcome_prices)
        if isinstance(prices, list) and len(prices) >= 1:
            return float(prices[0])
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Transform Jon Becker's prediction market dataset into arbscanner calibration format"
    )
    parser.add_argument(
        "data_dir",
        type=Path,
        help="Path to extracted Becker data directory (contains kalshi/ and polymarket/ subdirs)",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Output Parquet path (default: data/calibration/historical_becker.parquet)",
    )
    parser.add_argument(
        "--compute-curves",
        action="store_true",
        help="Also compute calibration curves after transforming",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    output = args.output or (project_root / "data" / "calibration" / "historical_becker.parquet")
    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Transforming Becker dataset from: {args.data_dir}")
    print()

    # Transform both exchanges
    print("[Kalshi]")
    kalshi_df = transform_kalshi(args.data_dir)
    print()

    print("[Polymarket]")
    poly_df = transform_polymarket(args.data_dir)
    print()

    # Combine
    frames = [df for df in [kalshi_df, poly_df] if not df.empty]
    if not frames:
        print("ERROR: No data found. Check that data_dir contains kalshi/markets/ and/or polymarket/markets/")
        sys.exit(1)

    combined = pd.concat(frames, ignore_index=True)

    # Apply normalize_category for consistency
    combined["category"] = combined["category"].apply(normalize_category)

    print(f"Combined dataset: {len(combined)} resolved markets")
    print(f"  Kalshi:      {len(kalshi_df) if not kalshi_df.empty else 0}")
    print(f"  Polymarket:  {len(poly_df) if not poly_df.empty else 0}")
    print(f"  Categories:  {dict(combined['category'].value_counts())}")
    print()

    # Keep only the columns the calibration module expects
    calibration_cols = ["category", "resolution_date", "created_date", "final_price", "resolved_yes"]
    combined[calibration_cols].to_parquet(output)
    print(f"Saved to: {output}")
    print(f"  Rows: {len(combined)}")
    size_mb = output.stat().st_size / 1024 / 1024
    print(f"  Size: {size_mb:.1f} MB")

    # Also save per-exchange files with backtest columns (market_id, title, exchange)
    backtest_cols = ["market_id", "category", "created_date", "resolution_date", "final_price", "resolved_yes", "title", "exchange"]
    for exchange_name, exchange_df in [("kalshi", kalshi_df), ("polymarket", poly_df)]:
        if exchange_df.empty:
            continue
        exchange_df["category"] = exchange_df["category"].apply(normalize_category)
        per_exchange_path = output.parent / f"historical_{exchange_name}.parquet"
        exchange_df[backtest_cols].to_parquet(per_exchange_path)
        print(f"Saved {exchange_name}: {per_exchange_path} ({len(exchange_df)} rows, {per_exchange_path.stat().st_size / 1024 / 1024:.1f} MB)")

    if args.compute_curves:
        print()
        print("Computing calibration curves...")
        from arbscanner.calibration import compute_calibration_curves

        curves = compute_calibration_curves(output)
        print(f"Computed {len(curves)} calibration entries:")
        print(curves.to_string())


if __name__ == "__main__":
    main()
