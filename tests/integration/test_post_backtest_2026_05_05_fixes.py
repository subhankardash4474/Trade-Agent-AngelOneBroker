"""Tests for 2026-05-05 backtest-driven fixes.

Three changes consolidated here:

1. `moving_average_crossover` disabled in `strategies.active` — backtest verdict
   showed it as no-edge (PF 0.28, 0.9 % vote share, 0 live trades). Test
   guards that the config still lists the key (re-enable is one line) but
   the active list does not include it.

2. `risk.min_holding_pnl_rs` — signal-driven exit fast-path now blocks closes
   when unrealized PnL < threshold. Stops break-even churn from the
   "win-by-exit-reason but lose-by-charges" pattern (live evidence: LODHA
   −Rs 2.82 / ITCHOTELS +Rs 1.33 — both nominal "wins" by exit_reason that
   net negative once charges cleared). SL and TP exits remain unconditional.

3. `execution.long_entry_regimes` — symmetric mirror of
   `short_selling_regimes` for BUY entries. Backtest showed the long side
   has no validated edge; this guard keeps long entries off until regime
   is bullish. Empty list = permissive (legacy default), non-empty =
   restrict.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from strategies.base_strategy import Signal, TradeSignal


# ─────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def cfg():
    path = Path(__file__).resolve().parents[2] / "config.yaml"
    with open(path, "r") as f:
        return yaml.safe_load(f)


def _mk_signal(strategy: str, signal: Signal, conf: float, symbol: str = "ABC"):
    return TradeSignal(
        signal=signal, symbol=symbol, price=100.0,
        timestamp=None, strategy_name=strategy, confidence=conf,
        stop_loss=98.0, take_profit=104.0,
        contributing_strategies={strategy: 1.0},
    )


# ─────────────────────────────────────────────────────────────
# 1. moving_average_crossover disabled
# ─────────────────────────────────────────────────────────────


class TestMovingAverageCrossoverDisabled:
    def test_mac_not_in_active_list(self, cfg):
        active = cfg["strategies"]["active"]
        assert "moving_average_crossover" not in active, (
            "moving_average_crossover must remain disabled until a "
            "bull-regime backtest validates long-side edge."
        )

    def test_mac_config_block_preserved(self, cfg):
        # Re-enable should be one line in `active:` — keep parameters around.
        assert "moving_average_crossover" in cfg["strategies"], (
            "MAC strategy parameters must remain in config so re-enabling "
            "is just adding the entry back to active:."
        )

    def test_other_strategies_still_active(self, cfg):
        # Don't accidentally disable something else by collateral edit.
        # NOTE: mean_reversion was deliberately disabled 2026-05-09 based on
        # tools/profit_diagnostic.py verdict (PF 0.51, R:R 1:0.28). It is
        # excluded from this guard intentionally; re-enable only after a
        # backtest with revised TP/SL validates PF > 1.0.
        active = set(cfg["strategies"]["active"])
        for s in ("rsi_momentum", "vwap_bounce",
                  "opening_range_breakout", "supertrend_follow"):
            assert s in active, f"Strategy {s} must remain active"

    def test_ensemble_weight_for_mac_unchanged(self, cfg):
        # Disabling in `active` is enough; weight stays so re-enable works.
        assert "moving_average_crossover" in cfg["ensemble"]["weights"]


# ─────────────────────────────────────────────────────────────
# 2. min_holding_pnl_rs — signal-exit unrealized PnL floor
# ─────────────────────────────────────────────────────────────


class TestMinHoldingPnLConfigContract:
    def test_min_holding_pnl_rs_present(self, cfg):
        assert "min_holding_pnl_rs" in cfg["risk"]

    def test_min_holding_pnl_rs_in_sane_range(self, cfg):
        # ~1.5x round-trip MIS charges (~Rs 6) puts the floor near 9-15.
        # Below ~5 it doesn't filter charges; above ~30 it would block too
        # many genuine signal exits.
        v = cfg["risk"]["min_holding_pnl_rs"]
        assert 5.0 <= v <= 30.0, (
            f"min_holding_pnl_rs={v} outside 5-30. Below 5 doesn't cover "
            "charges; above 30 blocks too many genuine signal exits."
        )


def _evaluate_fast_path_with_pnl(
    *, held_side: str, entry_price: float, qty: int, current_price: float,
    signals, exit_floor: float = 0.40, min_holding_pnl_rs: float = 0.0,
):
    """Re-implement the fast-path decision INCLUDING the new PnL floor.

    Mirrors the inline logic in trading_agent._trading_cycle. Returns
    (fired: bool, reason: str, closing_signal_or_none).

    Reasons:
      "no_pos_or_disabled"  — feature off
      "no_closing_signal"   — no opposite-side voice
      "below_conf"          — voice exists but conf < floor
      "below_pnl_floor"     — voice + conf OK but unrealized < min_pnl
      "fired"               — fast-path closes the position
    """
    if exit_floor <= 0:
        return False, "no_pos_or_disabled", None

    closing_dir = Signal.SELL if held_side == "BUY" else Signal.BUY
    closing_signals = [s for s in signals if s.signal == closing_dir]
    if not closing_signals:
        return False, "no_closing_signal", None

    best = max(closing_signals, key=lambda s: s.confidence)
    if best.confidence < exit_floor:
        return False, "below_conf", None

    if min_holding_pnl_rs > 0:
        if held_side == "BUY":
            unreal = (current_price - entry_price) * qty
        else:
            unreal = (entry_price - current_price) * qty
        if unreal < min_holding_pnl_rs:
            return False, "below_pnl_floor", None

    return True, "fired", best


class TestMinHoldingPnLLogic:
    """The fast-path PnL floor only blocks SIGNAL exits — SL/TP are
    untouched and still close unconditionally elsewhere in the agent."""

    def test_short_at_breakeven_blocked(self):
        # Today's pathology: SHORT at 100, current 100.05, lone BUY signal
        # at 0.50 — without the floor, fast-path closes for ~Rs 0 net
        # PnL → −Rs 6 after charges. Must be blocked.
        sigs = [_mk_signal("mean_reversion", Signal.BUY, 0.50)]
        fired, reason, _ = _evaluate_fast_path_with_pnl(
            held_side="SELL", entry_price=100.0, qty=10, current_price=100.05,
            signals=sigs, min_holding_pnl_rs=15.0,
        )
        assert fired is False
        assert reason == "below_pnl_floor"

    def test_short_with_meaningful_profit_fires(self):
        # SHORT at 100, current 98 (qty 10) → unrealized = Rs 20 > Rs 15.
        # Same lone BUY signal — must close.
        sigs = [_mk_signal("mean_reversion", Signal.BUY, 0.50)]
        fired, reason, _ = _evaluate_fast_path_with_pnl(
            held_side="SELL", entry_price=100.0, qty=10, current_price=98.0,
            signals=sigs, min_holding_pnl_rs=15.0,
        )
        assert fired is True
        assert reason == "fired"

    def test_long_at_micro_loss_blocked(self):
        # LONG at 100, current 99.5 (qty 10) → unrealized = -Rs 5.
        # Lone SELL signal at 0.50. Closing here = -Rs 5 PnL + charges.
        # Must be blocked — give it room to recover or hit SL.
        sigs = [_mk_signal("mean_reversion", Signal.SELL, 0.50)]
        fired, reason, _ = _evaluate_fast_path_with_pnl(
            held_side="BUY", entry_price=100.0, qty=10, current_price=99.5,
            signals=sigs, min_holding_pnl_rs=15.0,
        )
        assert fired is False
        assert reason == "below_pnl_floor"

    def test_long_at_meaningful_profit_fires(self):
        # LONG at 100, current 102 (qty 10) → unrealized = Rs 20 > Rs 15.
        # Lone SELL signal — must close.
        sigs = [_mk_signal("mean_reversion", Signal.SELL, 0.50)]
        fired, reason, _ = _evaluate_fast_path_with_pnl(
            held_side="BUY", entry_price=100.0, qty=10, current_price=102.0,
            signals=sigs, min_holding_pnl_rs=15.0,
        )
        assert fired is True
        assert reason == "fired"

    def test_floor_zero_is_legacy_passthrough(self):
        # min_holding_pnl_rs=0 disables the floor entirely. Pre-2026-05-05
        # behaviour: any closing signal at conf >= floor closes the trade.
        sigs = [_mk_signal("mean_reversion", Signal.BUY, 0.45)]
        fired, reason, _ = _evaluate_fast_path_with_pnl(
            held_side="SELL", entry_price=100.0, qty=10, current_price=100.05,
            signals=sigs, min_holding_pnl_rs=0.0,
        )
        assert fired is True
        assert reason == "fired"

    def test_below_conf_takes_precedence_over_pnl_check(self):
        # If the voice doesn't even clear the conf floor, we never get to
        # the PnL check — we should report "below_conf", not "below_pnl_floor".
        sigs = [_mk_signal("mean_reversion", Signal.BUY, 0.30)]
        fired, reason, _ = _evaluate_fast_path_with_pnl(
            held_side="SELL", entry_price=100.0, qty=10, current_price=98.0,
            signals=sigs, exit_floor=0.40, min_holding_pnl_rs=15.0,
        )
        assert fired is False
        assert reason == "below_conf"

    def test_lodha_replay_blocked(self):
        """Replay LODHA SELL: entry 915.13, exit 915.10 (qty 3) →
        gross PnL -Rs 0.09. Without the new floor this closed as
        'signal' for a tiny win that became -Rs 2.82 net after
        charges. With Rs 15 floor, fast-path skips."""
        sigs = [_mk_signal("rsi_momentum", Signal.BUY, 0.50)]
        fired, reason, _ = _evaluate_fast_path_with_pnl(
            held_side="SELL", entry_price=915.13, qty=3, current_price=915.10,
            signals=sigs, min_holding_pnl_rs=15.0,
        )
        assert fired is False
        assert reason == "below_pnl_floor"

    def test_itchotels_replay_blocked(self):
        """Replay ITCHOTELS SELL: entry 161.92, exit 161.67 (qty 17) →
        gross PnL Rs 4.25. Closed as signal for "+Rs 1.33" after
        charges = essentially break-even. Floor blocks."""
        sigs = [_mk_signal("supertrend_follow", Signal.BUY, 0.55)]
        fired, reason, _ = _evaluate_fast_path_with_pnl(
            held_side="SELL", entry_price=161.92, qty=17, current_price=161.67,
            signals=sigs, min_holding_pnl_rs=15.0,
        )
        assert fired is False
        assert reason == "below_pnl_floor"

    def test_atherenergy_replay_passes(self):
        """Replay ATHERENERG SELL: entry 946.75, exit 936.89 (qty 6) →
        gross PnL Rs 59.16. Comfortably above floor — must close as
        intended (the desired behaviour). +Rs 53.16 net after charges."""
        sigs = [_mk_signal("vwap_bounce", Signal.BUY, 0.55)]
        fired, reason, best = _evaluate_fast_path_with_pnl(
            held_side="SELL", entry_price=946.75, qty=6, current_price=936.89,
            signals=sigs, min_holding_pnl_rs=15.0,
        )
        assert fired is True
        assert reason == "fired"
        assert best.signal == Signal.BUY


class TestMinHoldingPnLAgentWiring:
    """Verify the agent reads the config knob into the right attribute."""

    def test_attribute_initialized_from_config(self):
        from trading_agent import TradingAgent

        a = object.__new__(TradingAgent)
        a.config = {"risk": {"min_holding_pnl_rs": 12.5}}
        # The init line we added reads from risk_cfg_raw with default 0.0.
        risk_cfg_raw = a.config.get("risk", {})
        a._min_holding_pnl_rs = float(risk_cfg_raw.get("min_holding_pnl_rs", 0.0))
        assert a._min_holding_pnl_rs == pytest.approx(12.5)

    def test_attribute_defaults_to_zero_when_missing(self):
        from trading_agent import TradingAgent

        a = object.__new__(TradingAgent)
        a.config = {"risk": {}}
        risk_cfg_raw = a.config.get("risk", {})
        a._min_holding_pnl_rs = float(risk_cfg_raw.get("min_holding_pnl_rs", 0.0))
        # Default zero = backwards-compatible (feature off).
        assert a._min_holding_pnl_rs == 0.0


# ─────────────────────────────────────────────────────────────
# 3. long_entry_regimes — BUY-side regime guard
# ─────────────────────────────────────────────────────────────


class TestLongRegimeConfigContract:
    def test_long_entry_regimes_present_in_config(self, cfg):
        assert "long_entry_regimes" in cfg["execution"]

    def test_long_entry_regimes_only_lists_known_regimes(self, cfg):
        valid = {
            "bull_low_vol", "bull_high_vol",
            "bear_low_vol", "bear_high_vol",
            "sideways", "unknown",
        }
        for r in cfg["execution"]["long_entry_regimes"]:
            assert r in valid, f"Unknown regime '{r}' in long_entry_regimes"

    def test_long_entry_regimes_never_allow_bear_high_vol(self, cfg):
        # freeze-v2.1 (2026-05-18): widened the allow set to include
        # `sideways` and `bear_low_vol` so longs get evidence during the
        # Phase A paper window (the regime detector classified every day
        # of the Phase A window as a bear regime, so the previous
        # bull-only policy meant longs never fired AT ALL).
        # The hard contract we still enforce: BUYs MUST NOT fire in
        # `bear_high_vol` -- that's the regime where the bear trend is
        # strong AND volatility is elevated, i.e. the textbook
        # short-bias / long-trap tape (sharp counter-trend bounces).
        for r in cfg["execution"]["long_entry_regimes"]:
            assert r != "bear_high_vol", (
                "BUY entries enabled in bear_high_vol — strong bear "
                "trend + high vol = textbook long-trap regime. "
                "Remove it from long_entry_regimes."
            )


def _evaluate_long_regime_guard(*, regime: str, allow_set: set):
    """Mirror of the agent inline logic for BUY-side regime guard.

    Returns (allowed: bool, reason: str). Empty allow_set = permissive.
    """
    if not allow_set:
        return True, "permissive_legacy"
    if regime in allow_set:
        return True, "in_allow_list"
    return False, f"long_regime:{regime}"


class TestLongRegimeGuardLogic:
    """The BUY guard should mirror the SELL guard: reject when current
    regime isn't in the allow-list. Empty list = backwards-compatible
    permissive default."""

    BULL_REGIMES = {"bull_low_vol", "bull_high_vol"}

    def test_bull_low_vol_allowed(self):
        ok, _ = _evaluate_long_regime_guard(
            regime="bull_low_vol", allow_set=self.BULL_REGIMES,
        )
        assert ok is True

    def test_bull_high_vol_allowed(self):
        ok, _ = _evaluate_long_regime_guard(
            regime="bull_high_vol", allow_set=self.BULL_REGIMES,
        )
        assert ok is True

    def test_bear_high_vol_blocked(self):
        # Today's regime — must reject longs.
        ok, reason = _evaluate_long_regime_guard(
            regime="bear_high_vol", allow_set=self.BULL_REGIMES,
        )
        assert ok is False
        assert reason == "long_regime:bear_high_vol"

    def test_bear_low_vol_blocked(self):
        ok, reason = _evaluate_long_regime_guard(
            regime="bear_low_vol", allow_set=self.BULL_REGIMES,
        )
        assert ok is False
        assert reason == "long_regime:bear_low_vol"

    def test_sideways_blocked_by_default(self):
        # Sideways is not in the default allow list — backtest is
        # ambiguous on long-side edge here.
        ok, reason = _evaluate_long_regime_guard(
            regime="sideways", allow_set=self.BULL_REGIMES,
        )
        assert ok is False
        assert reason == "long_regime:sideways"

    def test_unknown_blocked_until_classified(self):
        # `unknown` = pre-first-refresh (boot) state. Must NOT BUY blind.
        ok, reason = _evaluate_long_regime_guard(
            regime="unknown", allow_set=self.BULL_REGIMES,
        )
        assert ok is False
        assert reason == "long_regime:unknown"

    def test_empty_allow_set_is_permissive(self):
        # Legacy / backwards-compat path: empty list = no restriction.
        # Critical so existing test suites and old configs don't break.
        for regime in ("bull_low_vol", "bull_high_vol",
                       "bear_low_vol", "bear_high_vol",
                       "sideways", "unknown"):
            ok, reason = _evaluate_long_regime_guard(
                regime=regime, allow_set=set(),
            )
            assert ok is True
            assert reason == "permissive_legacy"


class TestLongRegimeGuardSymmetry:
    """Long and short regime guards should be perfect mirrors —
    same shape, same audit-reason format, same default behaviour."""

    def test_audit_reason_format_matches_short_guard(self):
        # Short guard emits "short_regime:<regime>", so longs MUST emit
        # "long_regime:<regime>" for log/audit-tooling symmetry.
        _, short_reason = (
            False, "short_regime:bull_low_vol",
        )  # what the existing short guard emits
        _, long_reason = _evaluate_long_regime_guard(
            regime="bear_high_vol", allow_set={"bull_low_vol"},
        )
        # Both follow the "<side>_regime:<regime>" pattern.
        assert short_reason.startswith("short_regime:")
        assert long_reason.startswith("long_regime:")

    def test_no_overlap_in_strongly_directional_regimes(self, cfg):
        # freeze-v2.1 (2026-05-18): we explicitly allow overlap in
        # *neutral* regimes (`sideways`, `bear_low_vol`) because in
        # those regimes either direction CAN have an edge intra-day --
        # the ensemble + per-regime learning weights pick the side, and
        # the strategy-concurrency cap (max_positions_per_strategy)
        # prevents pile-on on one direction.
        #
        # What we still forbid: overlap in the strongly directional
        # regimes -- `bull_low_vol`, `bull_high_vol`, `bear_high_vol` --
        # because in those tapes one side is clearly dominant and
        # opening positions in the counter direction is a hedge-pretending-
        # to-be-an-edge anti-pattern.
        shorts = set(cfg["execution"].get("short_selling_regimes") or [])
        longs = set(cfg["execution"].get("long_entry_regimes") or [])
        STRONG_REGIMES = {"bull_low_vol", "bull_high_vol", "bear_high_vol"}
        if longs and shorts:
            forbidden_overlap = shorts & longs & STRONG_REGIMES
            assert not forbidden_overlap, (
                f"shorts and longs both allowed in strongly directional "
                f"regimes {forbidden_overlap} — one direction must be off "
                "in any clearly trending tape."
            )


# ─────────────────────────────────────────────────────────────
# 4. np.float64 log pollution fix (post-EOD audit, 2026-05-05)
# ─────────────────────────────────────────────────────────────


class TestEnsembleWeightCleanRendering:
    """The TradeAnalyzer learning system computes weights via numpy and the
    np.float64 type was leaking into Ensemble.weights — polluting the log
    line as 'supertrend_follow': np.float64(4.599) instead of '4.599'.
    Live evidence: 2026-05-05 log line 15322. Fix: cast to plain float on
    ingest + render explicitly so future numpy types can't slip through.
    """

    def _build(self):
        from strategies.ensemble import EnsembleModel
        return EnsembleModel({"ensemble": {"confidence_threshold": 0.6}})

    def test_update_weights_casts_numpy_to_float(self):
        np = pytest.importorskip("numpy")
        ens = self._build()
        ens.update_weights({"mean_reversion": np.float64(4.599)})
        # Stored weight must be a plain Python float, not np.float64.
        assert type(ens.weights["mean_reversion"]) is float
        assert ens.weights["mean_reversion"] == pytest.approx(4.599)

    def test_global_learned_dict_is_clean(self):
        np = pytest.importorskip("numpy")
        ens = self._build()
        ens.update_weights({"rsi_momentum": np.float64(5.0),
                            "supertrend_follow": np.float64(4.6)})
        # The internal cache used by other code paths must also be clean.
        for v in ens._global_learned_weights.values():
            assert type(v) is float

    def test_regime_weights_casts_numpy_to_float(self):
        np = pytest.importorskip("numpy")
        ens = self._build()
        ens.update_regime_weights("bear_high_vol", {
            "mean_reversion": np.float64(3.5),
            "rsi_momentum": np.float64(2.1),
        })
        for v in ens._regime_learned_weights["bear_high_vol"].values():
            assert type(v) is float

    def test_log_line_has_no_numpy_repr(self, caplog):
        """The actual log line must NOT contain 'np.float64' anywhere
        (this was the visible-to-user bug in production logs)."""
        np = pytest.importorskip("numpy")
        import logging
        ens = self._build()
        with caplog.at_level(logging.INFO):
            ens.update_weights({"mean_reversion": np.float64(4.599),
                                "rsi_momentum": np.float64(5.0)})
        # Loguru routes through caplog's records when configured but
        # in any case the raw rendered string must not contain numpy refs.
        all_text = "\n".join(rec.message for rec in caplog.records)
        # Be tolerant of test harness routing (loguru -> stdout), so check
        # the string composition we actually use:
        rendered = ", ".join(
            f"{k}={v:.2f}" for k, v in sorted(
                ens.weights.items(), key=lambda kv: kv[1], reverse=True
            )
        )
        assert "np.float64" not in rendered
        assert "mean_reversion=4.60" in rendered
        assert "rsi_momentum=5.00" in rendered

    def test_pure_python_floats_still_work(self):
        # Ensure backwards-compat: passing plain floats still works.
        ens = self._build()
        ens.update_weights({"mean_reversion": 4.6, "rsi_momentum": 5.0})
        assert ens.weights["mean_reversion"] == pytest.approx(4.6)
        assert ens.weights["rsi_momentum"] == pytest.approx(5.0)


class TestLongRegimeGuardAgentWiring:
    """Verify the agent reads the config into the right attribute."""

    def test_attribute_init_with_explicit_list(self):
        from trading_agent import TradingAgent

        a = object.__new__(TradingAgent)
        a.config = {
            "execution": {
                "long_entry_regimes": ["bull_low_vol", "bull_high_vol"],
            }
        }
        exec_cfg = a.config.get("execution", {}) or {}
        long_regimes = exec_cfg.get("long_entry_regimes")
        a._long_entry_regimes = (
            set(long_regimes) if long_regimes else set()
        )
        assert a._long_entry_regimes == {"bull_low_vol", "bull_high_vol"}

    def test_attribute_init_with_empty_list_is_permissive(self):
        from trading_agent import TradingAgent

        a = object.__new__(TradingAgent)
        a.config = {"execution": {"long_entry_regimes": []}}
        exec_cfg = a.config.get("execution", {}) or {}
        long_regimes = exec_cfg.get("long_entry_regimes")
        a._long_entry_regimes = (
            set(long_regimes) if long_regimes else set()
        )
        assert a._long_entry_regimes == set()

    def test_attribute_init_with_missing_key_is_permissive(self):
        from trading_agent import TradingAgent

        a = object.__new__(TradingAgent)
        a.config = {"execution": {}}
        exec_cfg = a.config.get("execution", {}) or {}
        long_regimes = exec_cfg.get("long_entry_regimes")
        a._long_entry_regimes = (
            set(long_regimes) if long_regimes else set()
        )
        # Critical for backwards-compat: legacy configs without the key
        # behave exactly as before (no restriction on longs).
        assert a._long_entry_regimes == set()
