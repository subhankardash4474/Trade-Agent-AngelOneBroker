"""
Feature Engineering Pipeline
Computes a comprehensive set of technical indicators, volume metrics,
price action patterns, and market regime features in real-time.
"""

from typing import Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger


class FeatureEngine:
    """
    Computes all features required by rule-based and ML strategies.

    Feature categories:
      - Trend: EMA(9,21,50), MACD, ADX, Supertrend
      - Momentum: RSI(14), Stochastic, Williams %R, ROC
      - Volatility: Bollinger Bands, ATR(14), Keltner Channels
      - Volume: VWAP, OBV, Volume ratio
      - Price Action: Candle patterns, Support/Resistance levels
      - Market: Nifty trend, VIX level (injected externally)
      - Derived: Distance from day high/low, gap %, pre-market volume
    """

    def compute_all(self, df: pd.DataFrame, market_context: Optional[dict] = None) -> pd.DataFrame:
        """
        Compute all features on an OHLCV DataFrame.
        Modifies df in-place and returns it.
        """
        if df.empty or len(df) < 2:
            return df

        df = df.copy()
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df = self._add_trend_features(df)
        df = self._add_momentum_features(df)
        df = self._add_volatility_features(df)
        df = self._add_volume_features(df)
        df = self._add_price_action_features(df)
        df = self._add_derived_features(df)

        # 2026-05-14 BUGFIX: always emit market_context columns so the
        # column contract of `get_ml_feature_columns()` is honoured.
        # Previously this was gated on `if market_context:` which left
        # nifty_trend/india_vix absent whenever the caller hadn't wired
        # `set_market_context()` first — fine for trading_agent.py (which
        # always sets it) but broken for the battery, ad-hoc scripts,
        # and any test path. The defaults inside `_add_market_context`
        # (nifty_trend=0 neutral, india_vix=15.0 mid-vol, sector=0.0)
        # are safe stand-ins when no live context is available.
        df = self._add_market_context(df, market_context or {})

        return df

    # ── Trend ────────────────────────────────────────────────

    @staticmethod
    def _add_trend_features(df: pd.DataFrame) -> pd.DataFrame:
        close = df["close"]

        # EMAs
        for span in (9, 21, 50):
            df[f"ema_{span}"] = close.ewm(span=span, adjust=False).mean()

        # MACD
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        df["macd"] = ema12 - ema26
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        df["macd_histogram"] = df["macd"] - df["macd_signal"]

        # ADX (Average Directional Index)
        df = FeatureEngine._compute_adx(df, period=14)

        # Supertrend
        df = FeatureEngine._compute_supertrend(df, period=10, multiplier=3)

        return df

    @staticmethod
    def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        high, low, close = df["high"], df["low"], df["close"]

        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)

        atr = tr.ewm(span=period, adjust=False).mean()
        plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr)
        minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr)

        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        df["adx"] = dx.ewm(span=period, adjust=False).mean()
        df["plus_di"] = plus_di
        df["minus_di"] = minus_di
        return df

    @staticmethod
    def _compute_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
        high, low, close = df["high"], df["low"], df["close"]
        hl2 = (high + low) / 2

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()

        upper_band = hl2 + multiplier * atr
        lower_band = hl2 - multiplier * atr

        supertrend = pd.Series(np.nan, index=df.index)
        direction = pd.Series(1, index=df.index)  # 1 = uptrend, -1 = downtrend

        for i in range(1, len(df)):
            if close.iloc[i] > upper_band.iloc[i - 1]:
                direction.iloc[i] = 1
            elif close.iloc[i] < lower_band.iloc[i - 1]:
                direction.iloc[i] = -1
            else:
                direction.iloc[i] = direction.iloc[i - 1]

            if direction.iloc[i] == 1:
                lower_band.iloc[i] = max(lower_band.iloc[i], lower_band.iloc[i - 1]) if direction.iloc[i - 1] == 1 else lower_band.iloc[i]
                supertrend.iloc[i] = lower_band.iloc[i]
            else:
                upper_band.iloc[i] = min(upper_band.iloc[i], upper_band.iloc[i - 1]) if direction.iloc[i - 1] == -1 else upper_band.iloc[i]
                supertrend.iloc[i] = upper_band.iloc[i]

        df["supertrend"] = supertrend
        df["supertrend_direction"] = direction
        return df

    # ── Momentum ─────────────────────────────────────────────

    @staticmethod
    def _add_momentum_features(df: pd.DataFrame) -> pd.DataFrame:
        close, high, low = df["close"], df["high"], df["low"]

        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(com=13, min_periods=14).mean()
        avg_loss = loss.ewm(com=13, min_periods=14).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))

        # Stochastic Oscillator (14, 3, 3)
        low14 = low.rolling(14).min()
        high14 = high.rolling(14).max()
        df["stoch_k"] = 100 * (close - low14) / (high14 - low14).replace(0, np.nan)
        df["stoch_d"] = df["stoch_k"].rolling(3).mean()

        # Williams %R
        df["williams_r"] = -100 * (high14 - close) / (high14 - low14).replace(0, np.nan)

        # Rate of Change (12-period)
        df["roc"] = close.pct_change(12) * 100

        return df

    # ── Volatility ───────────────────────────────────────────

    @staticmethod
    def _add_volatility_features(df: pd.DataFrame) -> pd.DataFrame:
        close, high, low = df["close"], df["high"], df["low"]

        # ATR (14)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = tr.ewm(span=14, adjust=False).mean()

        # Bollinger Bands (20, 2)
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        df["bb_upper"] = sma20 + 2 * std20
        df["bb_middle"] = sma20
        df["bb_lower"] = sma20 - 2 * std20
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / sma20
        df["bb_pct"] = (close - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)

        # Keltner Channels (20, 1.5)
        ema20 = close.ewm(span=20, adjust=False).mean()
        df["kc_upper"] = ema20 + 1.5 * df["atr"]
        df["kc_lower"] = ema20 - 1.5 * df["atr"]

        return df

    # ── Volume ───────────────────────────────────────────────

    @staticmethod
    def _add_volume_features(df: pd.DataFrame) -> pd.DataFrame:
        close, volume = df["close"], df["volume"]
        high, low = df["high"], df["low"]

        # VWAP — session-reset: cumulative sums restart each calendar day
        typical_price = (high + low + close) / 3
        tp_vol = typical_price * volume
        if hasattr(df.index, "date"):
            day = df.index.date
            cum_tp_vol = tp_vol.groupby(day).cumsum()
            cum_vol = volume.groupby(day).cumsum()
        else:
            cum_tp_vol = tp_vol.cumsum()
            cum_vol = volume.cumsum()
        df["vwap"] = cum_tp_vol / cum_vol.replace(0, np.nan)

        # OBV (On-Balance Volume) — vectorized
        direction = np.sign(close.diff())
        df["obv"] = (direction * volume).fillna(0).cumsum()

        # Volume ratio (current bar vs 20-period average)
        vol_ma20 = volume.rolling(20).mean()
        df["volume_ratio"] = volume / vol_ma20.replace(0, np.nan)

        return df

    # ── Price Action ─────────────────────────────────────────

    @staticmethod
    def _add_price_action_features(df: pd.DataFrame) -> pd.DataFrame:
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        body = (c - o).abs()
        upper_shadow = h - pd.concat([o, c], axis=1).max(axis=1)
        lower_shadow = pd.concat([o, c], axis=1).min(axis=1) - l
        total_range = (h - l).replace(0, np.nan)

        # Doji: body is < 10% of total range
        df["is_doji"] = (body / total_range < 0.1).astype(int)

        # Hammer: small body at top, long lower shadow (2x body)
        df["is_hammer"] = ((lower_shadow > 2 * body) & (upper_shadow < body * 0.5) & (body > 0)).astype(int)

        # Engulfing patterns
        prev_body = body.shift(1)
        prev_o, prev_c = o.shift(1), c.shift(1)
        df["is_bullish_engulfing"] = ((c > o) & (prev_c < prev_o) & (o <= prev_c) & (c >= prev_o) & (body > prev_body)).astype(int)
        df["is_bearish_engulfing"] = ((c < o) & (prev_c > prev_o) & (o >= prev_c) & (c <= prev_o) & (body > prev_body)).astype(int)

        # Support / Resistance via rolling pivot points
        df["pivot"] = (h.shift(1) + l.shift(1) + c.shift(1)) / 3
        df["support_1"] = 2 * df["pivot"] - h.shift(1)
        df["resistance_1"] = 2 * df["pivot"] - l.shift(1)

        return df

    # ── Derived ──────────────────────────────────────────────

    @staticmethod
    def _add_derived_features(df: pd.DataFrame) -> pd.DataFrame:
        close, high, low, opn = df["close"], df["high"], df["low"], df["open"]

        # Distance from day high / low (as % of price)
        rolling_high = high.rolling(78).max()  # ~1 day on 5m candles
        rolling_low = low.rolling(78).min()
        df["dist_from_high_pct"] = (rolling_high - close) / close * 100
        df["dist_from_low_pct"] = (close - rolling_low) / close * 100

        # Gap % (open vs previous close)
        df["gap_pct"] = (opn - close.shift(1)) / close.shift(1) * 100

        # Candle body as % of range
        total_range = (high - low).replace(0, np.nan)
        df["body_pct"] = (close - opn).abs() / total_range

        # 2026-05-14 ML uplift features ---------------------------------
        # These are computed from columns the prior pipeline already
        # populated (atr, supertrend, vwap, obv, rsi). Each is a thin
        # transform that gives the XGBoost a *normalised* read on a
        # signal whose raw value varies wildly by stock price level.
        if "supertrend" in df.columns and "atr" in df.columns:
            atr_safe = df["atr"].replace(0, np.nan)
            df["dist_from_supertrend_atr"] = (close - df["supertrend"]) / atr_safe
        if "vwap" in df.columns:
            vwap_safe = df["vwap"].replace(0, np.nan)
            df["vwap_dist_pct"] = (close - df["vwap"]) / vwap_safe * 100
        if "atr" in df.columns:
            tr = pd.concat([
                high - low,
                (high - close.shift()).abs(),
                (low - close.shift()).abs(),
            ], axis=1).max(axis=1)
            avg_tr20 = tr.rolling(20).mean().replace(0, np.nan)
            df["range_expansion"] = tr / avg_tr20
        if "rsi" in df.columns:
            df["rsi_delta_3"] = df["rsi"].diff(3)
        if "obv" in df.columns:
            obv_ma20 = df["obv"].rolling(20).mean().replace(0, np.nan)
            df["obv_ratio"] = df["obv"] / obv_ma20

        # Time-of-day / day-of-week features (2026-05-06).
        # Markets behave differently at open (high vol/vol), midday (calm),
        # close (high vol/vol). Same with day-of-week (Mon/Fri patterns).
        # Encoded as sin/cos pairs so the model sees cyclical proximity
        # (15:25 is "near" 09:15 of the next day, not "far"). Both pairs
        # are bounded [-1, 1], so they mix cleanly with normalized features.
        # Falls back to neutral 0s when index is non-datetime (defensive).
        if isinstance(df.index, pd.DatetimeIndex):
            # Minutes since 09:15 (NSE open) — captures intraday position.
            # Day length = 6.25 hr = 375 min, so 2π/375 maps a full day
            # to one cycle. Cast to numpy first because pandas Index
            # (the result of `.hour * 60 + .minute`) doesn't expose .clip
            # the same way Series does.
            mins_raw = (
                df.index.hour.values * 60 + df.index.minute.values - (9 * 60 + 15)
            )
            mins_since_open = np.clip(mins_raw, 0, 375)
            theta_intraday = 2 * np.pi * mins_since_open / 375.0
            df["tod_sin"] = np.sin(theta_intraday)
            df["tod_cos"] = np.cos(theta_intraday)
            # Day-of-week (0=Mon ... 4=Fri)
            theta_dow = 2 * np.pi * df.index.dayofweek.values / 7.0
            df["dow_sin"] = np.sin(theta_dow)
            df["dow_cos"] = np.cos(theta_dow)
        else:
            df["tod_sin"] = 0.0
            df["tod_cos"] = 1.0  # neutral cosine
            df["dow_sin"] = 0.0
            df["dow_cos"] = 1.0

        return df

    # ── Market Context ───────────────────────────────────────

    @staticmethod
    def _add_market_context(df: pd.DataFrame, ctx: dict) -> pd.DataFrame:
        df["nifty_trend"] = ctx.get("nifty_trend", 0)       # 1=above 200EMA, -1=below
        df["india_vix"] = ctx.get("india_vix", 15.0)
        df["sector_momentum"] = ctx.get("sector_momentum", 0.0)
        return df

    def get_feature_columns(self) -> list[str]:
        """Return the list of feature column names produced by compute_all()."""
        return [
            # Trend
            "ema_9", "ema_21", "ema_50", "macd", "macd_signal", "macd_histogram",
            "adx", "plus_di", "minus_di", "supertrend", "supertrend_direction",
            # Momentum
            "rsi", "stoch_k", "stoch_d", "williams_r", "roc",
            # Volatility
            "atr", "bb_upper", "bb_middle", "bb_lower", "bb_width", "bb_pct",
            "kc_upper", "kc_lower",
            # Volume
            "vwap", "obv", "volume_ratio",
            # Price Action
            "is_doji", "is_hammer", "is_bullish_engulfing", "is_bearish_engulfing",
            "pivot", "support_1", "resistance_1",
            # Derived
            "dist_from_high_pct", "dist_from_low_pct", "gap_pct", "body_pct",
            # 2026-05-14: new derived features for ML feature pack.
            # Must stay registered here so get_ml_feature_columns()
            # is a true subset of get_feature_columns().
            "dist_from_supertrend_atr", "vwap_dist_pct",
            "range_expansion", "rsi_delta_3", "obv_ratio",
            # Time-of-day cyclical encoding
            "tod_sin", "tod_cos", "dow_sin", "dow_cos",
            # Market context
            "nifty_trend", "india_vix", "sector_momentum",
        ]

    def get_ml_feature_columns(self) -> list[str]:
        """Subset of features suitable for ML model input (all numeric, no NaN-heavy).

        2026-05-14: expanded from 23 to 31 features to address the
        "model is regime-blind" finding. New additions:
          * ema_50 -- long-term trend context (was computed but unused)
          * dist_from_supertrend_atr -- normalised distance from ST line
          * vwap_dist_pct -- intraday institutional reference offset
          * range_expansion -- bar TR vs 20-bar avg TR (vol regime)
          * rsi_delta_3 -- 3-bar RSI velocity (momentum acceleration)
          * obv_ratio -- accumulation vs 20-bar avg OBV (smart money)
          * nifty_trend, india_vix -- daily market context (regime)
        """
        return [
            # Trend
            "ema_9", "ema_21", "ema_50", "macd", "macd_histogram", "adx",
            # Momentum
            "rsi", "stoch_k", "williams_r", "roc",
            "rsi_delta_3",
            # Volatility / band position
            "atr", "bb_width", "bb_pct",
            "range_expansion",
            # Volume / accumulation
            "vwap", "volume_ratio", "obv_ratio",
            # Distance / position features
            "dist_from_high_pct", "dist_from_low_pct", "gap_pct", "body_pct",
            "dist_from_supertrend_atr", "vwap_dist_pct",
            # Trend regime (categorical -> int)
            "supertrend_direction",
            # Time-of-day cyclical pairs
            "tod_sin", "tod_cos", "dow_sin", "dow_cos",
            # Market context (filled by training pipeline + live agent)
            "nifty_trend", "india_vix",
        ]
