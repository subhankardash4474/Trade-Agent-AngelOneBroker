"""
Regression tests for the six trade-perspective fixes shipped 2026-04-30.

Each test class pins the exact behaviour of one fix so we catch regressions
if future refactors accidentally loosen these guardrails. Together they form
the profitability harness for the agent's production operation.

Fixes covered:
  1. Per-trade notional floor                         (trading_agent)
  2. Strategy-aware expected-profit / RR gate         (risk_manager)
  3. Minimum SL distance floor                        (risk_manager)
  4. Expanded NSE_SECTOR_MAP + per-stock UNKNOWN      (market_safety)
  5. TP ceiling clamp                                 (risk_manager)
  6. Strategy diversity monitor                       (trading_agent)
"""
from __future__ import annotations

import pytest

from core.market_safety import (
    NSE_SECTOR_MAP,
    _bucket_for,
    check_sector_exposure,
    get_sector,
)
from core.risk_manager import RiskManager


# ─────────────────────────────────────────────────────────────────────────
# Fix 2 & 3: strategy-aware RR gate + minimum SL distance
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def rm_strat_aware():
    """RM with all the new knobs enabled (matches production config.yaml)."""
    cfg = {
        "risk": {
            "min_profit_to_charges_ratio": 2.5,
            "min_absolute_reward_rs": 20.0,
            "min_stop_loss_pct": 1.2,
            "max_tp_to_sl_multiple": 2.5,
            "max_tp_pct": 2.5,
            "min_rr_by_strategy": {
                "mean_reversion": 0.6,
                "rsi_momentum": 1.0,
                "supertrend_follow": 1.3,
            },
            "atr_stop_multiplier": 1.5,
            "stop_loss_pct": 2.0,
        }
    }
    return RiskManager(cfg, initial_balance=50_000.0)


class TestStrategyAwareRRGate:
    """Fix 2: mean-reversion accepts lower RR; trend-followers demand higher."""

    def test_mean_reversion_accepts_rr_0p8(self, rm_strat_aware):
        # RR = 0.8, reward Rs 80, well above absolute floor. Should PASS for MR.
        ok, reason = rm_strat_aware.is_trade_worth_taking(
            entry_price=100.0,
            take_profit=100.8,    # reward 0.8
            stop_loss=99.0,        # risk 1.0 → RR=0.8
            quantity=100,
            side="BUY",
            strategy="mean_reversion",
        )
        assert ok, f"MR with RR=0.8 should pass (0.6 floor), got {reason}"

    def test_mean_reversion_rejects_rr_0p4(self, rm_strat_aware):
        # RR = 0.4, below the 0.6 floor. Should fail.
        ok, reason = rm_strat_aware.is_trade_worth_taking(
            entry_price=100.0,
            take_profit=100.4,
            stop_loss=99.0,
            quantity=100,
            strategy="mean_reversion",
        )
        assert not ok
        assert "poor_rr" in reason

    def test_supertrend_requires_higher_rr(self, rm_strat_aware):
        # RR = 1.2 should fail for supertrend (needs 1.3) but pass for default.
        ok, reason = rm_strat_aware.is_trade_worth_taking(
            entry_price=100.0,
            take_profit=101.2,
            stop_loss=99.0,
            quantity=100,
            strategy="supertrend_follow",
        )
        assert not ok
        assert "supertrend_follow" in reason

    def test_unknown_strategy_uses_default(self, rm_strat_aware):
        # Default RR floor is 1.2. RR=1.0 should fail.
        ok, reason = rm_strat_aware.is_trade_worth_taking(
            entry_price=100.0,
            take_profit=101.0,
            stop_loss=99.0,
            quantity=100,
            strategy="nonexistent_strategy",
        )
        assert not ok
        assert "poor_rr" in reason

    def test_min_rr_for_lookup(self, rm_strat_aware):
        assert rm_strat_aware.min_rr_for("mean_reversion") == 0.6
        assert rm_strat_aware.min_rr_for("supertrend_follow") == 1.3
        assert rm_strat_aware.min_rr_for(None) == 1.2  # default fallback
        assert rm_strat_aware.min_rr_for("unknown") == 1.2


