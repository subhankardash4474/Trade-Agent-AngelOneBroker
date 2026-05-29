"""Retrain pre-flight regression suite — Friday 2026-05-29.

Pins the 7 known training-pipeline bug fixes that produced the
broken 2026-05-14 .pkl when they were absent or partially absent
from the panic-patch deploy. The forensic audit
(`docs/findings/findings_log_2026-05-27.md` §5) catalogued these explicitly;
the next retrain MUST inherit all seven, otherwise we ship a model
with the same training-time leakage that took XGBoost from useful
to "95% SELL on validation" between 2026-05-11 and 2026-05-19.

Bug fixes pinned (all source-level, AST + text searches):

| Fix    | What                                                | File                                |
|--------|-----------------------------------------------------|-------------------------------------|
| F-22   | Early-stop validation slice carved from X_train,    | packages/training/train_xgboost.py  |
|        | NOT from X_test (selection-bias leakage closed).    |                                     |
| F-24   | Daily Nifty/VIX context shifted by +1 trading day   | packages/training/prepare_dataset.py |
|        | so intraday 09:30 bars get YESTERDAY's daily close, |                                     |
|        | matching live `_market_context` ffill.              |                                     |
| F-70   | Time-based-split exception path FAILS HARD instead  | packages/training/prepare_dataset.py |
|        | of silently falling back to row-index split, which  |                                     |
|        | re-introduces P1 #7 cross-symbol calendar leakage.  |                                     |
| C-23   | Calibrator fit and Brier eval use DIFFERENT halves  | packages/training/train_xgboost.py  |
|        | of X_test (chronological 50/50). Pre-fix both ran   |                                     |
|        | on full X_test → in-sample Brier reported.          |                                     |
| P1 #7  | Train/test split partitions by CALENDAR TIMESTAMP,  | packages/training/prepare_dataset.py |
|        | not by row index. Old `.iloc` split mixed same-date |                                     |
|        | bars across symbols, inflating ROC-AUC.             |                                     |
| P1 #8  | nifty_trend default = 0 (neutral / sideways) on     | packages/training/prepare_dataset.py |
|        | both market-context fetch and per-symbol injection. |                                     |
|        | Old default 1 (bull) silently licensed bear trades. |                                     |
| P1 #9  | --interval != 5m is a HARD BLOCK unless             | packages/training/prepare_dataset.py |
|        | --allow-distribution-skew is passed. Old WARNING-   |                                     |
|        | only path produced deployable artefacts that mis-   |                                     |
|        | aligned tod_/dow_ distributions vs serve.           |                                     |

These are structural source pins — they fail if the fix is
removed, weakened, or refactored to a form that loses the named
contract. They do NOT validate runtime correctness (that's the
smoke test in step E + the §5.9 step 3 held-out validation
during the actual training run).

Cross-references:
* `docs/findings/findings_log_2026-05-27.md` §5 (forensic audit) and §5.9
  (retrain runbook) and §5.10 (pre-flight checklist).
* `packages/training/prepare_dataset.py` — source of P1 #7 / P1 #8 /
  P1 #9 / F-24 / F-70 fixes.
* `packages/training/train_xgboost.py` — source of F-22 / C-23 fixes.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PREPARE = ROOT / "packages" / "training" / "prepare_dataset.py"
TRAIN = ROOT / "packages" / "training" / "train_xgboost.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _norm(s: str) -> str:
    """Collapse whitespace so multi-line code reads as a single string
    for substring matching. Preserves token boundaries."""
    return re.sub(r"\s+", " ", s)


# ─────────────────────────────────────────────────────────────────────
# F-24 — daily-context lookahead shift
# ─────────────────────────────────────────────────────────────────────


class TestF24LookaheadShift:
    """The daily Nifty/VIX context comes from end-of-day closes. Tagging
    a 09:30 intraday bar with TODAY's daily close is a lookahead. F-24
    shifts the lookup by +1 trading day so today's 09:30 gets
    yesterday's closed daily, matching live `_market_context`."""

    def test_ctx_shifted_by_one_day_in_prepare_dataset(self):
        src = _read(PREPARE)
        norm = _norm(src)
        assert "ctx_shifted = market_ctx.copy()" in norm, (
            "F-24: lost the `ctx_shifted = market_ctx.copy()` defensive "
            "copy in prepare_dataset.py. Without it the in-place index "
            "mutation below would corrupt the original market_ctx for "
            "subsequent symbols."
        )
        assert (
            "ctx_shifted.index = ctx_shifted.index + pd.Timedelta(days=1)"
            in norm
        ), (
            "F-24: lost the `+ pd.Timedelta(days=1)` shift in "
            "prepare_dataset.py. Without it the daily Nifty/VIX context "
            "tagged on a 09:30 intraday bar uses TODAY's close — a "
            "lookahead that the live `_market_context` ffill never "
            "produces."
        )

    def test_f24_comment_anchors_fix_at_the_site(self):
        """A reviewer reading the line should see the F-24 reference
        without grepping the findings log."""
        src = _read(PREPARE)
        # The fix comment block must contain F-24 within ~30 lines of
        # the shift line.
        idx = src.find(
            "ctx_shifted.index = ctx_shifted.index + pd.Timedelta(days=1)"
        )
        assert idx >= 0
        window = src[max(0, idx - 1500) : idx]
        assert "F-24" in window, (
            "F-24: lost the `F-24` comment anchor near the shift line. "
            "Future readers won't know the rationale; pin the comment."
        )


