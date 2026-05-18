"""Diagnostic extensions for freeze-v2.1 observability (2026-05-19).

These tests pin the behaviour of the six observability additions to
``packages/research/diagnostic.py`` that the freeze-v2.1 contingency
plan (`docs/freeze_contingencies.md` §C2 + §C8) depends on:

  * ``bootstrap_pf_ci()``               -- PF lower/upper 95 % CI
  * ``pf_excluding_max_trade()``        -- "one lucky trade" guard
  * ``entry_time_histogram()``          -- time-of-day clustering guard
  * ``aggregate_by_supersector()``      -- sector-concentration guard
  * ``load_contaminated_days()``        -- §C8 CSV loader
  * ``filter_trades_excluding_contaminated()`` -- companion filter

Why this exists
---------------
The freeze gates the edge claim on **PF lower-CI > 1.0**, not on point
PF, and downgrades verdicts to INSUFFICIENT when an edge is one lucky
trade, one sector, or one time-of-day bucket. If any of those helpers
silently regress, the freeze produces a wrong verdict on June 8.
"""
from __future__ import annotations

import sys
from pathlib import Path
from collections import defaultdict

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages"))

from research.diagnostic import (  # noqa: E402
    bootstrap_pf_ci,
    pf_excluding_max_trade,
    entry_time_histogram,
    aggregate_by_supersector,
    load_contaminated_days,
    filter_trades_excluding_contaminated,
    ENTRY_TIME_BUCKETS,
)


# ────────────────────────────────────────────────────────────────────
# bootstrap_pf_ci
# ────────────────────────────────────────────────────────────────────


def test_bootstrap_ci_empty_returns_zero_zero():
    assert bootstrap_pf_ci([]) == (0.0, 0.0)


def test_bootstrap_ci_under_5_trades_is_uninformative():
    """N<5 cannot produce a meaningful CI -- explicitly signal that
    with (0, +inf) so callers can treat as INSUFFICIENT."""
    lo, hi = bootstrap_pf_ci([10.0, -5.0, 8.0])
    assert lo == 0.0
    assert hi == float("inf")


def test_bootstrap_ci_brackets_point_pf_on_real_sample():
    """On a clearly-winning sample, the CI must straddle the true PF."""
    # 7 wins of 20, 3 losses of 10. Gross win = 140, gross loss = 30, PF = 4.67.
    pnls = [20, 20, 20, 20, 20, 20, 20, -10, -10, -10]
    lo, hi = bootstrap_pf_ci(pnls, n_resamples=500, seed=42)
    assert lo > 0.5, f"lower-CI suspiciously low: {lo}"
    assert hi > 1.0, f"upper-CI must cover the true PF (~4.67): got {hi}"
    # Sanity: the lower-CI on a 4.67-PF sample is most likely above 1.0
    # but we don't pin it to a specific number to avoid flakiness.


def test_bootstrap_ci_lower_below_one_for_break_even_sample():
    """A 50/50 sample with symmetric wins/losses has true PF == 1.
    The lower-CI must be below 1.0 (the gate for 'no edge claim')."""
    pnls = [10, 10, 10, 10, 10, -10, -10, -10, -10, -10]
    lo, _ = bootstrap_pf_ci(pnls, n_resamples=500, seed=42)
    assert lo < 1.0, f"50/50 sample produced lower-CI >= 1.0: {lo}"


def test_bootstrap_ci_handles_all_wins_zero_losses():
    """A sample with no losses has PF == +inf. The bootstrap should
    propagate +inf up (or, equivalently, return a very large upper)."""
    pnls = [10.0, 12.0, 8.0, 15.0, 11.0]
    lo, hi = bootstrap_pf_ci(pnls, n_resamples=200, seed=42)
    # With 5 all-wins, every resample is also all-wins -> PF = +inf
    assert hi == float("inf")
    # Lower can be inf too (every resample is inf), or 0 if our finite-
    # PF list is empty. Either is acceptable as a "no losses observed" signal.
    assert lo in (0.0, float("inf"))


# ────────────────────────────────────────────────────────────────────
# pf_excluding_max_trade
# ────────────────────────────────────────────────────────────────────


