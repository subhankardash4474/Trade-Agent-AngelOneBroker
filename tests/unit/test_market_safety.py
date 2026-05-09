"""
Tests for core/market_safety.py — circuit-limit detection, data-quality
guard, and sector concentration limits.
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
import pytz

from core.market_safety import (
    CIRCUIT_HARD_LIMIT_PCT,
    CIRCUIT_PROXIMITY_PCT,
    check_circuit_risk,
    check_data_quality,
    check_sector_exposure,
    get_sector,
)

IST = pytz.timezone("Asia/Kolkata")


class TestCircuitRisk:
    def test_normal_move_is_safe(self):
        safe, _ = check_circuit_risk(current_price=100.0, previous_close=98.0)
        assert safe is True

    def test_near_upper_circuit_rejected(self):
        # +9% move → near upper circuit
        safe, reason = check_circuit_risk(current_price=109.0, previous_close=100.0)
        assert safe is False
        assert "circuit" in reason

    def test_near_lower_circuit_rejected(self):
        safe, reason = check_circuit_risk(current_price=91.0, previous_close=100.0)
        assert safe is False
        assert "circuit" in reason

    def test_hard_circuit_always_rejected(self):
        safe, reason = check_circuit_risk(current_price=120.0, previous_close=100.0)
        assert safe is False
        assert "hard_circuit" in reason

    def test_at_day_high_after_big_range_rejected(self):
        safe, reason = check_circuit_risk(
            current_price=107.0, previous_close=105.0,
            day_high=107.0, day_low=100.0,  # 7% intraday range, at the top
        )
        assert safe is False
        assert "day_high" in reason or "exhausted" in reason

    def test_missing_prev_close_is_permissive(self):
        # Can't evaluate without prev close — caller should decide
        safe, reason = check_circuit_risk(current_price=100.0, previous_close=None)
        assert safe is True
        assert "no_prev_close" in reason

    def test_invalid_price_rejected(self):
        safe, _ = check_circuit_risk(current_price=0, previous_close=100.0)
        assert safe is False


class TestDataQuality:
    def _make_df(self, n=20, spike_at=None, nans=False, stale=False, zero_vol=False):
        now = datetime.now(IST) - timedelta(minutes=5)
        idx = [now - timedelta(minutes=5 * i) for i in range(n)][::-1]
        if stale:
            idx = [i - timedelta(hours=6) for i in idx]
        closes = np.linspace(100, 110, n).astype(float)
        if spike_at is not None:
            closes[spike_at] = closes[spike_at] * 2  # 100% spike
        df = pd.DataFrame({
            "open": closes * 0.99,
            "high": closes * 1.01,
            "low": closes * 0.98,
            "close": closes,
            "volume": np.full(n, 0.0 if zero_vol else 1_000_000.0),
        }, index=pd.DatetimeIndex(idx))
        if nans:
            df.iloc[-1, df.columns.get_loc("close")] = np.nan
        return df

    def test_clean_data_passes(self):
        df = self._make_df()
        ok, _ = check_data_quality(df)
        assert ok is True

    def test_empty_df_rejected(self):
        ok, reason = check_data_quality(pd.DataFrame())
        assert ok is False
        assert "empty" in reason

    def test_missing_columns_rejected(self):
        df = pd.DataFrame({"close": [1, 2, 3]})
        ok, reason = check_data_quality(df)
        assert ok is False
        assert "missing_columns" in reason

    def test_nan_in_recent_rejected(self):
        df = self._make_df(nans=True)
        ok, reason = check_data_quality(df)
        assert ok is False
        assert "nan" in reason

    def test_zero_volume_rejected(self):
        df = self._make_df(zero_vol=True)
        ok, reason = check_data_quality(df, min_volume=1.0)
        assert ok is False

    def test_stale_data_rejected(self):
        df = self._make_df(stale=True)
        ok, reason = check_data_quality(df, max_staleness_minutes=30)
        assert ok is False
        assert "stale" in reason

    def test_price_spike_rejected(self):
        df = self._make_df(spike_at=-1)
        ok, reason = check_data_quality(df)
        assert ok is False
        assert "spike" in reason


class TestSectorExposure:
    def test_new_sector_allowed(self):
        ok, _ = check_sector_exposure(
            symbol="TCS",
            current_positions_by_symbol={"HDFCBANK": 2000.0},
            additional_cost=2000.0,
            total_equity=10_000.0,
            max_sector_exposure_pct=40.0,
        )
        assert ok is True

    def test_same_sector_over_cap_rejected(self):
        # Two banks already at Rs 3500 on Rs 10K equity = 35%
        # Adding another bank at Rs 800 would push to 43% > 40% cap.
        ok, reason = check_sector_exposure(
            symbol="ICICIBANK",
            current_positions_by_symbol={"HDFCBANK": 2000.0, "SBIN": 1500.0},
            additional_cost=800.0,
            total_equity=10_000.0,
            max_sector_exposure_pct=40.0,
        )
        assert ok is False
        assert "Banks" in reason or "sector_concentration" in reason

    def test_unknown_symbol_bucketed(self):
        # Unknown symbols still get sector-limited (under UNKNOWN bucket)
        ok, _ = check_sector_exposure(
            symbol="FAKETICKER",
            current_positions_by_symbol={},
            additional_cost=1000.0,
            total_equity=10_000.0,
            max_sector_exposure_pct=40.0,
        )
        assert ok is True

    def test_zero_equity_permissive(self):
        ok, _ = check_sector_exposure(
            symbol="TCS",
            current_positions_by_symbol={},
            additional_cost=1000.0,
            total_equity=0.0,
        )
        assert ok is True


class TestSectorMap:
    @pytest.mark.parametrize(
        "symbol,sector",
        [
            ("HDFCBANK", "Banks"),
            ("TCS", "IT"),
            ("SUNPHARMA", "Pharma"),
            ("RELIANCE", "Energy"),
            ("ADANIENSOL", "Power"),
            ("COCHINSHIP", "Defence"),
            ("FAKETICKER", "UNKNOWN"),
        ],
    )
    def test_sector_lookup(self, symbol, sector):
        assert get_sector(symbol) == sector
