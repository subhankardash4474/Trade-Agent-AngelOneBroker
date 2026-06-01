"""Contract tests for ModeDispatcher skeleton (charter §7.2, Phase 8).

These pin the SKELETON's behaviour — schema validation, capital gate,
allocation gate, module resolution, active_modes ordering, and the
stub-routing structural rule (backtest_only modes never route).

The full route_order / kill_check / paper-broker tests land with the
hard-cutover commit (Q5) which actually wires PaperBroker.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from trader.mode_dispatcher import (
    AllocationGateError,
    CapitalGateError,
    DictCapitalProvider,
    ModeConfigError,
    ModeDispatcher,
    ModeRoutingError,
    ModeSpec,
    OVERRIDE_RUIN_RISK,
)


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


def _fake_resolver(known: dict):
    """Build a module_resolver that returns dummy modules / classes
    instead of hitting importlib (decouples tests from package layout).
    """
    def resolve(path: str):
        if path not in known:
            raise ModuleNotFoundError(f"fake_resolver: no entry for {path!r}")
        return known[path]
    return resolve


def _minimal_modes_config(**overrides):
    """A working baseline config with one Mode A entry, enabled paper."""
    cfg = {
        "strategies": {
            "modes": {
                "swing_cash_v27": {
                    "enabled": True,
                    "mode": "paper",
                    "capital_allocation_pct": 60,
                    "runtime": "swing_cnc",
                    "backtester_variant": "cross_asset_trend_v27",
                    "signal_module": "packages.strategies.swing_cash.cross_asset_trend_v27",
                    "cost_model": "packages.core.charges:CashCNCCharges",
                    "paper_to_live_threshold": {
                        "capital_inr": 300_000,
                    },
                    "kill_criteria": {
                        "paper": {"rolling_30d_dd_max_pct": 8.0},
                    },
                },
            },
        },
        "mode_router": {
            "max_capital_allocation_pct": 100,
            "override_capital_gate": "",
        },
    }
    for k, v in overrides.items():
        cfg[k] = v
    return cfg


_KNOWN_REFS = {
    "packages.strategies.swing_cash.cross_asset_trend_v27": SimpleNamespace(
        CrossAssetTrendV27=object,
    ),
    "packages.core.charges": SimpleNamespace(
        CashCNCCharges=object,
        compute_one_leg=lambda *a, **k: 0.0,
    ),
}


# ─────────────────────────────────────────────────────────────────────
# Schema validation
# ─────────────────────────────────────────────────────────────────────


class TestSchemaValidation:
    def test_minimal_config_loads(self):
        d = ModeDispatcher(
            _minimal_modes_config(),
            DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        assert len(d.active_modes()) == 1

    def test_missing_strategies_key_raises(self):
        with pytest.raises(ModeConfigError, match="strategies"):
            ModeDispatcher(
                {"mode_router": {}},
                DictCapitalProvider(_cash_inr=500_000),
                module_resolver=_fake_resolver(_KNOWN_REFS),
            )

    def test_missing_modes_block_raises(self):
        with pytest.raises(ModeConfigError, match="strategies.modes"):
            ModeDispatcher(
                {"strategies": {}, "mode_router": {}},
                DictCapitalProvider(_cash_inr=500_000),
                module_resolver=_fake_resolver(_KNOWN_REFS),
            )

    def test_invalid_mode_type_raises(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["swing_cash_v27"]["mode"] = "shadow"
        with pytest.raises(ModeConfigError, match=r"mode=.+not in"):
            ModeDispatcher(
                cfg, DictCapitalProvider(_cash_inr=500_000),
                module_resolver=_fake_resolver(_KNOWN_REFS),
            )

    def test_invalid_runtime_raises(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["swing_cash_v27"]["runtime"] = "highfreq_fx"
        with pytest.raises(ModeConfigError, match=r"runtime="):
            ModeDispatcher(
                cfg, DictCapitalProvider(_cash_inr=500_000),
                module_resolver=_fake_resolver(_KNOWN_REFS),
            )

    def test_enabled_mode_missing_signal_module_raises(self):
        cfg = _minimal_modes_config()
        del cfg["strategies"]["modes"]["swing_cash_v27"]["signal_module"]
        with pytest.raises(ModeConfigError, match="signal_module"):
            ModeDispatcher(
                cfg, DictCapitalProvider(_cash_inr=500_000),
                module_resolver=_fake_resolver(_KNOWN_REFS),
            )

    def test_disabled_legacy_mode_can_skip_signal_module(self):
        """Charter §2.1 swing_combined_shorts_legacy is enabled=False
        and intentionally has no signal_module — it's a config-only
        placeholder for DB row resolution. Schema must accept this."""
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["swing_combined_shorts_legacy"] = {
            "enabled": False,
            "mode": "paper",
            "frozen_until": "never",
            "reason": "V25/V26 wound down 2026-06-05",
        }
        d = ModeDispatcher(
            cfg, DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        assert "swing_combined_shorts_legacy" in {s.name for s in d.active_modes() + list(d._modes.values())}


# ─────────────────────────────────────────────────────────────────────
# Capital gate (charter §2.3 — the "no, you cannot" layer)
# ─────────────────────────────────────────────────────────────────────


class TestCapitalGate:
    def test_paper_mode_not_subject_to_capital_gate(self):
        """120k cash, mode=paper, gate=300k → MUST LOAD."""
        d = ModeDispatcher(
            _minimal_modes_config(),
            DictCapitalProvider(_cash_inr=120_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        assert d.active_modes()[0].mode == "paper"

    def test_live_mode_below_gate_raises(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["swing_cash_v27"]["mode"] = "live"
        with pytest.raises(CapitalGateError, match=r"300,000"):
            ModeDispatcher(
                cfg, DictCapitalProvider(_cash_inr=120_000),
                module_resolver=_fake_resolver(_KNOWN_REFS),
            )

    def test_live_mode_at_or_above_gate_loads(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["swing_cash_v27"]["mode"] = "live"
        d = ModeDispatcher(
            cfg, DictCapitalProvider(_cash_inr=300_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        assert d.active_modes()[0].mode == "live"

    def test_override_string_must_be_exact_verbatim(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["swing_cash_v27"]["mode"] = "live"
        cfg["mode_router"]["override_capital_gate"] = "i accept ruin risk"  # wrong case
        with pytest.raises(CapitalGateError):
            ModeDispatcher(
                cfg, DictCapitalProvider(_cash_inr=120_000),
                module_resolver=_fake_resolver(_KNOWN_REFS),
            )

    def test_override_string_verbatim_bypasses_gate(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["swing_cash_v27"]["mode"] = "live"
        cfg["mode_router"]["override_capital_gate"] = OVERRIDE_RUIN_RISK
        d = ModeDispatcher(
            cfg, DictCapitalProvider(_cash_inr=120_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        assert d.active_modes()[0].mode == "live"

    def test_disabled_live_mode_does_not_trip_gate(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["swing_cash_v27"]["mode"] = "live"
        cfg["strategies"]["modes"]["swing_cash_v27"]["enabled"] = False
        # Should NOT raise even though capital is well below gate.
        ModeDispatcher(
            cfg, DictCapitalProvider(_cash_inr=120_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )


# ─────────────────────────────────────────────────────────────────────
# Allocation sum gate
# ─────────────────────────────────────────────────────────────────────


class TestAllocationGate:
    def test_sum_within_max_loads(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["mode_b"] = dict(
            cfg["strategies"]["modes"]["swing_cash_v27"]
        )
        cfg["strategies"]["modes"]["mode_b"]["capital_allocation_pct"] = 40  # 60+40=100
        d = ModeDispatcher(
            cfg, DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        assert len(d.active_modes()) == 2

    def test_sum_over_max_raises(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["mode_b"] = dict(
            cfg["strategies"]["modes"]["swing_cash_v27"]
        )
        cfg["strategies"]["modes"]["mode_b"]["capital_allocation_pct"] = 50  # 60+50=110
        with pytest.raises(AllocationGateError, match=r"110"):
            ModeDispatcher(
                cfg, DictCapitalProvider(_cash_inr=500_000),
                module_resolver=_fake_resolver(_KNOWN_REFS),
            )

    def test_backtest_only_modes_dont_count_in_allocation_sum(self):
        """Charter §2.1: backtest_only modes don't consume capital."""
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["mode_d"] = dict(
            cfg["strategies"]["modes"]["swing_cash_v27"]
        )
        cfg["strategies"]["modes"]["mode_d"]["capital_allocation_pct"] = 100
        cfg["strategies"]["modes"]["mode_d"]["mode"] = "backtest_only"
        # 60 (paper Mode A) + 100 (backtest_only Mode D) — gate ignores Mode D.
        d = ModeDispatcher(
            cfg, DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        assert len(d.active_modes()) == 2


# ─────────────────────────────────────────────────────────────────────
# Module resolution
# ─────────────────────────────────────────────────────────────────────


class TestModuleResolution:
    def test_resolves_module_only_reference(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["swing_cash_v27"]["cost_model"] = (
            "packages.core.charges"
        )
        d = ModeDispatcher(
            cfg, DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        cm = d.cost_model_for("swing_cash_v27")
        assert hasattr(cm, "CashCNCCharges")

    def test_resolves_module_attribute_reference(self):
        d = ModeDispatcher(
            _minimal_modes_config(),
            DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        cm_class = d.cost_model_for("swing_cash_v27")
        assert cm_class is _KNOWN_REFS["packages.core.charges"].CashCNCCharges

    def test_unknown_module_raises(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["swing_cash_v27"]["cost_model"] = (
            "packages.nonexistent:Thing"
        )
        with pytest.raises(ModuleNotFoundError):
            ModeDispatcher(
                cfg, DictCapitalProvider(_cash_inr=500_000),
                module_resolver=_fake_resolver(_KNOWN_REFS),
            )

    def test_unknown_attribute_raises_modeconfigerror(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["swing_cash_v27"]["cost_model"] = (
            "packages.core.charges:NotARealClass"
        )
        with pytest.raises(ModeConfigError, match="NotARealClass"):
            ModeDispatcher(
                cfg, DictCapitalProvider(_cash_inr=500_000),
                module_resolver=_fake_resolver(_KNOWN_REFS),
            )


# ─────────────────────────────────────────────────────────────────────
# Routing stub structural rules
# ─────────────────────────────────────────────────────────────────────


class TestRouteOrderStub:
    """The skeleton enforces structure but raises NotImplementedError
    for the rest. The full route_order lands in the hard-cutover commit."""

    def test_backtest_only_mode_refuses_routing(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["swing_cash_v27"]["mode"] = "backtest_only"
        d = ModeDispatcher(
            cfg, DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        with pytest.raises(ModeRoutingError, match="backtest_only"):
            d.route_order(signal=object(), mode_name="swing_cash_v27")

    def test_disabled_mode_refuses_routing(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["swing_cash_v27"]["enabled"] = False
        d = ModeDispatcher(
            cfg, DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        with pytest.raises(ModeRoutingError, match="disabled"):
            d.route_order(signal=object(), mode_name="swing_cash_v27")

    def test_paper_mode_raises_not_implemented_until_cutover(self):
        d = ModeDispatcher(
            _minimal_modes_config(),
            DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        with pytest.raises(NotImplementedError, match="hard-cutover"):
            d.route_order(signal=object(), mode_name="swing_cash_v27")

    def test_unknown_mode_raises_keyerror(self):
        d = ModeDispatcher(
            _minimal_modes_config(),
            DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        with pytest.raises(KeyError, match="nonexistent"):
            d.route_order(signal=object(), mode_name="nonexistent")


# ─────────────────────────────────────────────────────────────────────
# kill_check stub structural rules
# ─────────────────────────────────────────────────────────────────────


class TestKillCheckStub:
    def test_unknown_window_raises(self):
        d = ModeDispatcher(
            _minimal_modes_config(),
            DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        with pytest.raises(ValueError, match="kill_check window"):
            d.kill_check("swing_cash_v27", window="quarterly")

    def test_missing_kill_criteria_for_window_raises(self):
        cfg = _minimal_modes_config()
        # config has 'paper' kill_criteria only; ask for 'live'
        d = ModeDispatcher(
            cfg, DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        with pytest.raises(ValueError, match="no kill_criteria"):
            d.kill_check("swing_cash_v27", window="live")

    def test_implemented_window_raises_not_implemented(self):
        d = ModeDispatcher(
            _minimal_modes_config(),
            DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        with pytest.raises(NotImplementedError, match="hard-cutover"):
            d.kill_check("swing_cash_v27", window="paper")


# ─────────────────────────────────────────────────────────────────────
# active_modes ordering + disable_mode
# ─────────────────────────────────────────────────────────────────────


class TestActiveModesOrdering:
    def test_active_modes_preserves_insertion_order(self):
        cfg = _minimal_modes_config()
        cfg["strategies"]["modes"]["mode_z"] = dict(
            cfg["strategies"]["modes"]["swing_cash_v27"]
        )
        cfg["strategies"]["modes"]["mode_z"]["capital_allocation_pct"] = 20
        cfg["strategies"]["modes"]["mode_a2"] = dict(
            cfg["strategies"]["modes"]["swing_cash_v27"]
        )
        cfg["strategies"]["modes"]["mode_a2"]["capital_allocation_pct"] = 10
        # 60+20+10 = 90 ≤ 100 ✓
        d = ModeDispatcher(
            cfg, DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        names = [s.name for s in d.active_modes()]
        assert names == ["swing_cash_v27", "mode_z", "mode_a2"]

    def test_active_modes_excludes_disabled(self):
        d = ModeDispatcher(
            _minimal_modes_config(),
            DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        assert len(d.active_modes()) == 1
        d.disable_mode("swing_cash_v27", reason="operator stop")
        assert len(d.active_modes()) == 0


class TestDisableMode:
    def test_disable_unknown_mode_raises_keyerror(self):
        d = ModeDispatcher(
            _minimal_modes_config(),
            DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        with pytest.raises(KeyError, match="unknown_mode"):
            d.disable_mode("unknown_mode", reason="test")

    def test_disable_logs_critical_audit_event(self, caplog):
        import logging
        caplog.set_level(logging.CRITICAL, logger="trader.mode_dispatcher")
        d = ModeDispatcher(
            _minimal_modes_config(),
            DictCapitalProvider(_cash_inr=500_000),
            module_resolver=_fake_resolver(_KNOWN_REFS),
        )
        d.disable_mode("swing_cash_v27", reason="kill criteria tripped")
        critical_msgs = [
            r.message for r in caplog.records
            if r.levelno == logging.CRITICAL
        ]
        assert any("MODE-DISABLED" in m for m in critical_msgs)
        assert any("kill criteria tripped" in m for m in critical_msgs)


# ─────────────────────────────────────────────────────────────────────
# ModeSpec.from_dict direct tests
# ─────────────────────────────────────────────────────────────────────


class TestModeSpecParse:
    def test_round_trip_basic(self):
        d = {
            "enabled": True,
            "mode": "paper",
            "capital_allocation_pct": 60,
            "runtime": "swing_cnc",
            "backtester_variant": "v27",
            "signal_module": "a.b",
            "cost_model": "c.d",
        }
        spec = ModeSpec.from_dict("name1", d)
        assert spec.name == "name1"
        assert spec.enabled is True
        assert spec.mode == "paper"
        assert spec.capital_allocation_pct == 60.0

    def test_missing_mode_raises(self):
        with pytest.raises(ModeConfigError, match="mode"):
            ModeSpec.from_dict("x", {"enabled": True})