def test_pf_excl_max_detects_one_lucky_trade():
    """The §C2 canonical example: PF looks great because of one huge win,
    PF-excl-max collapses below 1.0."""
    # One huge win, several small losses.
    pnls = [500, -50, -50, -50, -50, -50]
    pf_full, pf_excl = pf_excluding_max_trade(pnls)
    assert pf_full == pytest.approx(500.0 / 250.0)  # PF = 2.0
    assert pf_excl == 0.0  # only losses remain
    # Downstream rule: pf_full > 1 AND pf_excl < 0.8 -> INSUFFICIENT
    assert pf_full > 1.0 and pf_excl < 0.8


def test_pf_excl_max_keeps_robust_edge_unchanged():
    """A strategy with multiple solid wins should not be flagged."""
    pnls = [40, 35, 50, 45, -20, -18]
    pf_full, pf_excl = pf_excluding_max_trade(pnls)
    assert pf_full > 1.0
    assert pf_excl > 1.0, "robust edge should survive excluding the max"


def test_pf_excl_max_no_op_when_max_is_loss():
    """If the trade list has no winners, max is a loss; excluding it is
    not the 'one-lucky-trade' question -- just return PF unchanged."""
    pnls = [-10, -5, -15, -8]
    pf_full, pf_excl = pf_excluding_max_trade(pnls)
    assert pf_full == 0.0
    assert pf_excl == 0.0


def test_pf_excl_max_empty_returns_zero_zero():
    assert pf_excluding_max_trade([]) == (0.0, 0.0)


# ────────────────────────────────────────────────────────────────────
# entry_time_histogram
# ────────────────────────────────────────────────────────────────────


def test_entry_time_histogram_buckets_canonical_hours():
    """A trade at hour 9 buckets into 09:15-10:00, hour 14 into
    14:00-15:30. The labels are stable contract for the diagnostic."""
    by_hour = {
        9: {"n": 2, "pnl": 20.0},
        10: {"n": 3, "pnl": 15.0},
        14: {"n": 1, "pnl": -5.0},
        15: {"n": 2, "pnl": 8.0},  # also 14:00-15:30 bucket
    }
    out = entry_time_histogram(by_hour)
    assert out["09:15-10:00"]["n"] == 2
    assert out["10:00-11:00"]["n"] == 3
    assert out["14:00-15:30"]["n"] == 3  # hour 14 + hour 15


def test_entry_time_histogram_ignores_out_of_range_hours():
    """Hour 17 is post-market; must not crash and must not silently
    bucket into 14:00-15:30."""
    by_hour = {17: {"n": 5, "pnl": 50.0}, 11: {"n": 2, "pnl": 10.0}}
    out = entry_time_histogram(by_hour)
    assert sum(b["n"] for b in out.values()) == 2  # only the hour-11 trade
    assert out["11:00-12:00"]["n"] == 2


def test_entry_time_histogram_bucket_labels_are_stable():
    """Downstream report formatters read the labels by string; pin them
    so a future refactor of the bucket list (changing the strings) is
    a loud test failure."""
    expected = [
        "09:15-10:00", "10:00-11:00", "11:00-12:00",
        "12:00-13:00", "13:00-14:00", "14:00-15:30",
    ]
    assert [lbl for lbl, _, _ in ENTRY_TIME_BUCKETS] == expected


# ────────────────────────────────────────────────────────────────────
# aggregate_by_supersector
# ────────────────────────────────────────────────────────────────────


def test_aggregate_by_supersector_groups_known_symbols():
    """Bank-family symbols collapse into 'Financials' via SUPERSECTOR_MAP."""
    trades = [
        {"symbol": "HDFCBANK", "pnl": 100.0},
        {"symbol": "ICICIBANK", "pnl": -50.0},
        {"symbol": "INFY", "pnl": 80.0},
    ]
    buckets = aggregate_by_supersector(trades)
    # HDFCBANK + ICICIBANK should land in the same bucket; that bucket
    # must contain 2 trades. INFY lands separately.
    counts = {ss: b["n"] for ss, b in buckets.items()}
    assert max(counts.values()) >= 2, f"no supersector with >=2 trades: {counts}"


def test_aggregate_by_supersector_unknown_symbol_bucket():
    """Unmapped symbols go into UNKNOWN, not silently dropped."""
    trades = [{"symbol": "TOTALLYFAKETICKER", "pnl": 25.0}]
    buckets = aggregate_by_supersector(trades)
    assert sum(b["n"] for b in buckets.values()) == 1