# ─────────────────────────────────────────────────────────────────────
# P1 #8 — neutral nifty_trend default
# ─────────────────────────────────────────────────────────────────────


class TestP1NeutralNiftyTrendDefault:
    """The live FeatureEngine fills missing nifty_trend with 0 (neutral
    / sideways). Training MUST use the same sentinel — otherwise the
    model learns a non-existent "bull" signal that the serve path
    never produces."""

    def test_market_context_fetch_uses_neutral_default(self):
        src = _read(PREPARE)
        norm = _norm(src)
        # The market-context fetch path: if nifty_trend column is
        # missing, fall back to 0; .ffill().fillna(0) for the gap fill.
        assert 'ctx["nifty_trend"] = 0' in norm, (
            "P1 #8: lost the `ctx['nifty_trend'] = 0` default in "
            "fetch_market_context. Old default 1 silently licensed "
            "bear-regime trades by tagging missing days as bull."
        )
        assert (
            'ctx["nifty_trend"].ffill().fillna(0).astype(int)' in norm
            or 'ctx["nifty_trend"]' in norm
            and ".ffill()" in norm
            and ".fillna(0)" in norm
        ), (
            "P1 #8: lost the `.ffill().fillna(0)` post-fetch fill in "
            "market context. Gaps must default to 0 (neutral)."
        )

    def test_per_symbol_injection_uses_neutral_default(self):
        src = _read(PREPARE)
        norm = _norm(src)
        # Per-symbol injection path: reindex().ffill() on ctx_shifted,
        # then .fillna(0) so a symbol missing today's regime defaults
        # to neutral.
        assert 'featured["nifty_trend"] = ctx_shifted["nifty_trend"]' in norm, (
            "P1 #8: lost the per-symbol nifty_trend injection from "
            "ctx_shifted. The fix relies on this; without it the "
            "symbol's intraday rows miss the regime feature entirely."
        )
        # Must use .fillna(0) on the per-symbol injection path so a
        # missing-day default lands on neutral.
        # We look in a wide window around the injection.
        idx = norm.find('featured["nifty_trend"] = ctx_shifted["nifty_trend"]')
        assert idx >= 0
        injection_block = norm[idx : idx + 400]
        assert ".fillna(0)" in injection_block, (
            "P1 #8: lost `.fillna(0)` on the per-symbol nifty_trend "
            "injection. Symbol rows missing today's daily context will "
            "drift back to NaN → some downstream step will impute a "
            "non-zero default and re-introduce the regime skew."
        )

    def test_no_bull_default_anywhere(self):
        """Hard guard: nifty_trend default must NEVER be 1. A regression
        of P1 #8 would re-introduce the bias."""
        src = _read(PREPARE)
        # Look for the literal `nifty_trend"] = 1` or
        # `nifty_trend") = 1` style. No such default may exist.
        for hit in re.finditer(r'nifty_trend["\']\s*\]?\s*=\s*1\b', src):
            ctx = src[max(0, hit.start() - 80) : hit.end() + 80]
            # Allow `astype(int).replace({0: -1, 1: 1})` (intermediate
            # transformation, not a default). Reject everything else.
            if "replace" not in ctx:
                pytest.fail(
                    f"P1 #8 regression: found `nifty_trend = 1` default at "
                    f"offset {hit.start()}:\n{ctx}\n"
                    f"Default MUST be 0 (neutral). Old `1` silently "
                    f"licensed bear-regime trades."
                )


