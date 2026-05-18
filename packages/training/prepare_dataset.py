"""
Dataset Preparation
Downloads historical data for Indian stocks, computes features, and
creates labeled datasets for training XGBoost and LSTM models.
"""

import argparse
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yaml
import yfinance as yf
from loguru import logger

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.features import FeatureEngine


def download_data(
    symbols: list[str],
    start: str,
    end: str,
    interval: str = "1d",
    period: str | None = None,
) -> dict[str, pd.DataFrame]:
    """Download historical data for multiple NSE symbols via Yahoo Finance.

    For sub-daily intervals (1m, 5m, 15m, 30m, 60m) yfinance only serves
    the last 60 days regardless of `start`/`end`. Pass `period='60d'`
    explicitly to side-step the start/end window confusion.
    """
    data = {}
    for symbol in symbols:
        ticker = f"{symbol}.NS"
        if period:
            logger.info(f"Downloading {ticker} (period={period}, {interval})...")
        else:
            logger.info(f"Downloading {ticker} ({start} to {end}, {interval})...")
        try:
            t = yf.Ticker(ticker)
            if period:
                df = t.history(period=period, interval=interval)
            else:
                df = t.history(start=start, end=end, interval=interval)
            if not df.empty:
                df.columns = [c.lower().replace(" ", "_") for c in df.columns]
                df.index.name = "timestamp"
                keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
                data[symbol] = df[keep]
                logger.info(f"  {symbol}: {len(df)} bars")
        except Exception as e:
            logger.error(f"  {symbol}: download failed - {e}")
    return data


def fetch_market_context(start: str, end: str, period: str | None = None) -> pd.DataFrame:
    """Build a daily DataFrame of (date -> nifty_trend, india_vix).

    2026-05-14: prerequisite for the regime-aware ML feature set. Without
    this, training samples never see ``nifty_trend``/``india_vix`` and
    the model learns a regime-blind policy that mismatches what the live
    agent feeds it.

    Returns a DataFrame indexed by trading-date (UTC tz-naive) with two
    columns:
      * ``nifty_trend`` : 1 if Nifty close >= 200-EMA, else -1
      * ``india_vix``   : daily VIX close

    Falls back to neutral defaults (nifty_trend=1, india_vix=15.0) for
    dates the upstream feed couldn't supply, so training never crashes
    on a partial download.
    """
    logger.info("Fetching daily Nifty + India VIX history for market context...")
    nifty = yf.Ticker("^NSEI")
    vix = yf.Ticker("^INDIAVIX")
    if period:
        # For intraday training datasets (60 days) we still want a multi-year
        # daily Nifty so the 200-EMA is mature -- override period for context.
        nifty_df = nifty.history(period="2y", interval="1d")
        vix_df = vix.history(period=period, interval="1d")
    else:
        nifty_df = nifty.history(start=start, end=end, interval="1d")
        vix_df = vix.history(start=start, end=end, interval="1d")

    ctx = pd.DataFrame(index=pd.DatetimeIndex([]))
    if not nifty_df.empty:
        nifty_close = nifty_df["Close"].copy()
        nifty_ema200 = nifty_close.ewm(span=200, adjust=False).mean()
        nifty_trend = (nifty_close >= nifty_ema200).astype(int).replace({0: -1, 1: 1})
        nifty_trend.index = nifty_trend.index.tz_localize(None).normalize()
        ctx = ctx.reindex(nifty_trend.index, fill_value=None)
        ctx["nifty_trend"] = nifty_trend
    if not vix_df.empty:
        vix_close = vix_df["Close"].copy()
        vix_close.index = vix_close.index.tz_localize(None).normalize()
        ctx = ctx.reindex(ctx.index.union(vix_close.index))
        ctx["india_vix"] = vix_close
    # P1 #8 (2026-05-17) -- train/serve consistency: live FeatureEngine fills
    # missing nifty_trend with 0 (neutral / sideways). Training MUST use the
    # same sentinel or the model learns a non-existent "bull" signal that the
    # serve path never produces. Same rationale as P1 #11 in trading_agent:
    # the bull sentinel silently licensed bear-regime trades.
    #
    # NOTE FOR PRODUCTION: models/xgboost_model.pkl trained 2026-05-14 used
    # the OLD bull default. After this fix, retrain before the next live
    # deploy to remove the residual train/serve skew. Until then the loaded
    # model still has the old bias baked in.
    if "nifty_trend" not in ctx.columns:
        ctx["nifty_trend"] = 0
    if "india_vix" not in ctx.columns:
        ctx["india_vix"] = 15.0
    ctx["nifty_trend"] = ctx["nifty_trend"].ffill().fillna(0).astype(int)
    ctx["india_vix"] = ctx["india_vix"].ffill().fillna(15.0).astype(float)
    logger.info(f"Market context covers {len(ctx)} trading days "
                f"({ctx.index.min()} to {ctx.index.max()})")
    return ctx


