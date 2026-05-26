"""
XGBoost Model Training
Trains a gradient-boosted classifier to predict short-term price direction.

F-100 (audit 2026-05-27): the previous module docstring claimed
"time-series cross-validation" but no CV is implemented; training is a
single chronological train/test split produced upstream by
`prepare_dataset.py`, with early stopping done on a chronological tail
slice of the training set (carved out below, F-22 fix). The TEST set
remains a strict held-out for final metrics only.
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

    # F-22 (audit 2026-05-27): early stopping previously evaluated on
    # X_test and we then reported held-out metrics on the same X_test --
    # the test set was effectively used to pick `best_iteration`, so
    # those metrics were optimistic (selection-bias leakage). Carve a
    # CHRONOLOGICAL tail slice of X_train as the early-stopping
    # validation set. The order in X_train is preserved by
    # prepare_dataset; we take the last `val_frac` rows as the
    # validation slice. The official X_test stays untouched for the
    # final held-out report below.
    val_frac = 0.15
    n_train = len(X_train)
    n_val = max(1, int(n_train * val_frac))
    if n_val >= n_train:
        # Pathologically small training set -- fall back to using the
        # test set but warn loudly so it's not silent.
        logger.warning(
            f"[XGB-TRAIN] training set too small for a {val_frac:.0%} "
            f"validation tail ({n_train} rows); early stopping will "
            f"peek at the official test set. Held-out metrics below "
            f"will be optimistic."
        )
        X_fit, y_fit = X_train, y_train
        eval_set = [(X_test, y_test)]
    else:
        X_fit = X_train.iloc[: n_train - n_val]
        y_fit = y_train.iloc[: n_train - n_val]
        X_val = X_train.iloc[n_train - n_val :]
        y_val = y_train.iloc[n_train - n_val :]
        eval_set = [(X_val, y_val)]
        logger.info(
            f"Early-stopping validation: chronological tail "
            f"{n_val} rows of train ({val_frac:.0%}); "
            f"fit on {len(X_fit)} rows; test set untouched."
        )

    logger.info("Training XGBoost model (with early stopping)...")
    model.fit(X_fit, y_fit, eval_set=eval_set, verbose=False)
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
            # C-23 (audit 2026-05-26): split the held-out test set so the
            # calibrator FIT and the calibrator EVAL never see the same
            # rows. Pre-fix, both `calibrated.fit(X_test, y_test)` and
            # the Brier evaluation `brier_score_loss(y_test, ...)` ran
            # on the same set, so the reported Brier was in-sample for
            # the calibrator. Operators trying to decide between
            # `method=isotonic` vs `method=sigmoid` were comparing
            # apples to apples but both apples were rotten — neither
            # number generalises. We now use a chronological 50/50
            # split (first half = calib fit, second half = calib eval)
            # to preserve the time-ordering guarantee while still giving
            # the calibrator ~40k rows to learn the 1D mapping from.
            half = max(1, len(X_test) // 2)
            X_calib_fit, y_calib_fit = X_test[:half], y_test[:half]
            X_calib_eval, y_calib_eval = X_test[half:], y_test[half:]
            calibrated.fit(X_calib_fit, y_calib_fit)
            y_proba_cal = calibrated.predict_proba(X_calib_eval)[:, 1]
            y_pred_cal = (y_proba_cal >= 0.5).astype(int)
            brier_cal = brier_score_loss(y_calib_eval, y_proba_cal)
            logloss_cal = log_loss(y_calib_eval, y_proba_cal)
            auc_cal = roc_auc_score(y_calib_eval, y_proba_cal)
            # Recompute the RAW baseline on the SAME eval slice so the
            # before/after comparison is apples-to-apples (not raw on full
            # X_test vs calibrated on second half).
            y_proba_raw_eval = model.predict_proba(X_calib_eval)[:, 1]
            auc_raw_eval = roc_auc_score(y_calib_eval, y_proba_raw_eval)
            brier_raw_eval = brier_score_loss(y_calib_eval, y_proba_raw_eval)
            logloss_raw_eval = log_loss(y_calib_eval, y_proba_raw_eval)
            logger.info(
                f"CALIBRATED XGBoost on held-out eval slice "
                f"({len(X_calib_eval)} rows):\n"
                f"  AUC:     {auc_cal:.4f}  (raw_eval {auc_raw_eval:.4f})\n"
                f"  Brier:   {brier_cal:.4f}  (raw_eval {brier_raw_eval:.4f})\n"
                f"  LogLoss: {logloss_cal:.4f}  (raw_eval {logloss_raw_eval:.4f})"
            )
            # Re-target the AUC sanity check at the eval-only baseline so
            # we're comparing on the same rows.
            auc_raw_for_check = auc_raw_eval
            # Sanity: AUC must not collapse (calibration is monotonic so
            # AUC should be ~equal). If it drops by >0.02 vs the SAME-slice
            # raw baseline, something is off.
            if auc_cal < auc_raw_for_check - 0.02:
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