# ─────────────────────────────────────────────────────────────────────
# P1 #7 — calendar-time train/test split
# ─────────────────────────────────────────────────────────────────────


class TestP1CalendarTimeSplit:
    """Train/test split MUST partition by CALENDAR TIMESTAMP, not by
    row index. Row-index split (`combined.iloc[:0.8*N]`) after concat
    mixes same-date bars across symbols — the model learns "what other
    symbols did on the same date" → inflated ROC-AUC."""

    def test_split_uses_timestamp_cutoff(self):
        src = _read(PREPARE)
        norm = _norm(src)
        assert "Time-based split cutoff:" in norm, (
            "P1 #7: lost the `Time-based split cutoff:` log line. The "
            "split logic was likely refactored to row-index — verify "
            "and revert."
        )
        # The actual logic: sort by index, take 80th-percentile timestamp.
        assert "sort_index()" in norm, (
            "P1 #7: lost `sort_index()` in the split block. Without a "
            "sorted index the 80th-percentile cutoff is meaningless."
        )
        assert (
            "cutoff_idx = int(len(timestamps) * 0.8)" in norm
            or "int(len(timestamps) * 0.8)" in norm
        ), (
            "P1 #7: lost the `int(len(timestamps) * 0.8)` cutoff index "
            "computation. The split must use the 80th-percentile "
            "timestamp, not 80% of rows."
        )

    def test_no_iloc_row_index_split(self):
        """P1 #7 regression guard: the legacy `combined.iloc[:0.8*N]`
        pattern must NOT appear in the active train/test partition.

        We allow the degenerate single-row branch to use `.iloc`
        because that's the documented fallback for a non-datetime index
        (no timestamps → row split is the only option). Everywhere else
        the split must be timestamp-based.
        """
        src = _read(PREPARE)
        # Find every iloc[...split style] pattern.
        # The safe pattern is `train = sorted_idx[sorted_idx.index < cutoff]`
        # (timestamp slicing). Reject `combined.iloc[:0.8*N]`-style code.
        bad = re.findall(
            r"combined\.iloc\[\s*:\s*int\s*\(\s*len\s*\(\s*combined\s*\)\s*\*\s*0\.8\s*\)\s*\]",
            src,
        )
        assert not bad, (
            f"P1 #7 regression: `combined.iloc[:int(len(combined)*0.8)]` "
            f"found in prepare_dataset.py. This is the leaky row-index "
            f"split that mixes same-date bars across symbols. Replace "
            f"with the timestamp-cutoff split."
        )


# ─────────────────────────────────────────────────────────────────────
# F-70 — fail-hard on time-split exception
# ─────────────────────────────────────────────────────────────────────


class TestF70RefuseFallbackToRowSplit:
    """If the timestamp-based split fails (e.g. tz-inconsistent index),
    the OLD code silently fell back to row-index split — which is
    EXACTLY the leakage P1 #7 was supposed to eliminate. F-70 forces
    a RuntimeError so the operator fixes the index instead of
    shipping a leaky model."""

    def test_time_split_failure_raises_runtime_error(self):
        src = _read(PREPARE)
        norm = _norm(src)
        assert "Time-based split failed" in norm, (
            "F-70: lost the `Time-based split failed` RuntimeError "
            "message. A bare `except: pass` would re-introduce the "
            "silent fallback that P1 #7 closed."
        )
        assert "Refusing to silently fall back to row-index split" in norm, (
            "F-70: lost the explicit refusal language in the "
            "RuntimeError. Future contributors must read this and "
            "understand why the exception isn't swallowed."
        )
        # Look for the actual `raise RuntimeError(`.
        assert "raise RuntimeError(" in norm, (
            "F-70: time-split exception path must `raise RuntimeError`, "
            "not log+continue."
        )

    def test_f70_anchor_in_comment(self):
        """The fix comment must name F-70 + reference the audit so a
        reviewer can chase the rationale."""
        src = _read(PREPARE)
        assert "F-70" in src, (
            "F-70: lost the `F-70` comment anchor in prepare_dataset.py."
        )


# ─────────────────────────────────────────────────────────────────────
# P1 #9 — train/serve interval consistency
# ─────────────────────────────────────────────────────────────────────