def create_labels(df: pd.DataFrame, horizon: int = 3, threshold_pct: float = 0.3) -> pd.Series:
    """
    Create classification labels based on future price movement.

    Args:
        horizon: Number of bars to look ahead.
        threshold_pct: Minimum % change to classify as UP/DOWN (else FLAT).

    Returns:
        Series with values: 1 (UP), 0 (DOWN), -1 (FLAT/removed).
    """
    future_return = (df["close"].shift(-horizon) - df["close"]) / df["close"] * 100
    labels = pd.Series(-1, index=df.index)
    labels[future_return > threshold_pct] = 1   # UP
    labels[future_return < -threshold_pct] = 0  # DOWN
    return labels


def prepare_dataset(
    symbols: list[str],
    start: str = "2023-01-01",
    end: str = "2026-03-29",
    interval: str = "1d",
    horizon: int = 3,
    output_dir: str = "data",
    period: str | None = None,
    label_threshold_pct: float = 0.3,
    drop_skew_features: bool = False,
):
    """
    Full pipeline: download → features → labels → save.

    Parameters
    ----------
    drop_skew_features
        2026-05-18 (P1): when an off-canonical interval (e.g. daily bars
        for long-horizon research) is used together with
        ``--allow-distribution-skew``, the operator has explicitly
        acknowledged the train/serve mismatch. Drop the offending
        ``tod_*`` / ``dow_*`` columns from the produced dataset BEFORE
        writing so even a deployed-by-mistake model can't carry the
        distribution skew through to serve time. Other features remain.
    """
    os.makedirs(output_dir, exist_ok=True)
    feature_engine = FeatureEngine()

    raw_data = download_data(symbols, start, end, interval, period=period)
    # 2026-05-14: build a (date -> nifty_trend, india_vix) lookup once so each
    # per-symbol intraday timestamp can be tagged with the day's market regime.
    # Best-effort: if Yahoo refuses, we fall back to neutral defaults so
    # training still produces a model -- it just won't use those features.
    try:
        market_ctx = fetch_market_context(start, end, period=period)
    except Exception as e:
        logger.warning(f"Market context fetch failed ({e}); using neutral defaults")
        market_ctx = pd.DataFrame(columns=["nifty_trend", "india_vix"])

    all_features = []
    all_labels = []

    for symbol, df in raw_data.items():
        if len(df) < 60:
            logger.warning(f"Skipping {symbol}: only {len(df)} bars (need >= 60)")
            continue

        logger.info(f"Computing features for {symbol}...")
        featured = feature_engine.compute_all(df)
        # Inject market context columns (nifty_trend / india_vix) by date.
        # Falls back to neutral if a date is missing in the lookup.
        if not market_ctx.empty:
            day_index = featured.index.tz_localize(None).normalize() if (
                hasattr(featured.index, "tz_localize")
            ) else pd.DatetimeIndex(featured.index).normalize()
            # P1 #8 (2026-05-17): align default with serve path (0 = neutral).
            featured["nifty_trend"] = market_ctx["nifty_trend"].reindex(
                day_index, method="ffill"
            ).fillna(0).astype(int).values
            featured["india_vix"] = market_ctx["india_vix"].reindex(
                day_index, method="ffill"
            ).fillna(15.0).astype(float).values
        else:
            featured["nifty_trend"] = 0
            featured["india_vix"] = 15.0

        labels = create_labels(featured, horizon=horizon, threshold_pct=label_threshold_pct)

        ml_cols = feature_engine.get_ml_feature_columns()
        available = [c for c in ml_cols if c in featured.columns]
        if "nifty_trend" not in available or "india_vix" not in available:
            logger.warning(
                "Market context columns not in feature list -- model will be "
                "regime-blind unless the engine's get_ml_feature_columns() is "
                "updated."
            )

        feature_df = featured[available].copy()
        feature_df["label"] = labels
        feature_df["symbol"] = symbol
        feature_df = feature_df.dropna()
        feature_df = feature_df[feature_df["label"] >= 0]  # remove FLAT

        all_features.append(feature_df)
        logger.info(f"  {symbol}: {len(feature_df)} labeled samples (UP={sum(labels==1)}, DOWN={sum(labels==0)})")

    if not all_features:
        logger.error("No data produced. Exiting.")
        return

    combined = pd.concat(all_features)
    logger.info(f"\nTotal dataset: {len(combined)} samples")
    logger.info(f"  UP:   {sum(combined['label']==1)} ({sum(combined['label']==1)/len(combined)*100:.1f}%)")
    logger.info(f"  DOWN: {sum(combined['label']==0)} ({sum(combined['label']==0)/len(combined)*100:.1f}%)")

    # 2026-05-18 (P1): if the caller has explicitly opted-in to a
    # distribution-skewed interval, drop the four cyclic-time features so
    # they can't carry the skew into a deployed model. We do this AFTER
    # the row-level dropna so the row count doesn't shift; the columns
    # are simply removed from the wide dataset.
    if drop_skew_features:
        skew_cols = [
            c for c in combined.columns
            if c.startswith("tod_") or c.startswith("dow_")
        ]
        if skew_cols:
            combined = combined.drop(columns=skew_cols)
            logger.critical(
                f"[TRAIN/SERVE-SKEW] dropped {skew_cols} from dataset "
                f"because --allow-distribution-skew was set. Models "
                f"trained on this dataset cannot use these features, "
                f"which neutralises the interval mismatch."
            )

    # P1 #7 (2026-05-17) -- ML INTEGRITY: split by CALENDAR TIME, not by row
    # index. The OLD code did ``combined.iloc[:0.8*N]`` after ``pd.concat``,
    # which puts symbol1's last bars at row N1, symbol2's last bars at row
    # N1+N2, etc. So a 2025-12-31 candle for SYMBOL_A could be in train while
    # SYMBOL_B's 2025-12-31 candle ends up in test -- the model learns "what
    # other symbols did on the same date" and the test-set ROC-AUC is
    # inflated by cross-symbol leakage.
    #
    # Time-based split: sort by index (which is the bar timestamp for each
    # row), take the 80th-percentile timestamp as the cutoff, train on
    # rows strictly before, test on rows on/after. Guarantees no calendar
    # overlap between the two splits.
    if hasattr(combined.index, "tz_localize") and len(combined) > 1:
        try:
            sorted_idx = combined.sort_index()
            timestamps = sorted_idx.index
            # Use the 80th-percentile timestamp as cutoff.
            cutoff_idx = int(len(timestamps) * 0.8)
            cutoff = timestamps[cutoff_idx]
            train = sorted_idx[sorted_idx.index < cutoff]
            test = sorted_idx[sorted_idx.index >= cutoff]
            logger.info(f"Time-based split cutoff: {cutoff} "
                        f"(train={len(train)}, test={len(test)})")
        except Exception as exc:  # noqa: BLE001 - fall back to row split if index unusable
            logger.warning(f"Time-based split failed ({exc!r}); using row-index split.")
            split_idx = int(len(combined) * 0.8)
            train = combined.iloc[:split_idx]
            test = combined.iloc[split_idx:]
    else:
        # Degenerate case (no timestamp index, or single row): row split.
        split_idx = int(len(combined) * 0.8)
        train = combined.iloc[:split_idx]
        test = combined.iloc[split_idx:]

    train_path = os.path.join(output_dir, "train_dataset.csv")
    test_path = os.path.join(output_dir, "test_dataset.csv")
    train.to_csv(train_path)
    test.to_csv(test_path)

    logger.info(f"\nSaved: {train_path} ({len(train)} rows)")
    logger.info(f"Saved: {test_path} ({len(test)} rows)")