class TestMinStopLossDistance:
    """Fix 3: tight ATR stops get widened to the minimum distance floor."""

    def test_tight_atr_gets_widened_long(self, rm_strat_aware):
        # ATR of Rs 0.30 at entry Rs 100 → 0.45 % stop. Floor is 1.2 %.
        sl = rm_strat_aware.get_stop_loss(entry_price=100.0, side="BUY", atr=0.3)
        # Floor at 1.2 % means SL <= 98.80
        assert sl <= 98.80 + 0.01, f"Expected SL <= 98.80, got {sl}"

    def test_tight_atr_gets_widened_short(self, rm_strat_aware):
        sl = rm_strat_aware.get_stop_loss(entry_price=100.0, side="SELL", atr=0.3)
        assert sl >= 101.20 - 0.01, f"Expected SL >= 101.20, got {sl}"

    def test_wide_atr_respected(self, rm_strat_aware):
        # ATR of Rs 2.0 at entry Rs 100 → 3.0 % stop. Floor shouldn't tighten it.
        sl = rm_strat_aware.get_stop_loss(entry_price=100.0, side="BUY", atr=2.0)
        assert sl == 97.0  # 2.0 * 1.5 = 3.0 → 97.0

    def test_floor_disabled_when_zero(self):
        cfg = {"risk": {"min_stop_loss_pct": 0.0, "atr_stop_multiplier": 1.5}}
        rm = RiskManager(cfg, 10_000.0)
        sl = rm.get_stop_loss(entry_price=100.0, side="BUY", atr=0.3)
        assert sl == 99.55  # 100 - 0.45


# ─────────────────────────────────────────────────────────────────────────
# Fix 5: TP ceiling clamp
# ─────────────────────────────────────────────────────────────────────────

class TestTakeProfitClamp:
    """Fix 5: unrealistic TPs get pulled back inside max_tp caps."""

    def test_tp_clamped_to_2p5x_sl(self, rm_strat_aware):
        # SL distance = 1.2 % = Rs 1.20. Cap = 2.5x = Rs 3.00 → TP = 103.0.
        # Proposed TP = 108 (8 % move) → should be pulled to <=103.0.
        clamped = rm_strat_aware.clamp_take_profit(
            entry_price=100.0, take_profit=108.0, side="BUY", atr=None,
        )
        # min_stop_loss_pct floor is 1.2, so SL distance = 1.2 → 2.5x = 3.0
        # Also max_tp_pct 2.5 → 2.5 → most restrictive
        assert clamped <= 102.6, f"Expected clamped TP ≤ 102.6, got {clamped}"

    def test_tp_clamped_to_pct_cap_short(self, rm_strat_aware):
        clamped = rm_strat_aware.clamp_take_profit(
            entry_price=200.0, take_profit=160.0, side="SELL", atr=None,
        )
        # 2.5 % cap on Rs 200 = Rs 5 → TP = 195.0
        assert clamped >= 194.9, f"Expected TP >= 195.0, got {clamped}"

    def test_tp_not_widened(self, rm_strat_aware):
        # If the TP is already tighter than the cap, leave it alone.
        clamped = rm_strat_aware.clamp_take_profit(
            entry_price=100.0, take_profit=100.5, side="BUY", atr=None,
        )
        assert clamped == 100.5

    def test_clamp_disabled_when_both_zero(self):
        cfg = {"risk": {"max_tp_to_sl_multiple": 0.0, "max_tp_pct": 0.0}}
        rm = RiskManager(cfg, 10_000.0)
        assert rm.clamp_take_profit(100.0, 150.0, "BUY", atr=None) == 150.0

    def test_get_take_profit_applies_clamp(self, rm_strat_aware):
        # Wide ATR + bull_low_vol regime would otherwise push TP way out.
        # Ensure caller-side clamp still fires inside get_take_profit.
        tp = rm_strat_aware.get_take_profit(
            entry_price=100.0, side="BUY", atr=5.0, regime="bull_low_vol",
        )
        # Raw TP would be 100 + 3.0 * 1.5 * 5 = 122.5. Cap at 2.5 % → 102.5.
        assert tp <= 102.6


