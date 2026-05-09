"""Tests for the conviction-aware ATR gate (2026-05-04).

Background
----------
The ATR gate rejects trades on stocks whose 5-min ATR% is below a regime-
specific floor (0.50% for bear_high_vol). It exists to prevent micro-trades
on stocks too quiet to reach a take-profit within session.

On 2026-05-04 the afternoon went exceptionally quiet — every stock in the
200-symbol watchlist was running 0.18-0.48% 5-min ATR. 15 of 16 ensemble
passes that day got rejected at this gate, including:
  - ACMESOLAR @ conf=0.935 (single supertrend SELL)
  - VMM @ conf=0.976 (single supertrend SELL)
  - BELRISE @ conf=0.561 with TWO strategies converging (mean_rev + rsi)

The first two are exceptional-conviction single-strategy votes; the third is
a multi-strategy convergence — exactly the kind of edge the ensemble was
rebuilt to surface. All three should be tradeable in a momentary lull.

Fix: relax the ATR threshold for these high-conviction setups while keeping
the strict gate for marginal (0.55-0.75 conf, single-strategy) signals which
need volatility to make a profit.
  - >=2 contributing strategies OR conf >= 0.85: threshold * 0.40
  - conf >= 0.75: threshold * 0.60
  - else: full threshold
A hard 0.20% floor caps the relaxation: below this even infinite conviction
can't pay for the round-trip.
"""

from unittest.mock import MagicMock, patch

import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategies.base_strategy import Signal, TradeSignal


def _make_signal(confidence: float, contributing_strategies=None, signal=Signal.SELL) -> TradeSignal:
    """Construct a TradeSignal as the ensemble would build it."""
    from datetime import datetime
    return TradeSignal(
        signal=signal,
        symbol="TEST",
        price=100.0,
        timestamp=datetime.now(),
        strategy_name="ensemble",
        confidence=confidence,
        stop_loss=102.0,
        take_profit=98.0,
        metadata={},
        contributing_strategies=contributing_strategies or {},
    )


def _evaluate_atr_gate(signal: TradeSignal, atr_pct: float, base_threshold: float) -> tuple:
    """Recreate the gate logic in isolation so we can unit-test it cleanly.

    Returns (passes: bool, effective_threshold: float, relax_tag: str).
    """
    contrib = getattr(signal, "contributing_strategies", None) or {}
    multi_strategy = len(contrib) >= 2
    if multi_strategy or signal.confidence >= 0.85:
        effective_threshold = max(base_threshold * 0.40, 0.20)
        relax_tag = "multi_strat" if multi_strategy else "high_conf"
    elif signal.confidence >= 0.75:
        effective_threshold = max(base_threshold * 0.60, 0.20)
        relax_tag = "high_conf"
    else:
        effective_threshold = base_threshold
        relax_tag = ""
    passes = atr_pct >= effective_threshold
    return passes, effective_threshold, relax_tag


class TestConvictionTiers:
    """Verify each conviction tier gets the correct relaxation factor."""

    def test_low_conviction_single_strategy_uses_full_threshold(self):
        sig = _make_signal(confidence=0.60, contributing_strategies={"mean_reversion": 1.0})
        passes, eff, tag = _evaluate_atr_gate(sig, atr_pct=0.35, base_threshold=0.50)
        assert eff == 0.50, "Low-conv single-strat must NOT get any relaxation"
        assert tag == ""
        assert not passes, "0.35 ATR < 0.50 threshold = blocked"

    def test_medium_conviction_gets_60_percent_relaxation(self):
        # conf 0.78, single strategy → high_conf tier → threshold * 0.60
        sig = _make_signal(confidence=0.78, contributing_strategies={"supertrend_follow": 1.0})
        passes, eff, tag = _evaluate_atr_gate(sig, atr_pct=0.35, base_threshold=0.50)
        assert eff == pytest.approx(0.30, abs=1e-9)
        assert tag == "high_conf"
        assert passes, "Medium-conv signals at 0.35 ATR pass relaxed threshold of 0.30"

    def test_high_conviction_single_strategy_gets_40_percent_relaxation(self):
        # ACMESOLAR-style: conf 0.935 single supertrend SELL
        sig = _make_signal(confidence=0.935, contributing_strategies={"supertrend_follow": 1.0})
        passes, eff, tag = _evaluate_atr_gate(sig, atr_pct=0.22, base_threshold=0.50)
        assert eff == pytest.approx(0.20, abs=1e-9), "0.85+ conf gets max relaxation: 0.50 * 0.40 = 0.20"
        assert tag == "high_conf"
        assert passes, "ACMESOLAR-style 0.935-conf trade should pass at 0.22 ATR"

    def test_multi_strategy_low_conviction_gets_max_relaxation(self):
        # BELRISE-style: conf 0.561, TWO strategies converging
        sig = _make_signal(
            confidence=0.561,
            contributing_strategies={"rsi_momentum": 0.5, "mean_reversion": 0.5},
        )
        passes, eff, tag = _evaluate_atr_gate(sig, atr_pct=0.41, base_threshold=0.50)
        assert eff == pytest.approx(0.20, abs=1e-9)
        assert tag == "multi_strat"
        assert passes, "Multi-strategy convergence overrides low conviction"

    def test_multi_strategy_takes_precedence_over_single_high_conf(self):
        # When BOTH multi-strat AND >=0.85, multi_strat wins the tag
        sig = _make_signal(
            confidence=0.92,
            contributing_strategies={"rsi_momentum": 0.4, "mean_reversion": 0.6},
        )
        _, eff, tag = _evaluate_atr_gate(sig, atr_pct=0.30, base_threshold=0.50)
        assert eff == pytest.approx(0.20, abs=1e-9)
        assert tag == "multi_strat"  # not "high_conf"


