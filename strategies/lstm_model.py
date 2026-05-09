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
        self._load_model()

    @property
    def required_history_bars(self) -> int:
        return self.sequence_length + 20

    def _load_model(self):
        try:
            import torch
            import pickle

            if os.path.exists(self.model_path):
                self._model = torch.load(self.model_path, map_location="cpu", weights_only=False)
                self._model.eval()
                logger.info(f"LSTM model loaded from {self.model_path}")
            else:
                logger.warning(
                    f"LSTM model not found at {self.model_path}. "
                    f"Run `python training/train_lstm.py` to train."
                )

            if os.path.exists(self.scaler_path):
                with open(self.scaler_path, "rb") as f:
                    self._scaler = pickle.load(f)
        except ImportError:
            logger.warning("PyTorch not installed. LSTM strategy disabled. Install: pip install torch")
        except Exception as e:
            logger.warning(f"Failed to load LSTM model: {e}")

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        if self._model is None:
            return self._make_signal(Signal.HOLD, symbol, data, metadata={"reason": "model_not_loaded"})

        if not self.is_data_sufficient(data):
            return self._make_signal(Signal.HOLD, symbol, data, metadata={"reason": "insufficient_data"})

        try:
            import torch
        except ImportError:
            return self._make_signal(Signal.HOLD, symbol, data, metadata={"reason": "torch_not_installed"})

        df = self._feature_engine.compute_all(data)
        feature_cols = self._feature_engine.get_ml_feature_columns()
        available_cols = [c for c in feature_cols if c in df.columns]

        if len(available_cols) < 5:
            return self._make_signal(Signal.HOLD, symbol, df, metadata={"reason": "insufficient_features"})

        # Prepare sequence input
        feature_data = df[available_cols].iloc[-self.sequence_length:].copy()
        feature_data = feature_data.fillna(0)

        if self._scaler is not None:
            feature_data = pd.DataFrame(
                self._scaler.transform(feature_data),
                columns=available_cols,
                index=feature_data.index,
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
            logger.info(f"[{self.name}] SELL {symbol} | prob_down={prob_down:.3f}")
            return self._make_signal(Signal.SELL, symbol, df, confidence=prob_down, metadata=metadata)

        return self._make_signal(Signal.HOLD, symbol, df, metadata=metadata)