# ─────────────────────────────────────────────────────────────────────────
# Fix 4: sector map + per-symbol UNKNOWN bucket
# ─────────────────────────────────────────────────────────────────────────

class TestSectorMapExpansion:
    """Fix 4a: scanner mid-caps must now be classified correctly."""

    @pytest.mark.parametrize("symbol,expected", [
        ("UNIONBANK", "Banks"),
        ("INDIANB", "Banks"),
        ("BANDHANBNK", "Banks"),
        ("RBLBANK", "Banks"),
        ("RPOWER", "Power"),
        ("SUZLON", "Power"),
        ("NLCINDIA", "Power"),
        ("TTML", "Telecom"),
        ("CROMPTON", "Consumer Durables"),
        ("DELTACORP", "Consumer"),
        ("TATACHEM", "Chemicals"),
        ("DLF", "Realty"),
    ])
    def test_common_midcap_classified(self, symbol, expected):
        assert get_sector(symbol) == expected, (
            f"{symbol} must map to {expected} (was UNKNOWN, blocking trades)"
        )

    def test_sector_map_size_floor(self):
        # Hard floor — if someone deletes most of it by accident we want to fail fast.
        assert len(NSE_SECTOR_MAP) >= 200, (
            f"Sector map shrunk to {len(NSE_SECTOR_MAP)}; "
            f"expansion to cover mid-caps must not be reverted."
        )


class TestUnknownPerSymbolBucket:
    """Fix 4b: unmapped symbols get private buckets instead of one shared pool."""

    def test_bucket_known_symbol(self):
        assert _bucket_for("HDFCBANK", unknown_per_symbol=True) == "Banks"
        assert _bucket_for("HDFCBANK", unknown_per_symbol=False) == "Banks"

    def test_bucket_unknown_per_symbol(self):
        # Use a deliberately synthetic symbol guaranteed to be unclassified.
        assert _bucket_for("ZZZNEWCO", unknown_per_symbol=True) == "UNKNOWN:ZZZNEWCO"
        assert _bucket_for("ZZZNEWCO", unknown_per_symbol=False) == "UNKNOWN"

    def test_unknown_pool_no_longer_blocks_others(self):
        """Core bug fix: one open UNKNOWN position shouldn't cap other UNKNOWNs."""
        positions = {"MYSTERY1": 3_000.0}   # unmapped, open ~30 % of Rs 10k
        # New unmapped symbol MYSTERY2 trying to open Rs 3k more.
        safe, _ = check_sector_exposure(
            symbol="MYSTERY2",
            current_positions_by_symbol=positions,
            additional_cost=3_000.0,
            total_equity=10_000.0,
            max_sector_exposure_pct=40.0,
            unknown_per_symbol=True,
        )
        assert safe, (
            "With per-symbol UNKNOWN bucketing, one 30%-exposure position "
            "must NOT block an unrelated unclassified name."
        )

    def test_legacy_behaviour_still_lumps_unknowns(self):
        """Without the flag, the old pathological behaviour remains (for parity)."""
        positions = {"MYSTERY1": 3_000.0}
        safe, reason = check_sector_exposure(
            symbol="MYSTERY2",
            current_positions_by_symbol=positions,
            additional_cost=3_000.0,
            total_equity=10_000.0,
            max_sector_exposure_pct=40.0,
            unknown_per_symbol=False,
        )
        # Combined: 6k / 10k = 60 % > 40 % → blocked
        assert not safe
        assert "UNKNOWN" in reason

    def test_same_symbol_still_blocked(self):
        """Per-symbol bucketing must NOT relax single-symbol limits."""
        positions = {"ZZZNEWCO": 3_500.0}
        safe, reason = check_sector_exposure(
            symbol="ZZZNEWCO",  # same symbol again
            current_positions_by_symbol=positions,
            additional_cost=3_500.0,
            total_equity=10_000.0,
            max_sector_exposure_pct=40.0,
            unknown_per_symbol=True,
        )
        # 7k / 10k = 70 % in its own UNKNOWN:ZZZNEWCO bucket → blocked
        assert not safe
        assert "UNKNOWN/ZZZNEWCO" in reason or "ZZZNEWCO" in reason


