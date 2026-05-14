"""Unit tests for ``run_daemon._deep_merge`` and the ``--config-overlay``
+ ``--live`` plumbing added 2026-05-14 for Stage 3 live-basket runs.

Background
----------
Stage 3 (see ``docs/e2e_broker_test_plan.md``) demands a controlled
5-stock live basket with caps that differ from the day-to-day paper
config (5 instruments, Rs 5k cap, max_open_positions=5,
trading_hours 09:30-12:30). Pre-2026-05-14 the only way to express
this was either editing ``config.yaml`` in-place (risky -- the edit
gets forgotten on the way back to paper) or duplicating the entire
file (drifts).

The fix:
  * ``--config-overlay PATH`` -- deep-merge a small YAML over the
    base config.
  * ``--live`` -- flip ``broker.mode`` to "live" at runtime.
  * ``--paper`` -- still wins if both are set (defensive).

These tests pin the merge semantics and the mode-resolution order.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import run_daemon  # noqa: E402


# ---------------------------------------------------------------------------
# _deep_merge semantics
# ---------------------------------------------------------------------------

class TestDeepMerge:
    def test_scalar_in_overlay_replaces_base(self):
        base = {"a": 1, "b": 2}
        overlay = {"b": 99}
        assert run_daemon._deep_merge(base, overlay) == {"a": 1, "b": 99}

    def test_new_key_in_overlay_is_added(self):
        base = {"a": 1}
        overlay = {"b": 2}
        assert run_daemon._deep_merge(base, overlay) == {"a": 1, "b": 2}

    def test_nested_dicts_merge_recursively(self):
        base = {"market": {"exchange": "NSE", "instruments": []}}
        overlay = {"market": {"instruments": ["RELIANCE"]}}
        merged = run_daemon._deep_merge(base, overlay)
        # exchange (base-only) preserved, instruments (overlay) replaces []
        assert merged == {"market": {"exchange": "NSE",
                                     "instruments": ["RELIANCE"]}}

    def test_list_in_overlay_fully_replaces_base_list(self):
        # Critical: we DO NOT want list concatenation. If Stage 3 says
        # `instruments: [RELIANCE, HDFCBANK]`, the live universe must be
        # exactly those 2 names -- not "those 2 plus whatever the base
        # config had".
        base = {"market": {"instruments": ["TATA", "WIPRO", "SBIN"]}}
        overlay = {"market": {"instruments": ["RELIANCE", "HDFCBANK"]}}
        merged = run_daemon._deep_merge(base, overlay)
        assert merged["market"]["instruments"] == ["RELIANCE", "HDFCBANK"]

    def test_dict_replaces_scalar(self):
        # If the base value was a scalar/None and the overlay puts a dict
        # there, the dict wins (no attempt to merge into a non-dict).
        base = {"strategies": None}
        overlay = {"strategies": {"active": ["rsi"]}}
        merged = run_daemon._deep_merge(base, overlay)
        assert merged == {"strategies": {"active": ["rsi"]}}

    def test_scalar_replaces_dict(self):
        # Symmetric edge case. Real configs shouldn't do this but we
        # don't want a TypeError if they do.
        base = {"risk": {"max_open_positions": 12}}
        overlay = {"risk": 0}
        merged = run_daemon._deep_merge(base, overlay)
        assert merged == {"risk": 0}

    def test_empty_overlay_is_noop(self):
        base = {"a": 1, "b": {"c": 2}}
        assert run_daemon._deep_merge(base, {}) == base
        assert run_daemon._deep_merge(base, None) == base

    def test_inputs_not_mutated(self):
        base = {"market": {"instruments": ["ORIG"]}}
        overlay = {"market": {"instruments": ["NEW"]}}
        merged = run_daemon._deep_merge(base, overlay)
        # Mutate the merged result and verify it doesn't leak back
        merged["market"]["instruments"].append("MUTATED")
        assert base == {"market": {"instruments": ["ORIG"]}}
        assert overlay == {"market": {"instruments": ["NEW"]}}

    def test_stage3_realistic_overlay(self):
        # End-to-end smoke: real Stage 3 overlay merged over a slice of
        # the production config.
        base = {
            "market": {"exchange": "NSE",
                       "instruments": [],
                       "trading_hours": {"start": "09:15", "end": "15:30"}},
            "scanner": {"enabled": True, "top_n": 200},
            "capital": {"initial_balance": 100000.0},
            "risk": {"max_open_positions": 12, "max_trades_per_day": 25},
        }
        overlay = yaml.safe_load("""
