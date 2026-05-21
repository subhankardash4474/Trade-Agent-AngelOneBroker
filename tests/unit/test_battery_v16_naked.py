"""Tests for V16_completely_naked: the diagnostic upper-bound variant.

Lands 2026-05-21 alongside a small extension to ``_bt_config()`` that
propagates the ``apply_dead_hour`` and ``apply_expected_profit_gate``
boolean flags from a new ``backtest_gates`` cfg section. Variants can
now opt into disabling those two gates; defaults preserve the existing
behaviour for every other variant.

V16 is the freeze-v2.1 diagnostic for "what does the harness produce
when every modelled gate is OFF". Reading the result against V1
(baseline) and V2 (only trend filter off) tells us whether our gates
are over-aggressive, correctly protective, or barely doing anything --
each outcome implies a different freeze-exit interpretation.

These tests pin three contracts so future refactors can't silently
regress them:

  1. V16 is registered exactly once.
  2. V16 disables every modelled gate (the 9 fields the harness reads).
  3. V16 is a strict superset of V2 (so anything V2 tests, V16 tests).
  4. The two new flag-reads do NOT alter the BacktestConfig produced
     for any pre-existing variant.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from research.battery import VARIANTS, _bt_config, _build_variant_config  # noqa: E402


# ──────────────────────────── helpers ────────────────────────────


def _find(name: str) -> list:
    """Return the override list for a variant by name, or raise."""
    for n, ovs in VARIANTS:
        if n == name:
            return ovs
    raise AssertionError(f"variant {name!r} not in VARIANTS")


def _base_cfg() -> dict:
    """Minimal cfg shape mirroring config.yaml -- enough that
    _bt_config + _build_variant_config can produce a valid
    BacktestConfig without touching disk."""
    return {
        "backtest": {
            "initial_capital": 25000.0,
            "commission_pct": 0.03,
            "slippage_pct": 0.05,
        },
        "ensemble": {"confidence_threshold": 0.55},
        "robustness": {
            "min_entry_atr_pct": 0.5,
            "max_losses_per_stock_per_day": 2,
        },
        "risk": {
            "min_profit_to_charges_ratio": 4.0,
            "min_absolute_reward_rs": 20.0,
        },
        "strategies": {
            "mean_reversion": {"trend_filter_pct": 5.0},
            "xgboost_classifier": {"trend_filter_pct": 5.0},
            "supertrend_follow": {"trend_filter_pct": 5.0},
            "rsi_momentum": {"trend_filter_pct": 5.0},
            "vwap_bounce": {"trend_filter_pct": 5.0},
            "opening_range_breakout": {"trend_filter_pct": 5.0},
        },
        "execution": {"product_type": "INTRADAY"},
    }


# ──────────────────── 1. registration sanity ─────────────────────


class TestV16Registration:
    def test_v16_is_registered(self):
        names = [n for n, _ in VARIANTS]
        assert "V16_completely_naked" in names

    def test_v16_registered_exactly_once(self):
        names = [n for n, _ in VARIANTS]
        assert names.count("V16_completely_naked") == 1

    def test_v16_appears_after_v15(self):
        names = [n for n, _ in VARIANTS]
        assert names.index("V16_completely_naked") > names.index("V15_mr_xgb_only")


# ─────────────── 2. modelled-gate disables (the contract) ────────


class TestV16DisablesAllModelledGates:
    """V16 must zero / disable every gate the harness models."""

    def test_strategy_trend_filters_all_none(self):
        cfg = _build_variant_config(_base_cfg(), _find("V16_completely_naked"))
        for s_name, s_cfg in cfg["strategies"].items():
            assert s_cfg.get("trend_filter_pct") is None, (
                f"V16 left trend_filter on {s_name}: {s_cfg.get('trend_filter_pct')}"
            )

    def test_confidence_threshold_zero(self):
        cfg = _build_variant_config(_base_cfg(), _find("V16_completely_naked"))
        bt = _bt_config(cfg)
        assert bt.confidence_threshold == 0.0

    def test_min_entry_atr_pct_zero(self):
        cfg = _build_variant_config(_base_cfg(), _find("V16_completely_naked"))
        bt = _bt_config(cfg)
        assert bt.min_entry_atr_pct == 0.0

    def test_min_profit_to_charges_zero(self):
        cfg = _build_variant_config(_base_cfg(), _find("V16_completely_naked"))
        bt = _bt_config(cfg)
        assert bt.min_profit_to_charges_ratio == 0.0

    def test_min_absolute_reward_zero(self):
        cfg = _build_variant_config(_base_cfg(), _find("V16_completely_naked"))
        bt = _bt_config(cfg)
        assert bt.min_absolute_reward_rs == 0.0

    def test_max_positions_lifted(self):
        cfg = _build_variant_config(_base_cfg(), _find("V16_completely_naked"))
        bt = _bt_config(cfg)
        # Live agent uses max_open_positions=12; the harness reads
        # risk.max_positions which has no live config entry. V16 sets
        # it well above any plausible concurrent count.
        assert bt.max_positions >= 50

    def test_max_losses_per_stock_lifted(self):
        cfg = _build_variant_config(_base_cfg(), _find("V16_completely_naked"))
        bt = _bt_config(cfg)
        assert bt.max_losses_per_stock >= 50

    def test_dead_hour_disabled(self):
        cfg = _build_variant_config(_base_cfg(), _find("V16_completely_naked"))
        bt = _bt_config(cfg)
        assert bt.apply_dead_hour is False

    def test_expected_profit_gate_disabled(self):
        cfg = _build_variant_config(_base_cfg(), _find("V16_completely_naked"))
        bt = _bt_config(cfg)
        assert bt.apply_expected_profit_gate is False


# ──────────────── 3. V16 is a strict superset of V2 ──────────────


class TestV16SupersetOfV2:
    """V2 zeros only the per-strategy trend filter. V16 must keep
    every V2 override AND add more. If a future refactor accidentally
    drops one of V2's entries from V16, this catches it."""

    def test_v16_overrides_include_every_v2_override(self):
        v2 = dict(_find("V2_all_filters_off"))
        v16 = dict(_find("V16_completely_naked"))
        for k, v in v2.items():
            assert k in v16, f"V16 missing V2 override {k!r}"
            assert v16[k] == v, (
                f"V16 changed V2's value for {k!r}: V2={v!r} V16={v16[k]!r}"
            )

    def test_v16_strictly_more_overrides_than_v2(self):
        v2 = _find("V2_all_filters_off")
        v16 = _find("V16_completely_naked")
        assert len(v16) > len(v2)


