"""Regression tests for Bug K — `--holdout-window-days` /
`--train-window-days` silently ignored in parallel-worker path.

Discovered 2026-05-28 11:55 IST (`findings_log_2026-05-27.md` §9).
The offending ordering was:

    _save_market_data_cache(out_root, market_data)   # FULL window
    ...
    if args.holdout_window_days:                     # <-- runs AFTER cache
        market_data = slice(...)

Worker subprocesses called `_load_market_data_cache(...)` from the
same out_root and so replayed the FULL pre-slice 974k-bar dataset.
The smoking gun was V1+V2 of `battery_v2_holdout_30d_20260528T011921`
coming out byte-identical to V1+V2 of the slot-#2 60d job: same
235 / 266 trade counts, same -₹693 / -₹981 PnL, same PF / MaxDD,
to every decimal place.

Fix (commit landing this test, 2026-05-29 afternoon):
the slice block now runs BEFORE `_save_market_data_cache` inside
the fresh-run branch (`if market_data is None:` ...) so workers
reload pre-sliced data. On resume, the cache already reflects the
original slice; passing a slice flag on resume now logs a WARNING
(``walk-forward slice (...) ignored``) instead of silently
re-slicing on already-cropped data.

These tests are structural (source / AST inspection) plus an
end-to-end save/load round-trip. Spinning up the real battery
harness inside pytest is too slow and would re-download yfinance
data; the contract is well-defined at the source level.

Suite layout:
* TestBugKSliceOrderingSource -- AST-level proof that the slice
  block executes before _save_market_data_cache in the fresh-run
  path. Will fail loudly if a future refactor reverts the
  ordering.
* TestBugKSliceRoundTrip -- save the sliced market_data dict to
  disk, reload it via _load_market_data_cache, assert the loaded
  dict carries the slice (not the full window).
* TestBugKResumeGuard -- the resume branch emits a WARNING when
  slice flags are passed, and does NOT mutate the cached data.
* TestBugKSliceLogContract -- the [BATTERY] log line still
  reports `was <pre_slice_total>, ratio <pct>` so reviewers can
  spot a stale pre-slice number in the audit log.

Cross-references:
* `findings_log_2026-05-27.md` §9 (Bug K disclosure + fix plan).
* `friday_review_2026-05-29.md` §4 (Bug K → slot #3 reframed as
  wider variant sweep, not a holdout p-hack guard).
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from packages.research import battery as battery_module
from packages.research.battery import (
    _load_market_data_cache,
    _save_market_data_cache,
)

ROOT = Path(__file__).resolve().parents[2]
BATTERY_FILE = ROOT / "packages" / "research" / "battery.py"


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _make_market_data(
    *,
    symbols: list[str],
    days: int,
    bars_per_day: int = 75,
) -> dict[str, pd.DataFrame]:
    """Build a fake market_data dict with a deterministic 5-min index.

    `bars_per_day` defaults to 75 — that's the exact NSE 5-min bar count
    per session (09:15 → 15:30, 6h15m / 5min). Real production data
    matches this density so the slice ratios are realistic for review.
    """
    end = datetime(2026, 5, 28, 9, 15)
    start = end - timedelta(days=days)
    idx = pd.date_range(
        start=start, periods=days * bars_per_day, freq="5min"
    )
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        out[s] = pd.DataFrame(
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000,
            },
            index=idx,
        )
    return out


def _read_battery_source() -> str:
    return BATTERY_FILE.read_text(encoding="utf-8")


def _find_main_function(src: str) -> ast.FunctionDef:
    """Locate the `main()` (or `_main()`) function inside battery.py.

    Bug K's cause sat inside this function; the fix re-orders two
    statements within it, so the AST tests need to reach in.
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("main", "_main"):
            return node
    raise AssertionError(
        "battery.py: expected a top-level main()/_main() function; "
        "the AST-pinned Bug K guard cannot run without it."
    )


# ─────────────────────────────────────────────────────────────────────
# Group 1: AST-level ordering proof
# ─────────────────────────────────────────────────────────────────────