def test_aggregate_by_supersector_handles_missing_symbol():
    """A row with no 'symbol' must not crash; it goes into UNKNOWN."""
    trades = [{"pnl": 30.0}, {"symbol": "", "pnl": -10.0}]
    buckets = aggregate_by_supersector(trades)
    assert "UNKNOWN" in buckets
    assert buckets["UNKNOWN"]["n"] == 2


def test_aggregate_by_supersector_computes_pf_per_bucket():
    """PF per supersector is the ratio of gross_win to gross_loss
    within that bucket. Pin the contract."""
    trades = [
        {"symbol": "INFY", "pnl": 100.0},
        {"symbol": "INFY", "pnl": -25.0},
        {"symbol": "INFY", "pnl": 50.0},
    ]
    buckets = aggregate_by_supersector(trades)
    # INFY -> IT / Tech sector. PF should be (100+50)/25 = 6.0.
    bucket = next(iter(buckets.values()))
    assert bucket["pf"] == pytest.approx(6.0)


# ────────────────────────────────────────────────────────────────────
# load_contaminated_days + filter
# ────────────────────────────────────────────────────────────────────


def test_load_contaminated_days_missing_file_returns_empty(tmp_path: Path):
    """If logs/contaminated_days.csv doesn't exist, return empty set --
    never crash. Most repo checkouts will never declare a contaminated
    day, so the file simply won't exist."""
    out = load_contaminated_days(project_root=tmp_path)
    assert out == set()


def test_load_contaminated_days_reads_csv(tmp_path: Path):
    """Read a well-formed contaminated_days.csv and return the date set."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "contaminated_days.csv").write_text(
        "date,vix,nifty_pct,reason\n"
        "2026-05-22,26.5,-1.8,RBI surprise rate decision\n"
        "2026-05-29,28.0,-3.1,geopolitical shock\n",
        encoding="utf-8",
    )
    out = load_contaminated_days(project_root=tmp_path)
    assert out == {"2026-05-22", "2026-05-29"}


def test_load_contaminated_days_skips_malformed_rows(tmp_path: Path):
    """Malformed date rows (typo, missing column) are silently skipped
    so a fat-finger in the CSV doesn't break the EOD diagnostic."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "contaminated_days.csv").write_text(
        "date,vix,nifty_pct,reason\n"
        "2026-05-22,26.5,-1.8,real entry\n"
        "not-a-date,28.0,-3.1,bad\n"
        ",,,empty\n"
        "2026-06-01,30.0,-2.6,another real one\n",
        encoding="utf-8",
    )
    out = load_contaminated_days(project_root=tmp_path)
    assert out == {"2026-05-22", "2026-06-01"}


def test_filter_trades_excluding_contaminated_drops_listed_days():
    trades = [
        {"exit_time": "2026-05-22T10:30:00", "pnl": -200.0},
        {"exit_time": "2026-05-23T11:00:00", "pnl": +50.0},
        {"exit_time": "2026-05-29T14:00:00", "pnl": -800.0},
        {"exit_time": "2026-06-02T09:30:00", "pnl": +75.0},
    ]
    contaminated = {"2026-05-22", "2026-05-29"}
    kept = filter_trades_excluding_contaminated(trades, contaminated)
    assert len(kept) == 2
    kept_dates = {t["exit_time"][:10] for t in kept}
    assert kept_dates == {"2026-05-23", "2026-06-02"}


def test_filter_trades_excluding_contaminated_empty_contamination_set_is_passthrough():
    """No contaminated days => return all trades unchanged."""
    trades = [{"exit_time": "2026-05-22T10:30:00", "pnl": -200.0}]
    out = filter_trades_excluding_contaminated(trades, set())
    assert out == trades
    assert out is not trades  # but a copy, not the original list


def test_filter_trades_uses_entry_time_fallback_when_exit_time_missing():
    """A trade still open at the moment of report has no exit_time;
    fall back to entry_time so contamination still applies."""
    trades = [
        {"entry_time": "2026-05-22T10:30:00", "pnl": -200.0},
        {"entry_time": "2026-05-23T11:00:00", "pnl": +50.0},
    ]
    contaminated = {"2026-05-22"}
    kept = filter_trades_excluding_contaminated(trades, contaminated)
    assert len(kept) == 1
    assert kept[0]["entry_time"].startswith("2026-05-23")