# ──────────────── 4. existing variants are unchanged ─────────────


class TestExistingVariantsUnaffected:
    """The two new boolean reads in _bt_config must default to True
    (matching the dataclass default), so every variant that doesn't
    opt into ``backtest_gates`` keeps its previous behaviour bit-for-bit."""

    def test_v1_baseline_keeps_dead_hour_on(self):
        cfg = _build_variant_config(_base_cfg(), _find("V1_baseline_current_shipped"))
        bt = _bt_config(cfg)
        assert bt.apply_dead_hour is True
        assert bt.apply_expected_profit_gate is True

    def test_v2_keeps_dead_hour_on(self):
        # V2 disables the trend filter on strategies; it must NOT have
        # spuriously inherited the new dead_hour / profit_gate flags.
        cfg = _build_variant_config(_base_cfg(), _find("V2_all_filters_off"))
        bt = _bt_config(cfg)
        assert bt.apply_dead_hour is True
        assert bt.apply_expected_profit_gate is True

    def test_v14_opening_lockout_off_keeps_other_gates(self):
        cfg = _build_variant_config(_base_cfg(), _find("V14_opening_lockout_off"))
        bt = _bt_config(cfg)
        # V14 only touches opening_lockout_minutes, which the harness
        # doesn't even model. The harness-modelled gates must stay ON.
        assert bt.apply_dead_hour is True
        assert bt.apply_expected_profit_gate is True
        assert bt.confidence_threshold == 0.55
        assert bt.min_entry_atr_pct == 0.5

    def test_v15_mr_xgb_only_unchanged(self):
        cfg = _build_variant_config(_base_cfg(), _find("V15_mr_xgb_only"))
        bt = _bt_config(cfg)
        assert bt.apply_dead_hour is True
        assert bt.apply_expected_profit_gate is True


# ──────────── 5. backtest_gates cfg propagation contract ─────────


class TestBacktestGatesPropagation:
    """The new ``backtest_gates`` config section is the documented
    way for variants to flip ``apply_dead_hour`` and
    ``apply_expected_profit_gate``. Pin both halves of the contract."""

    def test_dead_hour_explicit_false(self):
        cfg = _base_cfg()
        cfg["backtest_gates"] = {"apply_dead_hour": False}
        bt = _bt_config(cfg)
        assert bt.apply_dead_hour is False
        assert bt.apply_expected_profit_gate is True  # untouched

    def test_profit_gate_explicit_false(self):
        cfg = _base_cfg()
        cfg["backtest_gates"] = {"apply_expected_profit_gate": False}
        bt = _bt_config(cfg)
        assert bt.apply_dead_hour is True  # untouched
        assert bt.apply_expected_profit_gate is False

    def test_both_flags_explicit_false(self):
        cfg = _base_cfg()
        cfg["backtest_gates"] = {
            "apply_dead_hour": False,
            "apply_expected_profit_gate": False,
        }
        bt = _bt_config(cfg)
        assert bt.apply_dead_hour is False
        assert bt.apply_expected_profit_gate is False

    def test_explicit_true_still_true(self):
        cfg = _base_cfg()
        cfg["backtest_gates"] = {
            "apply_dead_hour": True,
            "apply_expected_profit_gate": True,
        }
        bt = _bt_config(cfg)
        assert bt.apply_dead_hour is True
        assert bt.apply_expected_profit_gate is True

    def test_missing_section_defaults_to_true(self):
        cfg = _base_cfg()
        # No `backtest_gates` key at all.
        bt = _bt_config(cfg)
        assert bt.apply_dead_hour is True
        assert bt.apply_expected_profit_gate is True

    def test_empty_section_defaults_to_true(self):
        cfg = _base_cfg()
        cfg["backtest_gates"] = {}
        bt = _bt_config(cfg)
        assert bt.apply_dead_hour is True
        assert bt.apply_expected_profit_gate is True