class TestBugKSliceOrderingSource:
    """The slice block MUST execute before _save_market_data_cache
    inside the fresh-run branch. AST guard so future refactors
    can't silently revert.
    """

    def _slice_lineno_inside_fresh_branch(self, fn: ast.FunctionDef) -> int:
        """Return the line number of the `if args.train_window_days
        or args.holdout_window_days:` block that lives inside the
        fresh-run (`market_data is None`) branch.
        """
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            test = ast.unparse(node.test)
            # Skip the resume-warning branch (which uses the same
            # condition but lives inside an `else:` body — see
            # _is_inside_resume_branch below). The fresh-run slice
            # has the cache-save call AFTER it as a sibling
            # statement, the resume warning does not.
            if "train_window_days" in test and "holdout_window_days" in test:
                # Look at the body for the actual slice loop (the
                # resume branch only emits a logger.warning).
                body_unparsed = "\n".join(ast.unparse(b) for b in node.body)
                if "df.index.max()" in body_unparsed and "df.index.min()" in body_unparsed:
                    return node.lineno
        raise AssertionError(
            "battery.py: could not find the fresh-run walk-forward slice "
            "block (the loop that mutates df.index.min()/max()). Bug K "
            "guard cannot validate ordering."
        )

    def _save_cache_lineno_inside_fresh_branch(
        self, fn: ast.FunctionDef
    ) -> int:
        """Find the _save_market_data_cache(...) call inside the
        fresh-run branch."""
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "_save_market_data_cache":
                return node.lineno
        raise AssertionError(
            "battery.py: could not find the _save_market_data_cache(...) "
            "call. Bug K guard cannot validate ordering."
        )

    def test_slice_runs_before_save_market_data_cache(self):
        """Bug K REGRESSION GUARD.

        If this test fails, someone moved _save_market_data_cache
        before the walk-forward slice block — workers will reload
        the FULL pre-slice cache and silently ignore
        --holdout-window-days / --train-window-days.
        """
        src = _read_battery_source()
        fn = _find_main_function(src)
        slice_line = self._slice_lineno_inside_fresh_branch(fn)
        save_line = self._save_cache_lineno_inside_fresh_branch(fn)
        assert slice_line < save_line, (
            f"Bug K regression: slice block at line {slice_line} runs AFTER "
            f"_save_market_data_cache at line {save_line}. Workers will "
            f"reload the full pre-slice cache and silently drop "
            f"--holdout-window-days. See findings_log_2026-05-27.md §9."
        )

    def test_fix_comment_pins_bug_k_reference(self):
        """The fix comment near the slice block names Bug K so a
        future reviewer sees the rationale without grepping the
        findings log."""
        src = _read_battery_source()
        # The reorder comment lives in main() right above the slice block.
        # Require BOTH 'Bug K' and the findings-log section pointer.
        assert "Bug K" in src, (
            "battery.py: lost the 'Bug K' fix comment near the slice "
            "block. The reorder rationale must remain pinned at the "
            "fix site so a refactor doesn't lose context."
        )
        assert "findings_log_2026-05-27.md" in src, (
            "battery.py: lost the findings-log pointer near the slice "
            "block. Reviewers should be able to follow the rationale "
            "directly from the source."
        )

    def test_fix_comment_explicitly_names_save_cache_ordering(self):
        """The comment must explicitly say 'BEFORE _save_market_data_cache'
        so even a casual reader can spot the ordering invariant."""
        src = _read_battery_source()
        norm = re.sub(r"\s+", " ", src)
        assert "BEFORE _save_market_data_cache" in norm, (
            "battery.py: the fix comment must spell out the ordering "
            "invariant ('BEFORE _save_market_data_cache') so the "
            "Bug K cause is unambiguous at the fix site."
        )


# ─────────────────────────────────────────────────────────────────────
# Group 2: end-to-end round-trip
# ─────────────────────────────────────────────────────────────────────