class TestP1IntervalSkewHardBlock:
    """A non-canonical `--interval` (anything other than `5m`, the live
    FeatureEngine emit interval) used to be a WARNING-only path — the
    resulting models were deployable, several were eventually
    deployed, and the tod_*/dow_* features carried the train/serve
    distribution skew through to live. P1 #9 hard-blocks unless
    --allow-distribution-skew is passed, and drops tod_*/dow_*
    columns when the override is active."""

    def test_serve_interval_is_5m(self):
        src = _read(PREPARE)
        assert 'SERVE_INTERVAL = "5m"' in src, (
            "P1 #9: lost the `SERVE_INTERVAL = \"5m\"` constant. Live "
            "FeatureEngine emits 5m; training MUST default to 5m to "
            "match. If serve interval ever changes, update this and "
            "the tick_aggregator base candle in lockstep."
        )

    def test_interval_mismatch_without_override_exits(self):
        src = _read(PREPARE)
        norm = _norm(src)
        assert "args.interval != SERVE_INTERVAL:" in norm, (
            "P1 #9: lost the `args.interval != SERVE_INTERVAL` check "
            "before dataset production."
        )
        # In the no-override branch, the script must call sys.exit(2)
        # rather than continue with a WARNING.
        assert "if not args.allow_distribution_skew:" in norm, (
            "P1 #9: lost the `--allow-distribution-skew` opt-in branch."
        )
        # The non-override path must terminate.
        assert "sys.exit(2)" in norm, (
            "P1 #9: lost the `sys.exit(2)` on interval mismatch without "
            "override. WARNING-only path was the dominant failure mode "
            "for the broken pkl."
        )

    def test_skew_features_dropped_when_override_active(self):
        src = _read(PREPARE)
        norm = _norm(src)
        assert "drop_skew_features" in norm, (
            "P1 #9: lost the `drop_skew_features` plumbing. With the "
            "override active, the produced dataset MUST drop tod_*/dow_* "
            "columns so a deployed-by-mistake model can't carry the "
            "skew through to serve."
        )
        assert (
            'c.startswith("tod_")' in norm and 'c.startswith("dow_")' in norm
        ), (
            "P1 #9: skew-column drop must filter on `tod_` AND `dow_` "
            "prefixes (cyclic time-of-day + day-of-week features)."
        )


# ─────────────────────────────────────────────────────────────────────
# F-22 — early-stopping validation slice from X_train
# ─────────────────────────────────────────────────────────────────────


class TestF22EarlyStopValidationFromTrain:
    """Early stopping previously evaluated on X_test, then we reported
    held-out metrics on the same X_test — the test set was effectively
    used to pick `best_iteration`, so the metrics were optimistic
    (selection-bias leakage). F-22 carves a chronological tail slice
    of X_train as the early-stopping validation set; X_test stays
    untouched."""

    def test_eval_set_uses_X_val_not_X_test(self):
        src = _read(TRAIN)
        norm = _norm(src)
        assert "X_val = X_train.iloc[n_train - n_val :]" in norm, (
            "F-22: lost the `X_val = X_train.iloc[n_train - n_val :]` "
            "chronological-tail validation carve. Without it early "
            "stopping peeks at the official test set."
        )
        assert "y_val = y_train.iloc[n_train - n_val :]" in norm, (
            "F-22: lost the `y_val` carve from y_train."
        )
        assert "eval_set = [(X_val, y_val)]" in norm, (
            "F-22: lost the `eval_set = [(X_val, y_val)]` plumbing into "
            "model.fit. Without it XGBoost's early_stopping_rounds has "
            "nothing to evaluate against — or, worse, reverts to a "
            "default that uses X_test."
        )

    def test_X_fit_excludes_validation_tail(self):
        src = _read(TRAIN)
        norm = _norm(src)
        assert "X_fit = X_train.iloc[: n_train - n_val]" in norm, (
            "F-22: lost the `X_fit = X_train.iloc[: n_train - n_val]` "
            "training-set carve. Without it the validation tail is "
            "ALSO seen during fit → the validation signal is "
            "in-sample."
        )
        assert "y_fit = y_train.iloc[: n_train - n_val]" in norm, (
            "F-22: lost the `y_fit` carve from y_train."
        )

    def test_fit_uses_X_fit_not_X_train_directly(self):
        src = _read(TRAIN)
        norm = _norm(src)
        # The actual fit call must use X_fit, y_fit.
        assert "model.fit(X_fit, y_fit, eval_set=eval_set" in norm, (
            "F-22: model.fit() must pass X_fit + y_fit (the train-minus-"
            "validation slice) and the eval_set carve. Anything else "
            "leaks the validation data into the booster."
        )

    def test_pathological_path_warns_loudly(self):
        """If the training set is so small that a 15% tail is the whole
        thing, the code falls back to using X_test for early stopping —
        but it must WARN so the operator sees the optimism.

        Python concatenates adjacent f-strings at parse time, so the
        runtime warning is one continuous string; we search the source
        with whitespace + adjacent-string-quote tolerance so the test
        doesn't break on a pure formatting refactor.
        """
        src = _read(TRAIN)
        norm = _norm(src)
        assert "training set too small for a" in norm, (
            "F-22: lost the warning text for the pathological "
            "small-train fallback path. The fallback exists for a "
            "reason but must not be silent."
        )
        # AST-level: extract the actual concatenated string and search.
        tree = ast.parse(src)
        warning_strings: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Match logger.warning(...) calls.
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "warning"
            ):
                continue
            for arg in node.args:
                # JoinedStr (f-string) or Constant (plain string), or
                # adjacent-string concat which Python parses as a single
                # JoinedStr / BinOp tree.
                try:
                    rendered = ast.unparse(arg)
                except Exception:
                    rendered = ""
                warning_strings.append(rendered)
        joined = " ".join(warning_strings)
        # The actual warning text spans multiple adjacent f-strings.
        # After unparse the content is preserved without line breaks.
        joined_norm = _norm(joined)
        assert "Held-out metrics below" in joined_norm, (
            "F-22: lost the `Held-out metrics below` callout in the "
            "logger.warning argument. (AST-level search across adjacent "
            "f-string fragments.)"
        )
        assert "will be optimistic" in joined_norm, (
            "F-22: lost the explicit `optimistic` callout in the "
            "warning. The operator MUST know the reported numbers are "
            "biased when the fallback fires."
        )


