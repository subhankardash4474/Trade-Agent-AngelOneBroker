"""Tests for the Stock Scanner module."""

from unittest.mock import MagicMock, patch

import pytest

from core import stock_scanner as ss
from core.stock_scanner import NSE_UNIVERSE, StockScanner


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


class TestUseLiveUniverseFlag:
    """The freeze-safe gate added 2026-05-25.

    Doubling the universe mid-session is a real behavior change so the
    live archive-CSV fetch must be OPT-IN. Default = hardcoded list."""

    def test_default_uses_hardcoded_universe_no_network(self, config):
        """No use_live_universe flag in config => no network call,
        universe is exactly the hardcoded NSE_UNIVERSE."""
        with patch.object(ss, "_fetch_nse_index_symbols") as mock_fetch:
            scanner = StockScanner(config)
        mock_fetch.assert_not_called()
        assert len(scanner.universe) == len(set(NSE_UNIVERSE))
        assert set(scanner.universe) == set(NSE_UNIVERSE)

    def test_use_live_universe_true_calls_fetch(self, config):
        """When the operator explicitly enables live fetch, we hit the
        archive/API path and use whatever symbols come back."""
        config["scanner"]["use_live_universe"] = True
        live_syms = ["AAA", "BBB", "CCC"] + [f"SYM{i:03d}" for i in range(500)]
        with patch.object(ss, "_fetch_nse_index_symbols", return_value=live_syms) as mock_fetch:
            scanner = StockScanner(config)
        mock_fetch.assert_called_once()
        assert "AAA" in scanner.universe
        assert len(scanner.universe) == len(live_syms)

    def test_use_live_universe_falls_back_when_fetch_returns_empty(self, config):
        """If the operator opts into live but archives + API both fail,
        we MUST fall back to the hardcoded list rather than scan zero
        stocks (which would produce a no-op trading day)."""
        config["scanner"]["use_live_universe"] = True
        with patch.object(ss, "_fetch_nse_index_symbols", return_value=[]):
            scanner = StockScanner(config)
        assert len(scanner.universe) == len(set(NSE_UNIVERSE))

    def test_explicit_universe_wins_over_use_live(self, config):
        """An explicit `universe: [...]` in config always takes priority,
        regardless of use_live_universe -- this is how unit tests and
        small dev setups override behaviour."""
        config["scanner"]["use_live_universe"] = True
        config["scanner"]["universe"] = ["TCS", "SBIN"]
        with patch.object(ss, "_fetch_nse_index_symbols") as mock_fetch:
            scanner = StockScanner(config)
        mock_fetch.assert_not_called()
        assert scanner.universe == ["TCS", "SBIN"]


class TestNseUniverseHardcodedList:
    """Regression guard for the hardcoded fallback list.

    On 2026-05-25 the operator received a heartbeat showing only 169
    stocks in the watchlist, traced to (a) NSE Nifty 500 live API
    blocked from data-center IPs and (b) RECLTD duplicated in this
    list. Both fixed; this test prevents regression.
    """

    def test_no_duplicates_in_hardcoded_universe(self):
        """RECLTD was duplicated in the original list (lines 130 & 150).
        After dedupe in the constructor this hid; the hardcoded source
        itself must stay duplicate-free so 'len(NSE_UNIVERSE)' is the
        truth on first inspection."""
        seen = set()
        dupes = [s for s in NSE_UNIVERSE if s in seen or seen.add(s)]
        assert dupes == [], f"Duplicate symbols in NSE_UNIVERSE: {dupes}"

    def test_hardcoded_universe_size_within_documented_range(self):
        """The comment claims 'Nifty 50 + Next 50 + popular mids/smalls'
        which is roughly 230-260 stocks. If this drifts dramatically
        in either direction (someone deleted half the list, or pasted
        a whole new index in) we want a test failure to flag a review."""
        assert 200 <= len(NSE_UNIVERSE) <= 320, (
            f"NSE_UNIVERSE has {len(NSE_UNIVERSE)} entries; expected 200-320."
        )