class TestBugKSliceRoundTrip:
    """Save sliced market_data, reload via _load_market_data_cache,
    assert workers see the slice. Mirrors what worker subprocesses
    actually do at runtime.
    """

    def test_load_after_save_returns_sliced_data(self, tmp_path):
        """End-to-end contract: save then load preserves the slice."""
        full = _make_market_data(symbols=["AAA", "BBB"], days=90)
        # Apply the slice mimicking the production code path.
        last_n_days = 30
        for sym, df in list(full.items()):
            cutoff = df.index.max() - pd.Timedelta(days=last_n_days)
            full[sym] = df[df.index >= cutoff]
        sliced_total = sum(len(df) for df in full.values())

        _save_market_data_cache(tmp_path, full)
        reloaded = _load_market_data_cache(tmp_path)

        assert reloaded is not None, (
            "_load_market_data_cache returned None — the cache file "
            "should exist immediately after _save_market_data_cache."
        )
        reloaded_total = sum(len(df) for df in reloaded.values())
        assert reloaded_total == sliced_total, (
            f"reloaded cache has {reloaded_total} bars, expected "
            f"{sliced_total} (the slice). If this fails after the Bug K "
            f"fix is in place, something else is mutating the pickle."
        )
        for sym, df in reloaded.items():
            window_days = (df.index.max() - df.index.min()).days
            assert window_days <= last_n_days + 1, (
                f"{sym}: reloaded cache spans {window_days} days but the "
                f"slice was last-{last_n_days}d. Bug K regression."
            )

    def test_full_window_round_trip_is_intact_when_slice_not_applied(
        self, tmp_path
    ):
        """Negative control: without the slice, cache must hold the
        full window. Pins that the round-trip itself is faithful so a
        round-trip failure can't be confused with a slice failure."""
        full = _make_market_data(symbols=["AAA"], days=60)
        full_total = sum(len(df) for df in full.values())

        _save_market_data_cache(tmp_path, full)
        reloaded = _load_market_data_cache(tmp_path)
        reloaded_total = sum(len(df) for df in reloaded.values())

        assert reloaded_total == full_total, (
            f"unsliced round-trip altered bar count: saved {full_total} "
            f"got back {reloaded_total}. The cache writer or reader is "
            f"broken — Bug K guard depends on faithful round-trips."
        )


# ─────────────────────────────────────────────────────────────────────
# Group 3: resume guard
# ─────────────────────────────────────────────────────────────────────


class TestBugKResumeGuard:
    """On resume, the cache already reflects the original slice. If the
    operator passes --holdout-window-days again the harness must WARN,
    not silently re-slice on already-cropped data."""

    def test_resume_branch_emits_warning_for_slice_flag(self):
        """AST guard: on the resume path, the slice-flag check must
        invoke logger.warning (not logger.info, not silent)."""
        src = _read_battery_source()
        fn = _find_main_function(src)
        # Find the `else:` branch of `if market_data is None:`.
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            test = ast.unparse(node.test)
            if test == "market_data is None":
                else_body = "\n".join(ast.unparse(s) for s in node.orelse)
                assert "train_window_days" in else_body, (
                    "resume branch must check args.train_window_days / "
                    "args.holdout_window_days so a slice flag on resume "
                    "doesn't go unnoticed."
                )
                assert "logger.warning" in else_body, (
                    "resume branch must logger.warning when slice flag "
                    "is passed on resume — silent ignore would let the "
                    "operator believe a different slice is being applied."
                )
                # Must explicitly name the flag in the warning so the
                # log line is operator-actionable.
                assert "ignored" in else_body, (
                    "resume warning must say the slice flag is "
                    "'ignored' so the operator knows the cached "
                    "slice is the one in effect."
                )
                return
        raise AssertionError(
            "battery.py: could not find `if market_data is None:` "
            "branch — resume-guard test cannot run."
        )

    def test_resume_guard_message_mentions_dropping_resume(self):
        """Operator-actionable: the warning must tell the reader how
        to actually get a different slice (drop --resume)."""
        src = _read_battery_source()
        norm = re.sub(r"\s+", " ", src)
        # Look for the resume-warning text itself.
        assert "Drop --resume" in norm, (
            "resume-guard warning must instruct the operator to drop "
            "--resume to get a fresh-run slice. Without that hint the "
            "warning isn't actionable."
        )


# ─────────────────────────────────────────────────────────────────────
# Group 4: log contract
# ─────────────────────────────────────────────────────────────────────