market:
  instruments: [RELIANCE, HDFCBANK, INFY, TCS, ICICIBANK]
  trading_hours:
    start: "09:30"
    end: "12:30"
scanner:
  enabled: false
capital:
  initial_balance: 5000.0
risk:
  max_open_positions: 5
  max_trades_per_day: 5
""")
        merged = run_daemon._deep_merge(base, overlay)

        assert merged["market"]["exchange"] == "NSE"  # base preserved
        assert merged["market"]["instruments"] == \
            ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"]
        assert merged["market"]["trading_hours"] == \
            {"start": "09:30", "end": "12:30"}  # nested dict merged
        assert merged["scanner"]["enabled"] is False
        assert merged["scanner"]["top_n"] == 200  # base-only field kept
        assert merged["capital"]["initial_balance"] == 5000.0
        assert merged["risk"]["max_open_positions"] == 5
        assert merged["risk"]["max_trades_per_day"] == 5


# ---------------------------------------------------------------------------
# run_once: overlay + mode resolution
# ---------------------------------------------------------------------------

def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


class TestRunOnceConfigOverlay:
    """``run_once`` is a thin shim around ``TradingAgent`` -- we mock the
    agent to capture exactly what config it would have received."""

    @pytest.fixture
    def base_cfg_path(self, tmp_path):
        cfg = {
            "broker": {"mode": "paper"},
            "market": {"exchange": "NSE", "instruments": []},
            "risk": {"max_open_positions": 12},
            "logging": {"log_dir": str(tmp_path / "logs")},
        }
        p = tmp_path / "config.yaml"
        _write_yaml(p, cfg)
        return p

    @pytest.fixture
    def overlay_cfg_path(self, tmp_path):
        overlay = {
            "market": {"instruments": ["RELIANCE"]},
            "risk": {"max_open_positions": 5},
        }
        p = tmp_path / "stage3.yaml"
        _write_yaml(p, overlay)
        return p

    def _capture_config(self, base_path, overlay_path, *,
                        paper=False, live=False):
        """Drive ``run_once`` and return the config dict the
        TradingAgent constructor was called with.

        ``TradingAgent`` is imported lazily inside ``run_once`` (via
        ``from trading_agent import TradingAgent``) so we have to patch
        it on the source module, not on ``run_daemon``.
        """
        captured = {}

        class _StubAgent:
            def __init__(self, *a, **kw):
                # run_daemon passes the merged dict as kw["config"].
                # config_path is also passed (legacy compat) but we
                # care about the dict.
                captured["config"] = kw.get("config")
                captured["config_path"] = kw.get("config_path")

            def run(self, *a, **kw):
                # immediately exit so the wrapper doesn't loop.
                # ``poll_interval`` etc. arrive as kwargs from run_daemon --
                # we don't care, just consume.
                return

        # Pre-import the modules ``run_once`` will lazy-import so we can
        # patch the symbols it'll resolve.
        import trading_agent as _ta
        import main as _main

        with patch.object(_ta, "TradingAgent", _StubAgent), \
             patch.object(_main, "connect_angelone", return_value=None):
            run_daemon.run_once(
                str(base_path),
                paper=paper,
                interval=60,
                dashboard=False,
                live=live,
                config_overlay=str(overlay_path) if overlay_path else None,
            )
        return captured["config"]

    def test_no_overlay_passes_base_config_through(self, base_cfg_path):
        cfg = self._capture_config(base_cfg_path, None, paper=True)
        assert cfg["market"]["instruments"] == []
        assert cfg["risk"]["max_open_positions"] == 12

    def test_overlay_applied_before_mode_override(self, base_cfg_path,
                                                  overlay_cfg_path):
        cfg = self._capture_config(base_cfg_path, overlay_cfg_path, paper=True)
        # Overlay deltas visible
        assert cfg["market"]["instruments"] == ["RELIANCE"]
        assert cfg["risk"]["max_open_positions"] == 5
        # Mode forced to paper by --paper despite overlay being silent on mode
        assert cfg["broker"]["mode"] == "paper"

    def test_live_flag_flips_broker_mode(self, base_cfg_path):
        cfg = self._capture_config(base_cfg_path, None, live=True)
        assert cfg["broker"]["mode"] == "live"

    def test_paper_wins_over_live_when_both_set(self, base_cfg_path):
        # We don't expect this combo from real CLI usage (argparse top-level
        # guards it), but ``run_once`` is a callable too -- belt-and-braces.
        cfg = self._capture_config(base_cfg_path, None, paper=True, live=True)
        assert cfg["broker"]["mode"] == "paper"

    def test_neither_flag_preserves_yaml_mode(self, base_cfg_path):
        # base YAML says "paper" -- with no override, it stays paper.
        cfg = self._capture_config(base_cfg_path, None,
                                   paper=False, live=False)
        assert cfg["broker"]["mode"] == "paper"

    def test_overlay_can_set_mode_when_no_cli_override(self, tmp_path):
        # Edge case: overlay sets broker.mode = "live" and CLI flags are
        # silent. The overlay value should be honoured. This documents
        # the resolution order: overlay -> CLI; CLI wins when present.
        base = tmp_path / "config.yaml"
        _write_yaml(base, {"broker": {"mode": "paper"},
                           "logging": {"log_dir": str(tmp_path / "logs")}})
        overlay = tmp_path / "overlay.yaml"
        _write_yaml(overlay, {"broker": {"mode": "live"}})
        cfg = self._capture_config(base, overlay, paper=False, live=False)
        assert cfg["broker"]["mode"] == "live"

    def test_cli_live_overrides_overlay_paper(self, tmp_path):
        # Overlay says paper, CLI says --live -> CLI wins.
        base = tmp_path / "config.yaml"
        _write_yaml(base, {"broker": {"mode": "live"},
                           "logging": {"log_dir": str(tmp_path / "logs")}})
        overlay = tmp_path / "overlay.yaml"
        _write_yaml(overlay, {"broker": {"mode": "paper"}})
        cfg = self._capture_config(base, overlay, paper=False, live=True)
        assert cfg["broker"]["mode"] == "live"


# ---------------------------------------------------------------------------
# Static check on the actual Stage 3 overlay (catches regressions if the
# overlay file is hand-edited badly)
# ---------------------------------------------------------------------------

class TestStage3OverlayContents:
    @pytest.fixture
    def overlay_path(self):
        return ROOT / "config_overlays" / "stage3.yaml"

    def test_overlay_file_exists(self, overlay_path):
        assert overlay_path.exists(), \
            "config_overlays/stage3.yaml is missing -- Stage 3 cannot launch"

    def test_overlay_is_valid_yaml(self, overlay_path):
        # Will raise if malformed
        cfg = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
        assert isinstance(cfg, dict)

    def test_overlay_locks_basket_to_5_names(self, overlay_path):
        cfg = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
        instruments = cfg["market"]["instruments"]
        assert sorted(instruments) == sorted([
            "RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"
        ]), "Stage 3 basket changed -- update both overlay AND the plan"

    def test_overlay_disables_scanner(self, overlay_path):
        cfg = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
        assert cfg["scanner"]["enabled"] is False, \
            "scanner.enabled MUST be false in Stage 3 (basket is fixed)"

    def test_overlay_caps_capital_at_5k(self, overlay_path):
        cfg = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
        assert cfg["capital"]["initial_balance"] == 5000.0

    def test_overlay_caps_positions_and_trades(self, overlay_path):
        cfg = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
        assert cfg["risk"]["max_open_positions"] == 5
        assert cfg["risk"]["max_trades_per_day"] == 5

    def test_overlay_hard_caps_session_window(self, overlay_path):
        cfg = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
        hours = cfg["market"]["trading_hours"]
        assert hours["start"] == "09:30"
        assert hours["end"] == "12:30"

    def test_overlay_forbids_shadow_mode(self, overlay_path):
        # Stage 3 = real orders. shadow_mode true would silently drop them.
        cfg = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
        assert cfg["execution"]["shadow_mode"] is False