# ─────────────────────────────────────────────────────────────────────────
# Fix 1 & 6: trading-agent level fixes (loaded via config, not pure functions)
# ─────────────────────────────────────────────────────────────────────────

class TestConfigContract:
    """Make sure the shipped config.yaml actually sets every new knob.

    These are the settings the audit is counting on being live. If someone
    reverts the config without updating the tests, this catches it.
    """

    def test_config_loads_all_new_risk_keys(self):
        import yaml
        from pathlib import Path

        cfg_path = Path(__file__).parent.parent / "config.yaml"
        with cfg_path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        risk = cfg.get("risk", {})
        # 2026-05-04: lowered from Rs 5k assertion to Rs 2.5k now that
        # cap-aware scaling clips the floor to the symbol-exposure cap on
        # small books. Anything between Rs 2.5k and Rs 10k is acceptable —
        # the cap-aware sizer self-regulates.
        assert 2_500 <= risk.get("min_trade_notional", 0) <= 10_000, (
            "min_trade_notional should be in Rs 2.5k - Rs 10k range "
            "(cap-aware sizer handles small-book edge cases)."
        )
        assert risk.get("min_stop_loss_pct", 0) >= 1.0, (
            "min_stop_loss_pct must be >= 1 % to survive intraday noise."
        )
        assert risk.get("max_tp_to_sl_multiple", 0) > 0, (
            "TP-to-SL multiple cap must be set."
        )
        assert risk.get("max_tp_pct", 0) > 0, (
            "Absolute TP % cap must be set."
        )
        mr = (risk.get("min_rr_by_strategy") or {}).get("mean_reversion")
        assert mr is not None and mr < 1.0, (
            "Mean-reversion RR floor must be below 1.0 (high WR strategy)."
        )
        st = (risk.get("min_rr_by_strategy") or {}).get("supertrend_follow")
        assert st is not None and st >= 1.2, (
            "Supertrend RR floor must be >= 1.2 (low WR strategy)."
        )
        assert risk.get("unknown_sector_per_symbol") is True, (
            "UNKNOWN bucket must be per-symbol — otherwise mid-caps get logjammed."
        )


class TestStrategyDiversityMonitor:
    """Fix 6: monoculture detection in the EOD report."""

    def test_monoculture_flag_triggers(self):
        from trading_agent import TradingAgent

        # Stub out the __init__ to avoid full app boot — we only need the
        # helper method and the tally dict.
        agent = TradingAgent.__new__(TradingAgent)
        agent._strategy_contrib_today = {
            "mean_reversion": 9.0,
            "rsi_momentum": 1.0,
        }
        out = agent._build_strategy_mix_report()
        assert "Strategy mix today" in out
        assert "mean_reversion" in out
        assert "monoculture" in out.lower()

    def test_balanced_mix_no_warning(self):
        from trading_agent import TradingAgent
        agent = TradingAgent.__new__(TradingAgent)
        agent._strategy_contrib_today = {
            "mean_reversion": 3.0,
            "rsi_momentum": 2.0,
            "supertrend_follow": 2.0,
            "vwap_bounce": 3.0,
        }
        out = agent._build_strategy_mix_report()
        assert "monoculture" not in out.lower()

    def test_empty_mix_returns_empty(self):
        from trading_agent import TradingAgent
        agent = TradingAgent.__new__(TradingAgent)
        agent._strategy_contrib_today = {}
        out = agent._build_strategy_mix_report()
        assert out == ""