# ─────────────────────────────────────────────────────────────────────
# C-23 — out-of-sample calibration split
# ─────────────────────────────────────────────────────────────────────


class TestC23OutOfSampleCalibration:
    """Calibrator fit and Brier evaluation must use DIFFERENT halves
    of X_test (chronological 50/50). Pre-fix both `calibrated.fit(
    X_test, ...)` and `brier_score_loss(y_test, ...)` ran on the
    same set, so the reported Brier was in-sample for the
    calibrator → operators picking isotonic vs sigmoid were
    comparing rotten apples."""

    def test_calibration_split_present(self):
        src = _read(TRAIN)
        norm = _norm(src)
        assert "X_calib_fit, y_calib_fit = X_test[:half], y_test[:half]" in norm, (
            "C-23: lost the `X_calib_fit, y_calib_fit = X_test[:half], "
            "y_test[:half]` split. Calibrator must fit on the first "
            "half of X_test, not the whole thing."
        )
        assert "X_calib_eval, y_calib_eval = X_test[half:], y_test[half:]" in norm, (
            "C-23: lost the `X_calib_eval, y_calib_eval = X_test[half:], "
            "y_test[half:]` split. Calibrator must evaluate on the "
            "second half of X_test."
        )
        # Sanity: chronological. half = len(X_test) // 2.
        assert "half = max(1, len(X_test) // 2)" in norm, (
            "C-23: lost the `half = max(1, len(X_test) // 2)` chronological "
            "midpoint. C-23 specifically requires chronological 50/50."
        )

    def test_calibrator_fit_uses_first_half_only(self):
        src = _read(TRAIN)
        norm = _norm(src)
        assert "calibrated.fit(X_calib_fit, y_calib_fit)" in norm, (
            "C-23: calibrator must `.fit(X_calib_fit, y_calib_fit)`, "
            "not `.fit(X_test, y_test)`."
        )

    def test_calibration_eval_uses_second_half_only(self):
        src = _read(TRAIN)
        norm = _norm(src)
        # Brier / AUC / LogLoss must be computed on y_calib_eval, not y_test.
        assert "brier_score_loss(y_calib_eval, y_proba_cal)" in norm, (
            "C-23: Brier must be computed on `y_calib_eval` "
            "(second-half holdout for the calibrator), not on the full "
            "y_test."
        )
        assert "log_loss(y_calib_eval, y_proba_cal)" in norm, (
            "C-23: LogLoss must be on the second-half eval slice."
        )
        assert "roc_auc_score(y_calib_eval, y_proba_cal)" in norm, (
            "C-23: AUC must be on the second-half eval slice."
        )

    def test_apples_to_apples_raw_vs_calibrated_comparison(self):
        """The reported `(raw_eval N.NNNN)` line must compare on the
        SAME second-half slice — not raw on full X_test vs calibrated
        on second half. Otherwise the lift number is meaningless."""
        src = _read(TRAIN)
        norm = _norm(src)
        assert "y_proba_raw_eval = model.predict_proba(X_calib_eval)" in norm, (
            "C-23: lost the `y_proba_raw_eval = model.predict_proba("
            "X_calib_eval)` recompute. Without it the raw-vs-cal "
            "comparison is rotten in the opposite direction."
        )
        assert "auc_raw_eval = roc_auc_score(y_calib_eval, y_proba_raw_eval)" in norm, (
            "C-23: lost the `auc_raw_eval` apples-to-apples baseline."
        )

    def test_calibration_collapse_safety(self):
        """If calibration drops AUC by >2pp vs the SAME-slice raw
        baseline, the wrapper must fall back to the raw model. AUC is
        monotonic-preserving under calibration, so a drop signals a
        leak or numeric pathology."""
        src = _read(TRAIN)
        norm = _norm(src)
        assert "Calibration collapsed AUC by >2pp" in norm, (
            "C-23: lost the AUC-collapse safety log line. Without it a "
            "broken calibrator can still ship."
        )
        assert "if auc_cal < auc_raw_for_check - 0.02:" in norm, (
            "C-23: lost the `< auc_raw_for_check - 0.02` collapse "
            "threshold. The 2pp number is empirical — change it "
            "deliberately, not accidentally."
        )
        assert "final_model = model" in norm, (
            "C-23: lost the fallback to the raw model when calibration "
            "collapses AUC. The wrapper must ship a usable booster."
        )


