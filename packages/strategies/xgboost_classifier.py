"""
XGBoost Classifier Strategy
ML-based strategy that predicts 15-minute price direction using
gradient-boosted trees trained on 2+ years of feature-engineered data.
"""

import os
import pickle
import time
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from core.features import FeatureEngine
from strategies._trend_context import is_against_trend
from strategies.base_strategy import BaseStrategy, Signal, TradeSignal


# Maximum age (in days) before we WARN about model staleness. After this
# threshold the strategy still runs, but the operator should retrain. The
# threshold is deliberately permissive — financial models age with market
# regime shifts, not the calendar, so 30 days is a soft "consider retraining"
# nudge. Hard-cut behaviour can be enabled via params if needed.
MODEL_STALE_DAYS_DEFAULT = 30


class XGBoostClassifier(BaseStrategy):
    """
    XGBoost-based direction prediction strategy.

    Trained on historical data with features from FeatureEngine.
    Predicts whether price will go UP or DOWN in the next N bars.
    Only triggers when prediction probability exceeds confidence_threshold.

    Parameters:
        model_path: Path to saved XGBoost model (default: models/xgboost_model.pkl).
        confidence_threshold: Minimum prediction probability (default 0.65).
        prediction_horizon: Bars ahead to predict (default 3 = 15min on 5m candles).
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        params = params or {}
        defaults = {
            "model_path": "models/xgboost_model.pkl",
            "confidence_threshold": 0.65,
            "prediction_horizon": 3,
            "timeframe": "5min",
            "stale_days": MODEL_STALE_DAYS_DEFAULT,
            # 2026-05-07: trend filter is wired but DISABLED by default.
            # 30-day post-mortem showed only 2 xgboost trades over the
            # window, both profitable (+Rs165). Sample too small to commit
            # to a filter. Re-enable to 5.0 once Phase-2 backtest validates
            # over a longer window. Setting to None keeps the code path
            # but bypasses the check.
            "trend_filter_pct": None,
            # 2026-05-07: SELL signals were missing SL/TP, falling back to
            # ensemble defaults of 1.5%/3%. We now set them symmetric to BUY:
            # SL = 1.5*ATR, TP = 2.0*ATR.
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 2.0,
            # 2026-05-13: Direction-stability gate. Require N consecutive
            # above-threshold same-side classifications on a symbol before
            # emitting a BUY/SELL. Filters out XGB flip-flops (live ev:
            # BAJFINANCE went XGB-BUY @ 09:34 -> XGB-SELL @ 09:52 -> taken
            # @ 10:04 -> stopped @ 10:31 for -Rs 155). HOLD signals (any
            # reason) don't reset the counter -- only an opposite-side
            # above-threshold signal does. Value 1 = legacy behaviour
            # (no filtering); 2 is the recommended floor.
            "signal_stability_bars": 2,
        }
        merged = {**defaults, **params}
        super().__init__(name="xgboost_classifier", params=merged)

        self.model_path: str = merged["model_path"]
        self.confidence_threshold: float = merged["confidence_threshold"]
        self.prediction_horizon: int = merged["prediction_horizon"]
        self.stale_days: int = int(merged["stale_days"])
        self.trend_filter_pct: Optional[float] = (
            float(merged["trend_filter_pct"])
            if merged.get("trend_filter_pct") is not None else None
        )
        self.sl_atr_mult: float = float(merged["sl_atr_mult"])
        self.tp_atr_mult: float = float(merged["tp_atr_mult"])
        self.signal_stability_bars: int = max(
            1, int(merged.get("signal_stability_bars", 2))
        )
        # Per-symbol direction-stability tracker for the flip-flop filter.
        # Maps symbol -> (last_above_threshold_side, consecutive_count).
        # Not persisted to disk -- after restart all symbols start fresh
        # and the first 1-2 signals will be buffered as expected.
        self._stability_state: Dict[str, tuple] = {}
        self._feature_engine = FeatureEngine()
        self._model = None
        # Health flags — set by _validate_model_contract(). When any of
        # these is non-empty the strategy is in "safe HOLD" mode and will
        # never emit BUY/SELL until the operator retrains/repairs.
        # `_unhealthy_reason` is the durable hard-disable reason.
        # `_stale_warned` is a one-shot soft warning flag.
        self._unhealthy_reason: Optional[str] = None
        self._stale_warned: bool = False
        self._load_model()
        self._validate_model_contract()

    def _record_stability(self, symbol: str, side: str) -> int:
        """Update per-symbol direction-stability tracker; return current
        consecutive same-side count.

        Only called for ABOVE-threshold ``BUY``/``SELL`` classifications.
        An opposite-side classification resets the counter to 1; same-side
        increments it. HOLD signals must NOT call this (they neither
        increment nor reset -- the counter persists until the model
        disagrees).
        """
        last_side, consec = self._stability_state.get(symbol, (None, 0))
        if last_side == side:
            consec += 1
        else:
            consec = 1
        self._stability_state[symbol] = (side, consec)
        return consec

    @property
    def required_history_bars(self) -> int:
        return 60

    def is_healthy(self) -> bool:
        """True if the model can be safely used to emit live signals.
        False puts the strategy into 'safe HOLD' mode without crashing
        the agent."""
        return self._model is not None and self._unhealthy_reason is None

    def _load_model(self):
        if os.path.exists(self.model_path):
            try:
                with open(self.model_path, "rb") as f:
                    self._model = pickle.load(f)
                logger.info(f"XGBoost model loaded from {self.model_path}")
            except Exception as e:
                # Common causes: corrupted pickle, library version mismatch
                # (e.g. trained on xgboost 2.x, loaded on 3.x). Mark
                # unhealthy — the strategy will silently HOLD instead
                # of crashing the agent.
                self._unhealthy_reason = f"load_failed: {type(e).__name__}: {e}"
                logger.warning(
                    f"[XGB-HEALTH] Failed to load XGBoost model: {e}. "
                    f"Strategy will return HOLD on every cycle."
                )
                self._model = None
        else:
            self._unhealthy_reason = "model_file_missing"
            logger.warning(
                f"[XGB-HEALTH] XGBoost model not found at {self.model_path}. "
                f"Strategy will return HOLD. "
                f"Run `python training/train_xgboost.py` to train."
            )

    def _validate_model_contract(self):
        """Verify the loaded model is compatible with the current
        FeatureEngine. The most common silent-failure mode is a feature
        count drift: training adds a column, live loads an old model
        that expects fewer columns, predictions become noise.

        Also checks model file age and emits a one-time staleness
        warning (not a hard disable — staleness is a nudge, not an
        error)."""
        if self._model is None:
            return

        # Feature count contract
        live_features = self._feature_engine.get_ml_feature_columns()
        expected = getattr(self._model, "n_features_in_", None)
        if expected is not None and expected != len(live_features):
            self._unhealthy_reason = (
                f"feature_count_drift: model={expected} "
                f"live_engine={len(live_features)}"
            )
            logger.error(
                f"[XGB-HEALTH] FEATURE COUNT DRIFT — model expects "
                f"{expected} features, FeatureEngine produces "
                f"{len(live_features)}. Strategy will return HOLD until "
                f"the model is retrained. This usually means a feature "
                f"was added/removed without retraining."
            )
            return

        # Model age — soft warning only
        try:
            mtime = os.path.getmtime(self.model_path)
            age_days = (time.time() - mtime) / 86400.0
            if age_days > self.stale_days and not self._stale_warned:
                logger.warning(
                    f"[XGB-HEALTH] Model is {age_days:.1f} days old "
                    f"(threshold={self.stale_days}d). Consider retraining: "
                    f"`python training/train_xgboost.py`. "
                    f"Strategy will continue running."
                )
                self._stale_warned = True
        except OSError:
            pass

        if self._unhealthy_reason is None:
            logger.info(
                f"[XGB-HEALTH] OK — {expected} features, "
                f"threshold={self.confidence_threshold}"
            )

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        # Hard health gate — model load failed, file missing, or feature
        # contract broken. Always HOLD. The reason is in metadata so the
        # cycle digest / audit log can surface it.
        if not self.is_healthy():
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": self._unhealthy_reason or "model_not_loaded"},
            )

        if not self.is_data_sufficient(data):
            return self._make_signal(Signal.HOLD, symbol, data, metadata={"reason": "insufficient_data"})

        df = self._feature_engine.compute_all(data)
        feature_cols = self._feature_engine.get_ml_feature_columns()

        # Get the latest row of features
        available_cols = [c for c in feature_cols if c in df.columns]
        if not available_cols:
            return self._make_signal(Signal.HOLD, symbol, df, metadata={"reason": "no_features"})

        latest = df[available_cols].iloc[[-1]].copy()
        latest = latest.fillna(0)

        try:
            proba = self._model.predict_proba(latest)[0]
            # Classes: 0=DOWN, 1=UP. Cast to plain Python float — predict_proba
            # returns numpy.float32 which is NOT json-serializable. Live evidence
            # (2026-05-06 10:32:43): XGBoost-only ensemble votes for UNITDSPR
            # and CGPOWER were rejected by Portfolio.open_position with
            # "Object of type float32 is not JSON serializable" because
            # `TradeSignal.confidence` propagated np.float32 into
            # `contributing_strategies`. Casting at the source fixes every
            # downstream consumer.
            prob_up = float(proba[1]) if len(proba) > 1 else float(proba[0])
            prob_down = float(proba[0]) if len(proba) > 1 else 1.0 - float(proba[0])
            predicted_class = 1 if prob_up > prob_down else 0
        except Exception as e:
            logger.error(f"XGBoost prediction error: {e}")
            return self._make_signal(Signal.HOLD, symbol, df, metadata={"reason": f"prediction_error: {e}"})

        price = float(df["close"].iloc[-1])
        atr = float(df["atr"].iloc[-1]) if "atr" in df.columns and not pd.isna(df["atr"].iloc[-1]) else price * 0.01

        metadata = {
            "prob_up": round(prob_up, 4),
            "prob_down": round(prob_down, 4),
            "predicted_class": "UP" if predicted_class == 1 else "DOWN",
        }

        if predicted_class == 1 and prob_up >= self.confidence_threshold:
            if self.trend_filter_pct is not None and is_against_trend(
                symbol, "BUY", threshold_pct=self.trend_filter_pct
            ):
                logger.info(
                    f"[{self.name}] BUY blocked for {symbol} | prob_up={prob_up:.3f} | "
                    f"trend filter (price < 50d SMA - {self.trend_filter_pct}%)"
                )
                return self._make_signal(
                    Signal.HOLD, symbol, df,
                    metadata={**metadata, "reason": "trend_filter_long"},
                )
            # Direction-stability gate. Resets on opposite-side flip. The
            # counter advances only on consecutive above-threshold BUYs;
            # below-threshold predictions (HOLD path below) leave it alone.
            consec = self._record_stability(symbol, "BUY")
            if consec < self.signal_stability_bars:
                logger.info(
                    f"[{self.name}] BUY {symbol} buffered "
                    f"({consec}/{self.signal_stability_bars}) | "
                    f"prob_up={prob_up:.3f}"
                )
                return self._make_signal(
                    Signal.HOLD, symbol, df,
                    metadata={
                        **metadata,
                        "reason": f"stability_pending_{consec}/{self.signal_stability_bars}",
                        "pending_side": "BUY",
                    },
                )
            stop_loss = price - self.sl_atr_mult * atr
            take_profit = price + self.tp_atr_mult * atr
            logger.info(
                f"[{self.name}] BUY {symbol} | prob_up={prob_up:.3f} | "
                f"stability={consec}/{self.signal_stability_bars}"
            )
            return self._make_signal(
                Signal.BUY, symbol, df,
                confidence=prob_up, stop_loss=stop_loss,
                take_profit=take_profit, metadata=metadata,
            )

        if predicted_class == 0 and prob_down >= self.confidence_threshold:
            if self.trend_filter_pct is not None and is_against_trend(
                symbol, "SELL", threshold_pct=self.trend_filter_pct
            ):
                logger.info(
                    f"[{self.name}] SELL blocked for {symbol} | prob_down={prob_down:.3f} | "
                    f"trend filter (price > 50d SMA + {self.trend_filter_pct}%)"
                )
                return self._make_signal(
                    Signal.HOLD, symbol, df,
                    metadata={**metadata, "reason": "trend_filter_short"},
                )
            consec = self._record_stability(symbol, "SELL")
            if consec < self.signal_stability_bars:
                logger.info(
                    f"[{self.name}] SELL {symbol} buffered "
                    f"({consec}/{self.signal_stability_bars}) | "
                    f"prob_down={prob_down:.3f}"
                )
                return self._make_signal(
                    Signal.HOLD, symbol, df,
                    metadata={
                        **metadata,
                        "reason": f"stability_pending_{consec}/{self.signal_stability_bars}",
                        "pending_side": "SELL",
                    },
                )
            # Fix from 2026-05-07: SELL was missing SL/TP, fell back to
            # generic ensemble defaults. Now symmetric with BUY.
            stop_loss = price + self.sl_atr_mult * atr
            take_profit = price - self.tp_atr_mult * atr
            logger.info(
                f"[{self.name}] SELL {symbol} | prob_down={prob_down:.3f} | "
                f"stability={consec}/{self.signal_stability_bars}"
            )
            return self._make_signal(
                Signal.SELL, symbol, df,
                confidence=prob_down, stop_loss=stop_loss,
                take_profit=take_profit, metadata=metadata,
            )

        return self._make_signal(Signal.HOLD, symbol, df, metadata=metadata)