class TestNseArchiveCsvFetch:
    """Tests for _fetch_nse_archive_csv -- the new primary fetch path
    added 2026-05-25 to bypass the NSE live API which 403s from cloud
    IPs."""

    def test_returns_symbols_from_valid_csv(self):
        """Happy path: archives.nseindia.com returns a Nifty 500 CSV
        with the expected header. We must extract the Symbol column
        (3rd field) only for EQ/BE series rows."""
        csv_body = (
            "Company Name,Industry,Symbol,Series,ISIN Code\n"
            + "\n".join(
                f"Co{i},Sector,SYM{i:03d},EQ,INE000{i:03d}01001"
                for i in range(60)
            )
        )
        resp = MagicMock(status_code=200, content=csv_body.encode(),
                         text=csv_body)
        with patch.object(ss.req, "get", return_value=resp) as mock_get:
            symbols = ss._fetch_nse_archive_csv("NIFTY 500")
        assert len(symbols) == 60
        assert symbols[0] == "SYM000"
        assert "ind_nifty500list.csv" in mock_get.call_args.args[0]

    def test_skips_non_eq_series(self):
        """The CSV may include SME / preference rows (series='SM', 'BL')
        which we must not trade. Only EQ + BE qualify."""
        csv_body = (
            "Company Name,Industry,Symbol,Series,ISIN Code\n"
            + "EquityCo,Sec,EQUITY1,EQ,INE001\n"
            + "BeCo,Sec,BECO1,BE,INE002\n"
            + "SmeCo,Sec,SMECO1,SM,INE003\n"
            + "PrefCo,Sec,PREFCO1,BL,INE004\n"
            + "\n".join(
                f"Eq{i},Sec,EXTRA{i:03d},EQ,INE100{i:03d}"
                for i in range(50)
            )
        )
        resp = MagicMock(status_code=200, content=csv_body.encode(),
                         text=csv_body)
        with patch.object(ss.req, "get", return_value=resp):
            symbols = ss._fetch_nse_archive_csv("NIFTY 500")
        assert "EQUITY1" in symbols
        assert "BECO1" in symbols
        assert "SMECO1" not in symbols  # SM series excluded
        assert "PREFCO1" not in symbols  # BL series excluded

    def test_returns_empty_on_404(self):
        """A 404 must NOT propagate -- caller falls back to live API
        then to NSE_UNIVERSE. This was the failure mode pre-fix:
        DEBUG-level swallow caused silent universe shrinkage."""
        resp = MagicMock(status_code=404, content=b"<html>not found</html>",
                         text="not found")
        with patch.object(ss.req, "get", return_value=resp):
            assert ss._fetch_nse_archive_csv("NIFTY 500") == []

    def test_returns_empty_on_unknown_index(self):
        """We only know archive URLs for NIFTY 100/200/500. Anything
        else returns [] without making a network call (saves a timeout
        wait when callers fat-finger an index name)."""
        with patch.object(ss.req, "get") as mock_get:
            assert ss._fetch_nse_archive_csv("NIFTY 9999") == []
        mock_get.assert_not_called()

    def test_returns_empty_when_csv_is_too_short(self):
        """A CSV with a header but no rows (e.g. NSE returned a stub)
        must return [] so we fall back rather than scanning a 0-stock
        universe."""
        csv_body = "Company Name,Industry,Symbol,Series,ISIN Code\n"
        resp = MagicMock(status_code=200, content=csv_body.encode(),
                         text=csv_body)
        with patch.object(ss.req, "get", return_value=resp):
            assert ss._fetch_nse_archive_csv("NIFTY 500") == []

    def test_swallows_network_exception(self):
        """ConnectionError / ReadTimeout / SSLError must NOT crash the
        scanner -- caller falls back. This is what makes the scanner
        offline-tolerant: no NSE access -> hardcoded universe."""
        with patch.object(ss.req, "get", side_effect=Exception("boom")):
            assert ss._fetch_nse_archive_csv("NIFTY 500") == []


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
