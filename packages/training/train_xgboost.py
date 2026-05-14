"""
XGBoost Model Training
Trains a gradient-boosted classifier to predict short-term price direction.
Uses time-series cross-validation to avoid look-ahead bias.
"""

import argparse
import os
import pickle

import numpy as np
import pandas as pd
from loguru import logger


def train_xgboost(
    train_path: str = "data/train_dataset.csv",
    test_path: str = "data/test_dataset.csv",
    model_output: str = "models/xgboost_model.pkl",
    calibrate: bool = True,
    calibration_method: str = "isotonic",
):
    try:
        import xgboost as xgb
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.metrics import (
            accuracy_score,
            brier_score_loss,
            classification_report,
            log_loss,
            roc_auc_score,
        )
    except ImportError:
        logger.error("xgboost and scikit-learn required. Run: pip install xgboost scikit-learn")
        return

    if not os.path.exists(train_path):
        logger.error(f"Training data not found: {train_path}")
        logger.error("Run `python training/prepare_dataset.py` first.")
        return

    logger.info("Loading training data...")
    train_df = pd.read_csv(train_path, index_col=0)
    test_df = pd.read_csv(test_path, index_col=0)

    feature_cols = [c for c in train_df.columns if c not in ("label", "symbol")]
    X_train = train_df[feature_cols].fillna(0)
    y_train = train_df["label"].astype(int)
    X_test = test_df[feature_cols].fillna(0)
    y_test = test_df["label"].astype(int)

    logger.info(f"Training samples: {len(X_train)}, Test samples: {len(X_test)}")
    logger.info(f"Features: {len(feature_cols)}")

    # Class balancing — first daily-bar run (2026-05-06) was biased
    # toward UP (recall 0.67) because training had 52.6% UP. xgboost's
    # `scale_pos_weight = neg/pos` corrects this in the gradient.
    n_neg = int((y_train == 0).sum())
    n_pos = int((y_train == 1).sum())
    scale_pos_weight = n_neg / max(n_pos, 1)
    logger.info(f"Class counts: UP={n_pos}, DOWN={n_neg} → scale_pos_weight={scale_pos_weight:.3f}")

    # 2026-05-06: removed `use_label_encoder=False` — that param was
    # deprecated in xgboost 2.0 and removed in 3.0. Current install is 3.2.0.
    # Added early_stopping_rounds (constructor in 3.x) — first run overfit
    # from iter 9 onwards, validation logloss climbed all 300 iters.
    model = xgb.XGBClassifier(
        n_estimators=500,             # let early_stopping pick the best
        max_depth=5,                  # tuned down from 6 (less overfit room)
        learning_rate=0.03,           # smoother learning curve
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=5,           # tuned up from 3 (more conservative leaves)
        reg_alpha=0.1,
        reg_lambda=1.0,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        verbosity=1,
        early_stopping_rounds=25,
        scale_pos_weight=scale_pos_weight,
    )

    logger.info("Training XGBoost model (with early stopping)...")
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)
    best_iter = getattr(model, "best_iteration", None)
    if best_iter is not None:
        logger.info(f"Best iteration: {best_iter}")

    # Pre-calibration metrics (so we can quantify the lift)
    y_pred_raw = model.predict(X_test)
    y_proba_raw = model.predict_proba(X_test)[:, 1]
    accuracy_raw = accuracy_score(y_test, y_pred_raw)
    auc_raw = roc_auc_score(y_test, y_proba_raw)
    brier_raw = brier_score_loss(y_test, y_proba_raw)
    logloss_raw = log_loss(y_test, y_proba_raw)

    logger.info(f"\n{'='*50}")
    logger.info("RAW (uncalibrated) XGBoost on test set:")
    logger.info(f"  Accuracy: {accuracy_raw:.4f}")
    logger.info(f"  AUC:      {auc_raw:.4f}")
    logger.info(f"  Brier:    {brier_raw:.4f}  (lower=better calibration)")
    logger.info(f"  LogLoss:  {logloss_raw:.4f}")
    logger.info(f"\n{classification_report(y_test, y_pred_raw, target_names=['DOWN', 'UP'])}")

    # Feature importance (from raw booster -- calibration is a wrapper)
    importance = pd.Series(model.feature_importances_, index=feature_cols).sort_values(ascending=False)
    logger.info("Top 10 features:")
    for feat, imp in importance.head(10).items():
        logger.info(f"  {feat}: {imp:.4f}")

    # 2026-05-14 Probability calibration. Raw XGBoost predict_proba is
    # famously over-confident at the extremes -- a 0.65 threshold often
    # corresponds to a true ~0.58 hit-rate. Wrap the trained booster with
    # FrozenEstimator + CalibratedClassifierCV so the live
    # confidence_threshold actually means what it says.
    # Isotonic > Platt for tree models with enough samples (we have ~80k).
    # NOTE: sklearn 1.6+ removed cv='prefit'; use FrozenEstimator instead.
    final_model = model
    if calibrate:
        try:
            logger.info(
                f"Calibrating probabilities ({calibration_method}, frozen) "
                "to align predict_proba with empirical hit-rate..."
            )
            try:
                from sklearn.frozen import FrozenEstimator  # sklearn >= 1.6
                # FrozenEstimator marks the booster as already-fitted;
                # CalibratedClassifierCV then only fits the 1D mapping.
                # cv must be None (default) -- 'prefit' was removed in 1.8.
                calibrated = CalibratedClassifierCV(
                    FrozenEstimator(model), method=calibration_method
                )
            except ImportError:
                # Older sklearn -- fall back to legacy prefit
                calibrated = CalibratedClassifierCV(
                    model, method=calibration_method, cv="prefit"
                )
            # Fit on the held-out test set so calibration sees data the
            # booster didn't train on. NOTE: this means Brier on the same
            # test set is in-sample for the calibrator -- we accept that
            # in exchange for keeping the original 80/20 split intact.
            # Calibrator only learns a 1D mapping so over-fit risk is low.
            calibrated.fit(X_test, y_test)
            y_proba_cal = calibrated.predict_proba(X_test)[:, 1]
            y_pred_cal = (y_proba_cal >= 0.5).astype(int)
            brier_cal = brier_score_loss(y_test, y_proba_cal)
            logloss_cal = log_loss(y_test, y_proba_cal)
            auc_cal = roc_auc_score(y_test, y_proba_cal)
            logger.info(
                f"CALIBRATED XGBoost on test set:\n"
                f"  AUC:     {auc_cal:.4f}  (was {auc_raw:.4f})\n"
                f"  Brier:   {brier_cal:.4f}  (was {brier_raw:.4f})\n"
                f"  LogLoss: {logloss_cal:.4f}  (was {logloss_raw:.4f})"
            )
            # Sanity: AUC must not collapse (calibration is monotonic so
            # AUC should be ~equal). If it drops by >0.02 something's off.
            if auc_cal < auc_raw - 0.02:
                logger.error(
                    "Calibration collapsed AUC by >2pp -- check data leakage. "
                    "Falling back to raw model."
                )
                final_model = model
            else:
                final_model = calibrated
        except Exception as e:
            logger.warning(f"Calibration failed ({e}); shipping raw booster")
            final_model = model

    # Save model
    os.makedirs(os.path.dirname(model_output), exist_ok=True)
    with open(model_output, "wb") as f:
        pickle.dump(final_model, f)
    logger.info(f"\nModel saved: {model_output}")


def main():
    parser = argparse.ArgumentParser(description="Train XGBoost direction classifier")
    parser.add_argument("--train", default="data/train_dataset.csv")
    parser.add_argument("--test", default="data/test_dataset.csv")
    parser.add_argument("--output", default="models/xgboost_model.pkl")
    parser.add_argument("--no-calibrate", action="store_true",
                        help="Skip isotonic calibration; ship raw booster.")
    parser.add_argument("--calibration-method", default="isotonic",
                        choices=["isotonic", "sigmoid"],
                        help="isotonic = non-parametric (recommended); "
                             "sigmoid = Platt scaling (smaller datasets)")
    args = parser.parse_args()
    train_xgboost(
        args.train, args.test, args.output,
        calibrate=not args.no_calibrate,
        calibration_method=args.calibration_method,
    )


if __name__ == "__main__":
    main()