def main():
    parser = argparse.ArgumentParser(description="Prepare ML training dataset")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2026-03-29")
    # P1 #9 (2026-05-17) -- TRAIN/SERVE CONSISTENCY: live FeatureEngine
    # computes ``tod_sin/cos`` and ``dow_sin/cos`` from 5-minute bars
    # (tick_aggregator emits 1min/5min/15min). Training on 1d bars made
    # those four features land in a totally different distribution
    # (24 intraday slots vs 1 per day) -- the model was effectively
    # ignoring them at serve time. Default is now 5m to match serve.
    # ``--interval 1d`` is still allowed for long-horizon research, but
    # we emit a WARNING below so the operator knows the model produced
    # from this run will NOT match the live FeatureEngine.
    parser.add_argument("--interval", default="5m")
    # `period` is the right way to fetch intraday data — yfinance silently
    # truncates to last 60 days when interval < 1d, regardless of start/end.
    parser.add_argument("--period", default=None,
                        help="Use yfinance period (e.g. '60d') instead of start/end. "
                             "Required for intraday intervals (5m, 15m, etc.)")
    # P1 #9 -- canonical live-serve interval. Update this string if the
    # tick_aggregator base candle ever changes (currently 5min in live).
    SERVE_INTERVAL = "5m"
    parser.add_argument("--horizon", type=int, default=3,
                        help="Bars ahead to predict (3 bars = 15min on 5m candles)")
    parser.add_argument("--threshold-pct", type=float, default=0.3,
                        help="Label threshold: only |return| > threshold counts as UP/DOWN")
    parser.add_argument("--output", default="data")
    # Symbol source — added 2026-05-06 so the script doesn't depend on
    # config.market.instruments (which is empty when the live agent uses
    # the dynamic scanner). Provide one of:
    #   --symbols A,B,C  (explicit comma list)
    #   --symbols-file path/to/syms.txt  (one symbol per line)
    #   --use-scanner-universe  (use the hardcoded NSE_UNIVERSE list)
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated NSE symbols (override config)")
    parser.add_argument("--symbols-file", default=None,
                        help="Path to file with one NSE symbol per line")
    parser.add_argument("--use-scanner-universe", action="store_true",
                        help="Use core.stock_scanner.NSE_UNIVERSE")
    parser.add_argument("--limit", type=int, default=None,
                        help="Take first N symbols only (post-resolve)")
    # 2026-05-18 (P1): hard-block off-canonical intervals unless the
    # operator explicitly opts in. Without this flag, daily-bar runs used
    # to produce a deployable artifact whose tod_/dow_ features had a
    # train/serve distribution skew that silently degraded live edge --
    # the WARNING below was not enforceable. With the flag, the same
    # daily-bar run is allowed for research, but tod_/dow_ columns are
    # dropped from the dataset so the model can't carry the skew.
    parser.add_argument(
        "--allow-distribution-skew",
        action="store_true",
        help=(
            "Opt-in to a non-canonical --interval (e.g. 1d). Required to "
            "produce a dataset when --interval != serve interval. Causes "
            "tod_*/dow_* features to be dropped from the dataset."
        ),
    )
    args = parser.parse_args()

    symbols: list[str] = []
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.symbols_file:
        with open(args.symbols_file) as f:
            symbols = [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]
    elif args.use_scanner_universe:
        from core.stock_scanner import NSE_UNIVERSE
        symbols = list(NSE_UNIVERSE)
    else:
        with open(args.config) as f:
            config = yaml.safe_load(f)
        symbols = [i["symbol"] for i in config.get("market", {}).get("instruments", [])]
        if not symbols:
            logger.error(
                "No symbols resolved. config.market.instruments is empty. "
                "Pass --symbols, --symbols-file, or --use-scanner-universe."
            )
            return

    if args.limit:
        symbols = symbols[: args.limit]

    logger.info(f"Resolved {len(symbols)} symbols: {symbols[:5]}{'...' if len(symbols) > 5 else ''}")

    # 2026-05-18 (P1): a non-canonical --interval is now a HARD BLOCK
    # unless the operator passes --allow-distribution-skew. The OLD path
    # emitted a WARNING and continued -- the resulting model was
    # deployable, and operator memory being what it is, several were
    # eventually deployed. The block forces an explicit acknowledgement.
    # When the override is set, we also drop tod_*/dow_* from the dataset
    # so the model cannot carry the skew through to serve time.
    if args.interval != SERVE_INTERVAL:
        if not args.allow_distribution_skew:
            logger.critical(
                f"[TRAIN/SERVE-SKEW] --interval={args.interval} does NOT match "
                f"the live FeatureEngine interval ({SERVE_INTERVAL}). "
                f"Refusing to produce a dataset that would silently skew "
                f"tod_sin/cos and dow_sin/cos at serve time. Pass "
                f"--allow-distribution-skew to acknowledge and proceed "
                f"(the offending columns will be dropped from the output)."
            )
            sys.exit(2)
        logger.critical(
            f"[TRAIN/SERVE-SKEW] --interval={args.interval} does NOT match "
            f"the live FeatureEngine interval ({SERVE_INTERVAL}). The "
            f"operator has acknowledged the skew via "
            f"--allow-distribution-skew. tod_*/dow_* features will be "
            f"DROPPED from the produced dataset."
        )

    prepare_dataset(
        symbols, args.start, args.end, args.interval, args.horizon, args.output,
        period=args.period, label_threshold_pct=args.threshold_pct,
        drop_skew_features=(args.interval != SERVE_INTERVAL
                            and args.allow_distribution_skew),
    )


if __name__ == "__main__":
    main()