# ─────────────────────────────────────────────────────────────────────
# Cross-cutting: source-level signature pin for the runbook
# ─────────────────────────────────────────────────────────────────────


class TestPipelineSignaturesUnchanged:
    """The retrain runbook (§5.9) bakes in the function signatures of
    `prepare_dataset` and `train_xgboost`. If a contributor renames a
    parameter or changes a default, the docker run commands in the
    runbook silently break. Pin the public signatures so the runbook
    stays valid."""

    def test_prepare_dataset_signature_stable(self):
        from packages.training.prepare_dataset import prepare_dataset

        sig = inspect.signature(prepare_dataset)
        params = list(sig.parameters.keys())
        # Order-aware: the runbook positionally relies on (symbols,
        # start, end, interval, horizon, output) — keep these first.
        expected_prefix = [
            "symbols",
            "start",
            "end",
            "interval",
            "horizon",
            "output_dir",
            "period",
            "label_threshold_pct",
            "drop_skew_features",
        ]
        assert params == expected_prefix, (
            f"prepare_dataset() signature drift. Expected:\n"
            f"  {expected_prefix}\n"
            f"Got:\n"
            f"  {params}\n"
            f"Update the runbook in findings_log_2026-05-27.md §5.9 "
            f"if this rename is intentional."
        )

    def test_train_xgboost_signature_stable(self):
        from packages.training.train_xgboost import train_xgboost

        sig = inspect.signature(train_xgboost)
        params = list(sig.parameters.keys())
        expected = [
            "train_path",
            "test_path",
            "model_output",
            "calibrate",
            "calibration_method",
        ]
        assert params == expected, (
            f"train_xgboost() signature drift. Expected:\n"
            f"  {expected}\n"
            f"Got:\n"
            f"  {params}\n"
            f"Update the runbook + retrain CLI flags if intentional."
        )


# ─────────────────────────────────────────────────────────────────────
# Cross-cutting: required CLI flags for the runbook
# ─────────────────────────────────────────────────────────────────────


class TestPrepareDatasetCLIFlags:
    """The retrain runbook + the §5.10 pre-flight Step E both rely on
    a small set of prepare_dataset CLI flags. Pin them so the runbook
    can't silently break."""

    @pytest.mark.parametrize(
        "flag",
        [
            "--symbols",
            "--symbols-file",
            "--use-scanner-universe",
            "--interval",
            "--period",
            "--horizon",
            "--threshold-pct",
            "--limit",
            "--allow-distribution-skew",
            "--output",
        ],
    )
    def test_flag_is_registered(self, flag: str):
        src = _read(PREPARE)
        # ArgumentParser flags are added via add_argument("--name", ...).
        # We only need the flag string to appear, not enforce exact
        # parser shape.
        assert flag in src, (
            f"prepare_dataset.py: required CLI flag {flag!r} is "
            f"missing. Pre-flight Step E or the docker run commands in "
            f"findings_log §5.9 will break."
        )
