"""Tests for the Stock Scanner module."""

import pytest
from core.stock_scanner import StockScanner


@pytest.fixture
def config():
    return {
        "capital": {"initial_balance": 10000.0},
        "scanner": {
            "enabled": True,
            "top_n": 5,
            "max_price": 2000.0,
            "min_price": 5.0,
            "min_avg_volume": 500_000,
            "min_atr_pct": 1.0,
            "rescan_interval_minutes": 60,
            "universe": [],
        },
    }


class TestScannerInit:
    def test_default_init(self, config):
        scanner = StockScanner(config)
        assert scanner.top_n == 5
        assert scanner.max_price == 2000.0
        assert scanner.min_price == 5.0
        assert scanner.min_avg_volume == 500_000
        assert len(scanner.universe) > 0  # built-in universe

    def test_custom_universe(self, config):
        config["scanner"]["universe"] = ["SBIN", "TCS"]
        scanner = StockScanner(config)
        assert scanner.universe == ["SBIN", "TCS"]

    def test_needs_rescan_on_fresh(self, config):
        scanner = StockScanner(config)
        assert scanner.needs_rescan() is True


class TestFiltering:
    def test_price_filter(self, config):
        scanner = StockScanner(config)
        candidates = [
            {"symbol": "A", "price": 3.0, "avg_volume": 1_000_000, "atr_pct": 2.0, "rsi": 50},
            {"symbol": "B", "price": 100.0, "avg_volume": 1_000_000, "atr_pct": 2.0, "rsi": 50},
            {"symbol": "C", "price": 5000.0, "avg_volume": 1_000_000, "atr_pct": 2.0, "rsi": 50},
        ]
        filtered = scanner._apply_filters(candidates)
        symbols = [c["symbol"] for c in filtered]
        assert "A" not in symbols  # below min_price
        assert "B" in symbols
        assert "C" not in symbols  # above max_price

    def test_volume_filter(self, config):
        scanner = StockScanner(config)
        candidates = [
            {"symbol": "LOW_VOL", "price": 100, "avg_volume": 100, "atr_pct": 2.0, "rsi": 50},
            {"symbol": "HIGH_VOL", "price": 100, "avg_volume": 2_000_000, "atr_pct": 2.0, "rsi": 50},
        ]
        filtered = scanner._apply_filters(candidates)
        symbols = [c["symbol"] for c in filtered]
        assert "LOW_VOL" not in symbols
        assert "HIGH_VOL" in symbols

    def test_volatility_filter(self, config):
        scanner = StockScanner(config)
        candidates = [
            {"symbol": "FLAT", "price": 100, "avg_volume": 1_000_000, "atr_pct": 0.3, "rsi": 50},
            {"symbol": "VOLATILE", "price": 100, "avg_volume": 1_000_000, "atr_pct": 3.0, "rsi": 50},
        ]
        filtered = scanner._apply_filters(candidates)
        symbols = [c["symbol"] for c in filtered]
        assert "FLAT" not in symbols
        assert "VOLATILE" in symbols

    def test_rsi_extreme_filter(self, config):
        scanner = StockScanner(config)
        candidates = [
            {"symbol": "OVERBOUGHT", "price": 100, "avg_volume": 1_000_000, "atr_pct": 2.0, "rsi": 90},
            {"symbol": "OVERSOLD", "price": 100, "avg_volume": 1_000_000, "atr_pct": 2.0, "rsi": 10},
            {"symbol": "NORMAL", "price": 100, "avg_volume": 1_000_000, "atr_pct": 2.0, "rsi": 55},
        ]
        filtered = scanner._apply_filters(candidates)
        symbols = [c["symbol"] for c in filtered]
        assert "OVERBOUGHT" not in symbols
        assert "OVERSOLD" not in symbols
        assert "NORMAL" in symbols


class TestRanking:
    def test_ranking_selects_top_n(self, config):
        scanner = StockScanner(config)
        candidates = [
            {"symbol": f"S{i}", "price": 100, "avg_volume": 1_000_000,
             "vol_ratio": 1.0 + i * 0.1, "atr_pct": 2.0 + i * 0.2,
             "momentum_5d": i * 0.5, "rsi": 40 + i}
            for i in range(20)
        ]
        ranked = scanner._rank_and_select(candidates)
        assert len(ranked) == 5  # top_n
        assert all("score" in r for r in ranked)
        # Scores should be descending
        scores = [r["score"] for r in ranked]
        assert scores == sorted(scores, reverse=True)

    def test_empty_candidates(self, config):
        scanner = StockScanner(config)
        ranked = scanner._rank_and_select([])
        assert ranked == []


class TestScanSummary:
    def test_summary_no_results(self, config):
        scanner = StockScanner(config)
        assert "No scan results" in scanner.get_scan_summary()

    def test_summary_with_results(self, config):
        scanner = StockScanner(config)
        scanner._cached_results = [
            {"symbol": "SBIN", "price": 100.0, "avg_volume": 1_000_000,
             "atr_pct": 2.5, "momentum_5d": 1.2, "rsi": 55, "score": 0.85},
        ]
        summary = scanner.get_scan_summary()
        assert "SBIN" in summary
        assert "100.00" in summary
