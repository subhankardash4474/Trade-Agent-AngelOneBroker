"""Unit tests for the 2026-05-13 XGBoost direction-stability gate.

The gate exists because of a concrete live failure:

    09:34 XGB BUY  BAJFINANCE prob_up=0.695
    09:52 XGB SELL BAJFINANCE prob_down=0.725   <-- 180-degree flip in 18 min
    10:04 ENTRY: SHORT 20 @ 896.72
    10:31 STOP:        @ 903.53   (-Rs 155.28)

The fix: only emit BUY/SELL after `signal_stability_bars` consecutive
above-threshold same-side classifications on that symbol. HOLD signals do
not reset the counter; only an opposite-side above-threshold signal does.

These tests exercise the tracker in isolation (no model load, no feature
engineering) so they're fast and deterministic.
"""

from __future__ import annotations

import pytest


class _Stub:
    """Minimal stand-in for XGBoostClassifier carrying just the state and
    helper exercised by the stability tests. Mirrors the production
    implementation -- if the production helper changes shape, this stub
    must follow."""

    def __init__(self, signal_stability_bars: int = 2):
        self.signal_stability_bars = max(1, int(signal_stability_bars))
        self._stability_state: dict[str, tuple] = {}

    def _record_stability(self, symbol: str, side: str) -> int:
        last_side, consec = self._stability_state.get(symbol, (None, 0))
        if last_side == side:
            consec += 1
        else:
            consec = 1
        self._stability_state[symbol] = (side, consec)
        return consec


class TestStabilityCounter:
    def test_first_signal_yields_count_one(self):
        s = _Stub()
        assert s._record_stability("AAPL", "BUY") == 1

    def test_consecutive_same_side_increments(self):
        s = _Stub()
        assert s._record_stability("AAPL", "BUY") == 1
        assert s._record_stability("AAPL", "BUY") == 2
        assert s._record_stability("AAPL", "BUY") == 3

    def test_opposite_side_resets_counter(self):
        s = _Stub()
        s._record_stability("AAPL", "BUY")
        s._record_stability("AAPL", "BUY")
        # Flip resets to 1, not 0 -- we observed one SELL.
        assert s._record_stability("AAPL", "SELL") == 1

    def test_per_symbol_isolation(self):
        """A flip on AAPL must not affect MSFT's counter."""
        s = _Stub()
        s._record_stability("AAPL", "BUY")
        s._record_stability("MSFT", "BUY")
        s._record_stability("AAPL", "SELL")
        # MSFT still on count=1 (it has not flipped).
        assert s._stability_state["MSFT"] == ("BUY", 1)
        # AAPL reset.
        assert s._stability_state["AAPL"] == ("SELL", 1)

    def test_bajfinance_replay(self):
        """The actual 2026-05-13 BAJFINANCE event sequence."""
        s = _Stub(signal_stability_bars=2)
        # 09:34: XGB BUY
        c = s._record_stability("BAJFINANCE", "BUY")
        assert c == 1  # buffered, not emitted
        # 09:52: XGB flips to SELL. Counter resets.
        c = s._record_stability("BAJFINANCE", "SELL")
        assert c == 1  # ALSO buffered -- this is where the actual entry was taken
        # IF 09:56 had been another SELL, counter would have reached 2
        # and the entry would have fired then -- 22 min later than the
        # actual entry, more time for the flip to prove itself.
        c = s._record_stability("BAJFINANCE", "SELL")
        assert c == 2  # gate clears, would emit
        # Now suppose 10:00 swings back to BUY -- counter resets again.
        c = s._record_stability("BAJFINANCE", "BUY")
        assert c == 1


