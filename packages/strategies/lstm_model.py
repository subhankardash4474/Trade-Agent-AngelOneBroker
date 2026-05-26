"""
LSTM Price Model Strategy
Deep learning strategy using an LSTM network to predict short-term
price movements based on sequential feature data.
"""

import os
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from core.features import FeatureEngine
from strategies.base_strategy import BaseStrategy, Signal, TradeSignal


class LSTMPriceModel(BaseStrategy):
    """
    LSTM-based price movement prediction strategy.

    Uses a trained PyTorch LSTM model that takes a sequence of feature
    vectors and outputs a predicted price direction + magnitude.

    Parameters:
        model_path: Path to saved LSTM model (default: models/lstm_model.pt).
        scaler_path: Path to saved feature scaler (default: models/lstm_scaler.pkl).
        sequence_length: Number of historical bars per input sequence (default 30).
        confidence_threshold: Minimum confidence to act (default 0.6).
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        params = params or {}
        defaults = {
            "model_path": "models/lstm_model.pt",
            "scaler_path": "models/lstm_scaler.pkl",
            "sequence_length": 30,
            "confidence_threshold": 0.6,
            "timeframe": "15min",
        }
        merged = {**defaults, **params}
        super().__init__(name="lstm_price_model", params=merged)

        self.model_path: str = merged["model_path"]
        self.scaler_path: str = merged["scaler_path"]
        self.sequence_length: int = merged["sequence_length"]
        self.confidence_threshold: float = merged["confidence_threshold"]
        self._feature_engine = FeatureEngine()
        self._model = None
        self._scaler = None
        # F-15 (audit 2026-05-27): unhealthy reason is set when a
        # load-time invariant is violated (model loads but scaler
        # missing, feature-count drift, etc.). The strategy then
        # returns a HOLD with this reason on every cycle instead
        # of emitting arbitrary signals from un-scaled inputs.
        self._unhealthy_reason: Optional[str] = None
        # F-13: mirror the XGBoost market-context plumbing.
        # `trading_agent` calls `set_market_context()` on every strategy
        # before each scan cycle (Nifty trend + India VIX). Previously
        # this was stored on `self._market_context` but `generate_signal`
        # never passed it into `FeatureEngine.compute_all`, so nifty_trend
        # / india_vix silently fell back to the engine's neutral defaults
        # (0 / 15.0) and diverged from BOTH the training pipeline AND the
        # live XGBoost path. That is silent train/serve skew. Fixed by
        # passing the stored ctx into compute_all() below.
        self._market_context: Dict[str, Any] = {}
        # F-14: NaN-rate train/serve skew tripwire — log a one-shot
        # warning when an inference window arrives with more than this
        # fraction of NaN feature values. Training pads NaNs to 0 but
        # the *upstream* preprocessing drops the worst rows; if live
        # inference is filling >25% of cells with zeros we're effectively
        # asking the model to extrapolate, and the prediction is junk.
        self._nan_warn_threshold = 0.25
        self._nan_warned_for: set[str] = set()
        self._load_model()

    @property
    def required_history_bars(self) -> int:
        return self.sequence_length + 20

    def set_market_context(self, context: Dict[str, Any]) -> None:
        """Push live regime/VIX context for use during the next inference.

        F-13 (audit 2026-05-27): symmetric with XGBoostClassifier so
        the orchestrator can call set_market_context() uniformly on
        every ML strategy. Previously the implementation also called
        `_feature_engine.set_market_context(...)` and silently
        swallowed AttributeError -- but `FeatureEngine` is stateless
        and takes `market_context` as a `compute_all()` kwarg, so the
        except branch hit on every call and the context never reached
        the features. Now we just stash it and the inference path
        passes it explicitly.
        """
        if not context:
            return
        self._market_context = dict(context)

    def _load_model(self):
        try:
            import torch
            import pickle

            if os.path.exists(self.model_path):
                # B-19 (audit 2026-05-25): `torch.load(..., weights_only=False)`
                # uses pickle deserialisation under the hood, which executes
                # arbitrary code embedded in the model file. We MUST load
                # from a path under our own control (the operator-owned
                # `models/` directory) — never from a tempdir, download
                # cache, or operator-supplied path. The startup log line
                # below makes the source path visible for audit. We keep
                # `weights_only=False` because the current artifacts use
                # full-module pickle; future retrains should migrate to
                # `state_dict()` + reconstruct + `weights_only=True` so we
                # can flip this safely (tracked separately).
                abs_path = os.path.abspath(self.model_path)
                logger.info(
                    f"[security] Loading LSTM model from trusted path: {abs_path}"
                )
                self._model = torch.load(self.model_path, map_location="cpu", weights_only=False)
                self._model.eval()
                logger.info(f"LSTM model loaded from {self.model_path}")
            else:
                self._unhealthy_reason = "model_file_missing"
                logger.warning(
                    f"LSTM model not found at {self.model_path}. "
                    f"Run `python training/train_lstm.py` to train."
                )

            if os.path.exists(self.scaler_path):
                # B-19: same threat model as torch.load — scaler is also
                # arbitrary-code-on-load via pickle. Log the path so the
                # security audit trail can verify provenance.
                abs_scaler_path = os.path.abspath(self.scaler_path)
                logger.info(
                    f"[security] Loading LSTM scaler from trusted path: {abs_scaler_path}"
                )
                with open(self.scaler_path, "rb") as f:
                    self._scaler = pickle.load(f)
            elif self._model is not None:
                # F-15: A model trained with StandardScaler (see
                # `training/train_lstm.py`) expects mean-0 / unit-variance
                # inputs. Feeding raw OHLCV-derived features straight
                # through the LSTM produces predictions in the wrong
                # numeric regime — confidently wrong, not silently
                # diffuse. Mark unhealthy so generate_signal HOLDs
                # instead of trading on garbage.
                self._unhealthy_reason = "scaler_missing"
                logger.error(
                    f"[LSTM-HEALTH] Model loaded but scaler missing at "
                    f"{self.scaler_path}. Strategy will return HOLD on "
                    f"every cycle (un-scaled features would yield "
                    f"arbitrary predictions). Re-run "
                    f"`python training/train_lstm.py` to regenerate the "
                    f"scaler alongside the model."
                )
                self._model = None  # force HOLD via the model_not_loaded path

            # F-42: feature-count contract validation. If the live
            # FeatureEngine produces a different number of columns
            # than the model expects on its first LSTM layer, the
            # strategy can either crash on tensor mismatch or, worse,
            # silently slice/zero-pad and produce noise. Refuse early.
            if self._model is not None:
                self._validate_model_contract()
        except ImportError:
            logger.warning("PyTorch not installed. LSTM strategy disabled. Install: pip install torch")
            self._unhealthy_reason = "torch_not_installed"
        except Exception as e:
            logger.warning(f"Failed to load LSTM model: {e}")
            self._unhealthy_reason = f"load_failed: {type(e).__name__}: {e}"
            self._model = None

    def _validate_model_contract(self) -> None:
        """F-42: enforce input-feature count parity with the live
        FeatureEngine. We read it off the FIRST nn.LSTM submodule's
        ``input_size`` attribute (PyTorch convention). If we can't
        find it (custom architectures), we skip validation rather
        than fail-closed — the contract is best-effort, not gospel.
        """
        try:
            live_cols = self._feature_engine.get_ml_feature_columns()
            expected = None
            try:
                import torch.nn as nn
                for m in self._model.modules():
                    if isinstance(m, nn.LSTM):
                        expected = int(m.input_size)
                        break
            except Exception:
                expected = None

            if expected is None:
                return  # nothing to compare against
            if expected != len(live_cols):
                self._unhealthy_reason = (
                    f"feature_count_drift: model={expected} "
                    f"live_engine={len(live_cols)}"
                )
                logger.error(
                    f"[LSTM-HEALTH] FEATURE COUNT DRIFT — model expects "
                    f"{expected} features, FeatureEngine produces "
                    f"{len(live_cols)}. Strategy will return HOLD until "
                    f"the model is retrained. Usually means a feature "
                    f"was added/removed without retraining."
                )
                self._model = None
        except Exception as exc:
            # Validation is best-effort; never let it crash startup.
            logger.warning(f"[LSTM-HEALTH] contract validation skipped: {exc}")

    def is_healthy(self) -> bool:
        """F-15 + F-42: True iff the strategy can emit live signals.
        Mirrors XGBoostClassifier.is_healthy()."""
        return self._model is not None and self._unhealthy_reason is None

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        if self._model is None:
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": self._unhealthy_reason or "model_not_loaded"},
            )

        if not self.is_data_sufficient(data):
            return self._make_signal(Signal.HOLD, symbol, data, metadata={"reason": "insufficient_data"})

        try:
            import torch
        except ImportError:
            return self._make_signal(Signal.HOLD, symbol, data, metadata={"reason": "torch_not_installed"})

        # F-13: pass the live market context (nifty_trend / india_vix /
        # sector_momentum) into the feature engine so inference matches
        # both the training pipeline and the XGBoost live path.
        df = self._feature_engine.compute_all(
            data, market_context=self._market_context or None
        )
        feature_cols = self._feature_engine.get_ml_feature_columns()
        available_cols = [c for c in feature_cols if c in df.columns]

        if len(available_cols) < 5:
            return self._make_signal(Signal.HOLD, symbol, df, metadata={"reason": "insufficient_features"})

        # Prepare sequence input
        feature_data = df[available_cols].iloc[-self.sequence_length:].copy()
        # F-14: detect train/serve skew from NaN dominance BEFORE we
        # fillna(0). The training pipeline does its own fillna(0) but
        # upstream preprocessing dropped the worst rows; if live
        # inference is filling >25% of cells with zeros we're feeding
        # the model out-of-distribution input. Log once per symbol and
        # HOLD — better to skip a cycle than emit a junk signal.
        try:
            total_cells = feature_data.size
            nan_cells = int(feature_data.isna().sum().sum())
            if total_cells > 0 and (nan_cells / total_cells) >= self._nan_warn_threshold:
                if symbol not in self._nan_warned_for:
                    logger.warning(
                        f"[LSTM-SKEW] {symbol}: {nan_cells}/{total_cells} "
                        f"({nan_cells/total_cells:.0%}) feature cells are NaN. "
                        f"Skipping inference (would diverge from training "
                        f"distribution)."
                    )
                    self._nan_warned_for.add(symbol)
                return self._make_signal(
                    Signal.HOLD, symbol, df,
                    metadata={
                        "reason": "feature_nan_skew",
                        "nan_fraction": round(nan_cells / total_cells, 3),
                    },
                )
        except Exception:
            # NaN counting is diagnostics — never let it block inference.
            pass
        feature_data = feature_data.fillna(0)

        if self._scaler is not None:
            feature_data = pd.DataFrame(
                self._scaler.transform(feature_data),
                columns=available_cols,
                index=feature_data.index,
            )
        else:
            # F-15: belt-and-braces. _load_model already disables the
            # strategy when the scaler is absent, but if anything ever
            # re-enables the model without a scaler, HOLD instead of
            # silently feeding raw features.
            return self._make_signal(
                Signal.HOLD, symbol, df,
                metadata={"reason": "scaler_missing"},
            )

        try:
            x = torch.FloatTensor(feature_data.values).unsqueeze(0)  # (1, seq_len, features)
            with torch.no_grad():
                output = self._model(x)

            if output.shape[-1] >= 2:
                probs = torch.softmax(output, dim=-1).numpy()[0]
                prob_up, prob_down = float(probs[1]), float(probs[0])
            else:
                pred_val = float(output.numpy()[0][0])
                prob_up = 1.0 / (1.0 + np.exp(-pred_val))
                prob_down = 1 - prob_up

        except Exception as e:
            logger.error(f"LSTM inference error: {e}")
            return self._make_signal(Signal.HOLD, symbol, df, metadata={"reason": f"inference_error: {e}"})

        price = float(df["close"].iloc[-1])
        atr = float(df["atr"].iloc[-1]) if "atr" in df.columns and not pd.isna(df["atr"].iloc[-1]) else price * 0.01

        metadata = {
            "prob_up": round(prob_up, 4),
            "prob_down": round(prob_down, 4),
        }

        if prob_up >= self.confidence_threshold and prob_up > prob_down:
            stop_loss = price - 1.5 * atr
            take_profit = price + 2.5 * atr
            logger.info(f"[{self.name}] BUY {symbol} | prob_up={prob_up:.3f}")
            return self._make_signal(
                Signal.BUY, symbol, df,
                confidence=prob_up, stop_loss=stop_loss,
                take_profit=take_profit, metadata=metadata,
            )

        if prob_down >= self.confidence_threshold and prob_down > prob_up:
            # C-8 (audit 2026-05-26): the SELL branch previously returned
            # no stop_loss / take_profit. Downstream the ensemble fell back
            # to generic 1.5% / 3% defaults, asymmetric with the BUY branch
            # which uses 1.5× ATR / 2.5× ATR. Mirror the BUY math so a
            # short and a long off the same |prob - 0.5| produce
            # comparable R:R. Sign flip: SL above entry, TP below entry.
            stop_loss = price + 1.5 * atr
            take_profit = price - 2.5 * atr
            logger.info(f"[{self.name}] SELL {symbol} | prob_down={prob_down:.3f}")
            return self._make_signal(
                Signal.SELL, symbol, df,
                confidence=prob_down, stop_loss=stop_loss,
                take_profit=take_profit, metadata=metadata,
            )

        return self._make_signal(Signal.HOLD, symbol, df, metadata=metadata)