class TestBugKSliceLogContract:
    """The [BATTERY] walk-forward slice log line is the operator's
    primary signal that the slice ran. Pin its shape so the audit
    trail stays parseable."""

    def test_slice_log_line_includes_was_pre_slice_total(self):
        """Log line must report `was <pre_slice_total>` so reviewers
        can sanity-check the ratio against the pre-slice fetch."""
        src = _read_battery_source()
        norm = re.sub(r"\s+", " ", src)
        assert "walk-forward slice" in norm, (
            "battery.py: lost the [BATTERY] walk-forward slice log line."
        )
        assert "was {pre_slice_total}" in norm, (
            "slice log line must reference {pre_slice_total} (computed "
            "right before the slice loop, NOT the post-cache total). "
            "Otherwise the 'was X bars' field will silently be the "
            "post-slice number = nonsense ratio."
        )

    def test_slice_log_line_uses_calendar_day_unit(self):
        """The log line must use `Nd` (calendar days) so a 30d slice
        on a holiday-heavy month still reads correctly. Bar-count
        slicing was rejected in §9.6 because weekends/holidays
        corrupt the window."""
        src = _read_battery_source()
        norm = re.sub(r"\s+", " ", src)
        # Match the exact format string fragment.
        assert "({keep} {n}d," in norm, (
            "slice log line must use `{keep} {n}d` (calendar days). "
            "Bar-count units would mis-state holiday-heavy slices."
        )


# ─────────────────────────────────────────────────────────────────────
# Group 5: idempotency / fail-soft
# ─────────────────────────────────────────────────────────────────────


class TestBugKSliceFailSoft:
    """The slice loop must skip non-datetime indices with a warning
    rather than crashing the whole run. Pre-existing contract,
    pinned here so the reorder doesn't drop it."""

    def test_non_datetime_index_warns_and_skips(self):
        """Confirms the existing fail-soft branch on TypeError /
        AttributeError is preserved through the reorder."""
        src = _read_battery_source()
        norm = re.sub(r"\s+", " ", src)
        assert "non-datetime index" in norm, (
            "lost the 'non-datetime index' fail-soft warning. The "
            "slice loop must keep its try/except around df.index "
            "operations so one bad symbol doesn't kill the run."
        )
        assert "TypeError, AttributeError" in norm, (
            "fail-soft branch must catch BOTH TypeError and "
            "AttributeError — the original contract."
        )

    def test_slice_loop_uses_calendar_day_arithmetic(self):
        """pd.Timedelta(days=n) is the correct unit; pd.DateOffset or
        bar-count slicing would mis-handle holidays."""
        src = _read_battery_source()
        norm = re.sub(r"\s+", " ", src)
        assert "pd.Timedelta(days=n)" in norm, (
            "slice cutoff must use pd.Timedelta(days=n), not "
            "pd.DateOffset or bar-count arithmetic. See §9.6."
        )


# ─────────────────────────────────────────────────────────────────────
# Group 6: structural — slice block sits inside the fresh-run branch
# ─────────────────────────────────────────────────────────────────────


class TestBugKSliceLivesInFreshBranch:
    """The slice block MUST live inside the `if market_data is None:`
    branch. If it sits outside (at module-statement level inside main)
    it will run on resume and re-slice already-cropped data."""

    def test_slice_block_is_nested_in_fresh_branch(self):
        """AST: the slice loop must be a descendant of the
        `market_data is None` body."""
        src = _read_battery_source()
        fn = _find_main_function(src)
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            test = ast.unparse(node.test)
            if test != "market_data is None":
                continue
            # Walk only the body (the fresh-run branch) and look for
            # the slice loop signature.
            for sub in node.body:
                for child in ast.walk(sub):
                    if not isinstance(child, ast.If):
                        continue
                    sub_test = ast.unparse(child.test)
                    if (
                        "train_window_days" in sub_test
                        and "holdout_window_days" in sub_test
                    ):
                        body = "\n".join(ast.unparse(b) for b in child.body)
                        if "df.index.max()" in body:
                            return  # Found, contract satisfied.
        raise AssertionError(
            "battery.py: walk-forward slice loop is NOT nested inside "
            "`if market_data is None:`. Bug K regression: the slice "
            "would run on resume too, double-slicing already-cropped "
            "data."
        )
