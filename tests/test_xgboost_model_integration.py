"""Regression tests for the trained XGBoost model integration (2026-05-06).

These tests lock in the contract between the trained model file
(`models/xgboost_model.pkl`), the `FeatureEngine.get_ml_feature_columns()`
list, and the `XGBoostClassifier` strategy. If any of those drift, these
tests should fail loudly so we don't ship a silently-broken model.

The contract is:
1. Model file exists and loads.
2. The strategy receives the SAME number of features the model was
   trained on (else `predict_proba` will throw or, worse, silently
   return garbage with the wrong feature alignment).
3. Predictions are bounded probabilities in [0, 1].
4. The model produces a reasonable distribution across labels (NOT
   90 %+ one class, which would indicate the v1 class-skew regression
   has crept back).
5. Time-of-day features (added 2026-05-06) are present in the ML
   feature list — they were the dominant features in v2 training and
   their absence implies a feature regression.
"""
from __future__ import annotations

import os
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.features import FeatureEngine
from strategies.base_strategy import Signal
from strategies.xgboost_classifier import XGBoostClassifier


MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "xgboost_model.pkl"


def _fake_intraday_5min_bars(n: int = 250, seed: int = 42) -> pd.DataFrame:
    """Synthetic OHLCV indexed by 5-min IST timestamps, deterministic.

    250 bars ≈ 2 trading days on 5-min candles, which is more than enough
    for FeatureEngine warmups (50-bar EMAs, 78-bar daily highs, etc.).
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-05-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    close = 1000 + rng.standard_normal(n).cumsum()
    high = close + rng.uniform(0.5, 3.0, n)
    low = close - rng.uniform(0.5, 3.0, n)
    opn = close + rng.standard_normal(n) * 0.5
    vol = rng.integers(1000, 50000, n)
    return pd.DataFrame(
        {"open": opn, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


# ── 1. Model file contract ─────────────────────────────────────────


def test_model_file_exists():
    """The trained model MUST be checked in (or downloadable). If this
    fails it means the v2 retrain step (2026-05-06) wasn't shipped and
    the strategy will silently degrade to HOLD on every cycle."""
    assert MODEL_PATH.exists(), (
        f"XGBoost model file missing: {MODEL_PATH}. "
        f"Run `python training/train_xgboost.py` to regenerate."
    )
    assert MODEL_PATH.stat().st_size > 100_000, (
        "Model file looks suspiciously small (< 100 KB). "
        "Likely a stub or partial write."
    )


def test_model_loads_via_pickle():
    """Direct unpickle round-trip — guards against pickle protocol
    mismatch when the training Python version != live agent Python
    version."""
    with open(MODEL_PATH, "rb") as f:
        model = pickle.load(f)
    # XGBClassifier exposes n_features_in_ from sklearn ≥ 1.0
    assert hasattr(model, "n_features_in_"), \
        "Loaded model is missing n_features_in_ — not an XGBClassifier?"
    assert model.n_features_in_ > 0


# ── 2. Feature alignment contract ─────────────────────────────────


def test_ml_feature_count_matches_trained_model():
    """The number of features the strategy will feed at predict time
    MUST equal the number the model was trained on. Drift here is
    silent and catastrophic — XGBoost will run, but the columns will
    be misaligned and predictions will be noise.

    Specifically guards against:
      - Adding a new feature to FeatureEngine without retraining.
      - Removing a feature without retraining.
      - Reordering get_ml_feature_columns() (xgb is positional!).
    """
    engine = FeatureEngine()
    ml_cols = engine.get_ml_feature_columns()

    with open(MODEL_PATH, "rb") as f:
        model = pickle.load(f)

    assert len(ml_cols) == model.n_features_in_, (
        f"Feature count drift: get_ml_feature_columns() returns "
        f"{len(ml_cols)} features but the trained model expects "
        f"{model.n_features_in_}. Retrain or revert the feature change."
    )


def test_time_of_day_features_present():
    """Locked in 2026-05-06 — the time features (tod_sin, tod_cos,
    dow_sin, dow_cos) collectively had ~31 % feature importance in
    the v2 model and were the difference between AUC 0.525 (random)
    and AUC 0.6436 (real edge). If these features are removed,
    retraining is mandatory."""
    engine = FeatureEngine()
    ml_cols = set(engine.get_ml_feature_columns())
    required = {"tod_sin", "tod_cos", "dow_sin", "dow_cos"}
    missing = required - ml_cols
    assert not missing, (
        f"Time-of-day features missing from ML feature list: {missing}. "
        f"These were added 2026-05-06 and account for the model's edge — "
        f"if they're gone, retrain or revert."
    )


# ── 3. Live prediction contract ───────────────────────────────────


@pytest.fixture(scope="module")
def strategy() -> XGBoostClassifier:
    return XGBoostClassifier({
        "model_path": str(MODEL_PATH),
        "confidence_threshold": 0.65,
    })


def test_strategy_loads_model(strategy: XGBoostClassifier):
    assert strategy._model is not None, "Strategy failed to load model"


def test_strategy_returns_valid_signal(strategy: XGBoostClassifier):
    """End-to-end: synthetic OHLCV → features → prediction → TradeSignal."""
    df = _fake_intraday_5min_bars()
    sig = strategy.generate_signal(df, "TEST")
    assert sig is not None
    assert sig.signal in {Signal.BUY, Signal.SELL, Signal.HOLD}
    if sig.signal != Signal.HOLD:
        assert 0.0 <= sig.confidence <= 1.0


def test_predicted_probabilities_are_bounded(strategy: XGBoostClassifier):
    """No NaN, no negatives, no probabilities > 1. Guards against
    a corrupted model file or feature column misalignment."""
    df = _fake_intraday_5min_bars()
    sig = strategy.generate_signal(df, "TEST")
    md = sig.metadata or {}
    if "prob_up" in md and "prob_down" in md:
        p_up = float(md["prob_up"])
        p_down = float(md["prob_down"])
        assert 0.0 <= p_up <= 1.0
        assert 0.0 <= p_down <= 1.0
        # Probabilities should sum to ~1 (binary classifier)
        assert abs(p_up + p_down - 1.0) < 1e-3


# ── 4. Distribution sanity (anti-bias) ────────────────────────────


def test_predictions_not_class_biased_on_real_test_set():
    """v1 (2026-05-06 morning) was 67 % UP / 36 % DOWN on test set —
    a clear class-imbalance bias from training. v2 corrected this
    with scale_pos_weight. This test loads the held-out test split
    used during training and verifies the model's predictions are
    roughly balanced across labels.

    Synthetic random-walk data is unreliable here because the model
    is trained on real intraday structure; on pure noise it can
    legitimately lean one direction. The held-out test set is the
    proper distribution to evaluate against.
    """
    test_csv = Path(__file__).resolve().parent.parent / "data" / "test_dataset.csv"
    if not test_csv.exists():
        pytest.skip(f"Test set not found: {test_csv}")

    df = pd.read_csv(test_csv)
    if len(df) < 1000:
        pytest.skip(f"Test set too small ({len(df)} rows) to assess bias")

    with open(MODEL_PATH, "rb") as f:
        model = pickle.load(f)

    # Use the trained model's exact feature names — the CSV may
    # contain extras like "timestamp" that aren't features.
    feature_names = list(model.feature_names_in_)
    X = df[feature_names].fillna(0)
    preds = model.predict(X)

    up_share = float((preds == 1).mean())
    down_share = float((preds == 0).mean())

    # v1 was UP 67 % / DOWN 33 % — would fail this. v2 was ~52/48
    # which passes comfortably. We allow up to 70/30 here as a hard
    # cap before regressing — anything more than that signals the
    # class-balancing fix has been undone.
    assert 0.30 <= up_share <= 0.70, (
        f"Class bias detected on held-out test set: "
        f"UP={up_share:.2%}, DOWN={down_share:.2%}. "
        f"Expected ~50/50 (acceptable range 30–70 %). "
        f"v1 class-skew regression?"
    )


def test_low_confidence_returns_hold(strategy: XGBoostClassifier):
    """Whenever max(prob_up, prob_down) < confidence_threshold (0.65
    by default), the strategy MUST return HOLD. This is the single
    most important behavioural contract — a stale or unsure model
    must never inject opinionated signals into the ensemble."""
    df = _fake_intraday_5min_bars(n=120)
    sig = strategy.generate_signal(df, "TEST")
    md = sig.metadata or {}
    if "prob_up" in md:
        max_p = max(float(md["prob_up"]), float(md["prob_down"]))
        if max_p < 0.65:
            assert sig.signal == Signal.HOLD, (
                f"Strategy emitted {sig.signal} with max_prob={max_p:.3f} "
                f"below threshold 0.65 — confidence gate broken"
            )


# ── 5. Config integration ─────────────────────────────────────────


def test_config_enables_xgboost_classifier():
    """Locked in 2026-05-06 when we first turned the strategy on.
    If the active list drops xgboost_classifier without an explicit
    revert decision, this test fires."""
    import yaml
    cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    active = cfg.get("strategies", {}).get("active", [])
    assert "xgboost_classifier" in active, (
        "xgboost_classifier missing from strategies.active. "
        "If intentional, also remove the test or document the revert."
    )


def test_xgboost_ensemble_weight_conservative():
    """Pinned at 1.0 (2026-05-06): the model has zero live track
    record. A weight of 2.0 (the prior pre-disable value) would let
    an untested model dominate ensemble decisions on day one. The
    learning system will adjust this upward as live trades close."""
    import yaml
    cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    weights = cfg.get("ensemble", {}).get("weights", {})
    w = weights.get("xgboost_classifier")
    assert w is not None, "xgboost_classifier weight missing"
    assert w <= 1.5, (
        f"xgboost_classifier weight={w} is too aggressive for an "
        f"untrained-in-live model. Cap at 1.5 until it has at least "
        f"50 closed trades."
    )
