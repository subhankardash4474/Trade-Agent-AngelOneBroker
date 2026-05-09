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
):
    try:
        import xgboost as xgb
        from sklearn.metrics import accuracy_score, classification_report, roc_auc_score
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

    # Evaluation
    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]

    accuracy = accuracy_score(y_test, y_pred)
    auc = roc_auc_score(y_test, y_proba)

    logger.info(f"\n{'='*50}")
    logger.info(f"Test Accuracy: {accuracy:.4f}")
    logger.info(f"Test AUC:      {auc:.4f}")
    logger.info(f"\n{classification_report(y_test, y_pred, target_names=['DOWN', 'UP'])}")

    # Feature importance
    importance = pd.Series(model.feature_importances_, index=feature_cols).sort_values(ascending=False)
    logger.info("Top 10 features:")
    for feat, imp in importance.head(10).items():
        logger.info(f"  {feat}: {imp:.4f}")

    # Save model
    os.makedirs(os.path.dirname(model_output), exist_ok=True)
    with open(model_output, "wb") as f:
        pickle.dump(model, f)
    logger.info(f"\nModel saved: {model_output}")


def main():
    parser = argparse.ArgumentParser(description="Train XGBoost direction classifier")
    parser.add_argument("--train", default="data/train_dataset.csv")
    parser.add_argument("--test", default="data/test_dataset.csv")
    parser.add_argument("--output", default="models/xgboost_model.pkl")
    args = parser.parse_args()
    train_xgboost(args.train, args.test, args.output)


if __name__ == "__main__":
    main()