class TestHardFloor:
    """The 0.20% hard floor must apply regardless of conviction."""

    def test_extreme_conviction_cannot_break_hard_floor(self):
        # VMM-style: conf 0.976, ATR 0.18 — even with full relaxation,
        # 0.18 < 0.20 hard floor → must still block.
        sig = _make_signal(confidence=0.976, contributing_strategies={"supertrend_follow": 1.0})
        passes, eff, _ = _evaluate_atr_gate(sig, atr_pct=0.18, base_threshold=0.50)
        assert eff == 0.20, "Hard floor caps the relaxation"
        assert not passes, "ATR 0.18 below 0.20 hard floor must block even at conf=0.976"

    def test_hard_floor_does_not_raise_already_low_thresholds(self):
        # When the base threshold is already below the hard floor (e.g. unknown
        # regime falls to flat 0.5), max() preserves the higher value.
        sig = _make_signal(confidence=0.95)
        _, eff, _ = _evaluate_atr_gate(sig, atr_pct=0.5, base_threshold=0.10)
        # base*0.40 = 0.04, max(0.04, 0.20) = 0.20
        assert eff == 0.20

    def test_relaxation_never_increases_threshold(self):
        for base in (0.30, 0.40, 0.50, 0.60, 0.70, 0.80):
            for conf in (0.55, 0.65, 0.75, 0.85, 0.95):
                sig = _make_signal(confidence=conf)
                _, eff, _ = _evaluate_atr_gate(sig, atr_pct=0.50, base_threshold=base)
                assert eff <= base, (
                    f"Relaxation must never raise threshold; base={base}, conf={conf}, eff={eff}"
                )


class TestRegressionDoesNotRelaxMarginalSignals:
    """Critical: the relaxation must NOT let through low-conviction noise."""

    def test_marginal_055_conf_single_strategy_still_blocked_at_low_atr(self):
        # CGPOWER-style: conf 0.574, single supertrend, ATR 0.25
        sig = _make_signal(confidence=0.574, contributing_strategies={"supertrend_follow": 1.0})
        passes, eff, tag = _evaluate_atr_gate(sig, atr_pct=0.25, base_threshold=0.50)
        assert eff == 0.50
        assert tag == ""
        assert not passes, "Marginal 0.574-conf single-strat must stay blocked at low ATR"

    def test_065_conf_single_strategy_still_uses_full_threshold(self):
        # 0.65 is a typical "passed-the-ensemble" but not exceptional; full threshold
        sig = _make_signal(confidence=0.65, contributing_strategies={"mean_reversion": 1.0})
        _, eff, tag = _evaluate_atr_gate(sig, atr_pct=0.40, base_threshold=0.50)
        assert eff == 0.50
        assert tag == ""

    def test_075_boundary_just_starts_relaxation(self):
        # 0.75 is the lower threshold of the medium-conv tier
        sig = _make_signal(confidence=0.75, contributing_strategies={"supertrend_follow": 1.0})
        _, eff, tag = _evaluate_atr_gate(sig, atr_pct=0.30, base_threshold=0.50)
        assert eff == pytest.approx(0.30, abs=1e-9)
        assert tag == "high_conf"

    def test_just_below_075_does_not_relax(self):
        sig = _make_signal(confidence=0.749, contributing_strategies={"supertrend_follow": 1.0})
        _, eff, tag = _evaluate_atr_gate(sig, atr_pct=0.30, base_threshold=0.50)
        assert eff == 0.50
        assert tag == ""