class TestGateInGenerateSignal:
    """Real integration with XGBoostClassifier.generate_signal --
    requires patching out model load and feature engineering.
    """

    @pytest.fixture
    def classifier(self, tmp_path, monkeypatch):
        """Build a classifier that 'thinks' BUY with prob_up=0.80 always,
        without needing a real model file or a DataFrame with all
        features.
        """
        from strategies.xgboost_classifier import XGBoostClassifier
        # Bypass _load_model and _validate_model_contract by routing the
        # constructor through a healthy stub.
        clf = XGBoostClassifier.__new__(XGBoostClassifier)
        # Mirror the relevant fields from __init__ so generate_signal works
        from strategies.base_strategy import BaseStrategy
        BaseStrategy.__init__(clf, name="xgboost_classifier", params={
            "model_path": "/nonexistent",
            "confidence_threshold": 0.65,
            "prediction_horizon": 3,
            "timeframe": "5min",
            "stale_days": 30,
            "trend_filter_pct": None,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 2.0,
            "signal_stability_bars": 2,
        })
        clf.model_path = "/nonexistent"
        clf.confidence_threshold = 0.65
        clf.prediction_horizon = 3
        clf.stale_days = 30
        clf.trend_filter_pct = None
        clf.sl_atr_mult = 1.5
        clf.tp_atr_mult = 2.0
        clf.signal_stability_bars = 2
        clf._stability_state = {}
        clf._unhealthy_reason = None
        clf._stale_warned = False
        # 2026-05-14: ML feature pipeline now accepts an optional market
        # context (nifty_trend, india_vix). Mirror the production default
        # of None so generate_signal() can call compute_all(df, ctx).
        clf._live_market_context = None
        clf._model = object()  # truthy non-None, predict_proba mocked below

        # Force is_healthy() to True
        clf._model = object()
        # is_data_sufficient defers to BaseStrategy default that checks
        # len(data) >= required_history_bars. We'll patch around it.
        monkeypatch.setattr(clf, "is_data_sufficient", lambda data: True)

        # Replace _feature_engine.compute_all with a passthrough and
        # get_ml_feature_columns with a single fake column.
        class _FeatureStub:
            def compute_all(self, df, market_context=None):
                # Add the column the gate looks for so the .iloc path works.
                # 2026-05-14: signature now accepts optional market_context
                # to match the production FeatureEngine.
                d = df.copy()
                if "atr" not in d.columns:
                    d["atr"] = 1.0
                return d

            def get_ml_feature_columns(self):
                return ["close"]

        clf._feature_engine = _FeatureStub()
        return clf

    def _make_df(self, n=60, close=100.0):
        import pandas as pd
        idx = pd.date_range("2026-05-13 09:30:00+05:30", periods=n, freq="1min")
        return pd.DataFrame(
            {"open": close, "high": close * 1.001, "low": close * 0.999,
             "close": close, "volume": 100000.0},
            index=idx,
        )

    def _patch_model_to_predict(self, clf, prob_up: float):
        prob_down = 1.0 - prob_up

        class _M:
            def predict_proba(self_inner, X):
                import numpy as np
                return np.array([[prob_down, prob_up]])

        clf._model = _M()

    def test_first_buy_signal_is_buffered_as_hold(self, classifier):
        from strategies.base_strategy import Signal
        self._patch_model_to_predict(classifier, prob_up=0.80)
        sig = classifier.generate_signal(self._make_df(), "AAPL")
        assert sig.signal == Signal.HOLD
        assert "stability_pending" in sig.metadata.get("reason", "")
        assert sig.metadata.get("pending_side") == "BUY"

    def test_second_consecutive_buy_emits_buy(self, classifier):
        from strategies.base_strategy import Signal
        self._patch_model_to_predict(classifier, prob_up=0.80)
        # 1st: buffered
        classifier.generate_signal(self._make_df(), "AAPL")
        # 2nd: emits (stability_bars=2)
        sig = classifier.generate_signal(self._make_df(), "AAPL")
        assert sig.signal == Signal.BUY

    def test_flip_resets_counter(self, classifier):
        from strategies.base_strategy import Signal
        self._patch_model_to_predict(classifier, prob_up=0.80)
        # 1st BUY: buffered
        classifier.generate_signal(self._make_df(), "AAPL")
        # Flip to SELL -- counter resets, this SELL is buffered too.
        self._patch_model_to_predict(classifier, prob_up=0.10)  # prob_down=0.90
        sig = classifier.generate_signal(self._make_df(), "AAPL")
        assert sig.signal == Signal.HOLD
        assert sig.metadata.get("pending_side") == "SELL"
        # 2nd SELL: emits
        sig = classifier.generate_signal(self._make_df(), "AAPL")
        assert sig.signal == Signal.SELL

    def test_stability_bars_one_disables_gate(self, classifier):
        """signal_stability_bars=1 must reproduce legacy single-signal behaviour
        so existing tunings and backtests aren't disturbed."""
        from strategies.base_strategy import Signal
        classifier.signal_stability_bars = 1
        self._patch_model_to_predict(classifier, prob_up=0.80)
        sig = classifier.generate_signal(self._make_df(), "AAPL")
        assert sig.signal == Signal.BUY

    def test_below_threshold_does_not_advance_counter(self, classifier):
        """If the model isn't confident enough to fire BUY/SELL, the
        gate must NOT record it -- so a HOLD-period doesn't leak credit
        toward a later opposite-side signal."""
        from strategies.base_strategy import Signal
        # Below-threshold prediction first (returns HOLD via the final
        # fallthrough, not via the gate).
        self._patch_model_to_predict(classifier, prob_up=0.55)  # < 0.65
        sig = classifier.generate_signal(self._make_df(), "AAPL")
        assert sig.signal == Signal.HOLD
        # Counter should remain empty for this symbol.
        assert "AAPL" not in classifier._stability_state

    def test_per_symbol_state_isolated_in_generate_signal(self, classifier):
        """Confirm the per-symbol isolation also works in the live
        generate_signal call path, not just in the helper."""
        from strategies.base_strategy import Signal
        self._patch_model_to_predict(classifier, prob_up=0.80)
        # AAPL builds up to 1
        classifier.generate_signal(self._make_df(), "AAPL")
        # MSFT first signal -- still buffered
        sig = classifier.generate_signal(self._make_df(), "MSFT")
        assert sig.signal == Signal.HOLD
        # AAPL 2nd: now emits
        sig = classifier.generate_signal(self._make_df(), "AAPL")
        assert sig.signal == Signal.BUY