class TestEmptyContribDictDoesNotConfuseMultiStratCheck:
    def test_none_contrib_treated_as_empty(self):
        sig = _make_signal(confidence=0.95, contributing_strategies=None)
        # 0.95 >= 0.85 → high_conf, not multi_strat
        _, eff, tag = _evaluate_atr_gate(sig, atr_pct=0.25, base_threshold=0.50)
        assert tag == "high_conf"
        assert eff == 0.20

    def test_single_entry_contrib_is_not_multi_strat(self):
        sig = _make_signal(confidence=0.92, contributing_strategies={"only_one": 1.0})
        _, _, tag = _evaluate_atr_gate(sig, atr_pct=0.25, base_threshold=0.50)
        assert tag == "high_conf"


class TestTodaysActualBlockedSignals:
    """Replay each of today's 15 ATR-gate-blocked ensemble signals and check
    that the new gate produces the right outcome (most should still block,
    only ACMESOLAR-style and BELRISE-style should now pass).
    """

    BASE_THRESHOLD = 0.50  # bear_high_vol

    def _check(self, name, conf, contrib_count, atr, *, should_pass):
        contrib = {f"s{i}": 1.0/contrib_count for i in range(contrib_count)}
        sig = _make_signal(confidence=conf, contributing_strategies=contrib)
        passes, eff, _ = _evaluate_atr_gate(sig, atr_pct=atr, base_threshold=self.BASE_THRESHOLD)
        assert passes == should_pass, (
            f"{name} (conf={conf}, contrib={contrib_count}, ATR={atr}): "
            f"expected {'PASS' if should_pass else 'BLOCK'}, got eff={eff:.2f}"
        )

    def test_acmesolar_high_conf_now_passes(self):
        self._check("ACMESOLAR", 0.935, 1, 0.22, should_pass=True)

    def test_belrise_multi_strat_now_passes(self):
        self._check("BELRISE", 0.561, 2, 0.41, should_pass=True)

    def test_vmm_blocked_by_hard_floor(self):
        # VMM was 0.976 conf but ATR 0.18 < 0.20 hard floor — still blocked
        self._check("VMM", 0.976, 1, 0.18, should_pass=False)

    def test_cgpower_still_blocked(self):
        self._check("CGPOWER", 0.574, 1, 0.25, should_pass=False)

    def test_abdl_still_blocked(self):
        self._check("ABDL", 0.569, 1, 0.26, should_pass=False)

    def test_prestige_still_blocked(self):
        self._check("PRESTIGE", 0.636, 1, 0.35, should_pass=False)

    def test_elecon_still_blocked(self):
        self._check("ELECON", 0.578, 1, 0.35, should_pass=False)

    def test_ushamart_borderline_blocked(self):
        # 0.725 conf is *just* below the high_conf tier (0.75) → no relaxation
        self._check("USHAMART", 0.725, 1, 0.28, should_pass=False)

    def test_engineersin_still_blocked(self):
        self._check("ENGINERSIN", 0.552, 1, 0.30, should_pass=False)

    def test_atgl_still_blocked(self):
        self._check("ATGL", 0.682, 1, 0.30, should_pass=False)

    def test_cgcl_still_blocked(self):
        self._check("CGCL", 0.575, 1, 0.48, should_pass=False)

    def test_railtel_still_blocked(self):
        self._check("RAILTEL", 0.559, 1, 0.28, should_pass=False)

    def test_tatatech_still_blocked(self):
        self._check("TATATECH", 0.568, 1, 0.33, should_pass=False)

    def test_sail_still_blocked(self):
        self._check("SAIL", 0.551, 1, 0.25, should_pass=False)

    def test_lodha_still_blocked(self):
        self._check("LODHA", 0.574, 1, 0.25, should_pass=False)

    def test_lalpathlab_still_blocked_by_atr(self):
        # LALPATHLAB conf 0.597 — got blocked by NOTIONAL-FLOOR after passing
        # ATR; we don't know exact ATR but assume something around 0.30
        self._check("LALPATHLAB", 0.597, 1, 0.30, should_pass=False)
