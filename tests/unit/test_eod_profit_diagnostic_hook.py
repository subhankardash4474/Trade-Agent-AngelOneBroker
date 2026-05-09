"""Unit tests for the EOD profit-diagnostic email hook (2026-05-09).

Validates:
  - _extract_verdict_tag picks the worst-priority verdict (KILL > WATCH >
    SCALE > KEEP).
  - Empty / unparseable reports return "" without crashing.
  - _send_profit_diagnostic_email respects the subprocess-isolation
    contract (any failure is logged, never raised).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add packages/ to sys.path BEFORE importing trading_agent so its top-level
# `from core...` imports resolve. conftest.py at the project root handles
# this when pytest is invoked from the repo root, but be explicit so this
# test is robust to standalone invocation as well.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "packages")
)

from trading_agent import TradingAgent  # noqa: E402


# ─────────────────────────────────────────────────────────────────
# _extract_verdict_tag — priority + edge cases
# ─────────────────────────────────────────────────────────────────


class TestExtractVerdictTag:
    def test_kill_wins_over_watch_and_scale(self):
        report = (
            "| rsi_momentum | 15 | ... | SCALE |\n"
            "| mean_reversion | 22 | ... | KILL |\n"
            "| supertrend_follow | 30 | ... | WATCH |\n"
        )
        assert TradingAgent._extract_verdict_tag(report) == "[KILL]"

    def test_watch_wins_over_scale_and_keep(self):
        report = (
            "| supertrend_follow | 30 | ... | WATCH |\n"
            "| rsi_momentum | 15 | ... | SCALE |\n"
            "| vwap_bounce | 20 | ... | KEEP |\n"
        )
        assert TradingAgent._extract_verdict_tag(report) == "[WATCH]"

    def test_scale_wins_over_keep(self):
        report = (
            "| rsi_momentum | 15 | ... | KEEP |\n"
            "| xgboost_classifier | 12 | ... | SCALE |\n"
        )
        assert TradingAgent._extract_verdict_tag(report) == "[SCALE]"

    def test_keep_only(self):
        report = "| rsi_momentum | 15 | ... | KEEP |"
        assert TradingAgent._extract_verdict_tag(report) == "[KEEP]"

    def test_no_verdict_returns_empty_string(self):
        # All-INSUFFICIENT_DATA day — nothing is actionable yet.
        report = (
            "| rsi_momentum | 5 | ... | INSUFFICIENT_DATA |\n"
            "| xgboost_classifier | 3 | ... | INSUFFICIENT_DATA |\n"
        )
        assert TradingAgent._extract_verdict_tag(report) == ""

    def test_empty_report(self):
        assert TradingAgent._extract_verdict_tag("") == ""

    def test_no_false_positive_on_prose(self):
        # The legend mentions all 4 verdict words, but in prose form
        # (`KILL` -- bleeding...). Those must NOT match the regex anchor.
        report = (
            "**Verdict legend:**\n"
            "- `SCALE` -- strong edge\n"
            "- `KEEP` -- net positive\n"
            "- `WATCH` -- marginal\n"
            "- `KILL` -- bleeding, disable\n"
        )
        # Anchor is `| TAG |` (table cell), so prose mentions don't match.
        assert TradingAgent._extract_verdict_tag(report) == ""


# ─────────────────────────────────────────────────────────────────
# _send_profit_diagnostic_email — isolation + behaviour
# ─────────────────────────────────────────────────────────────────


class TestSendProfitDiagnosticEmail:
    """The integration test for the full subprocess+email path runs in
    test_eod_profit_diagnostic_integration.py (when added). Here we
    validate the in-process logic only, with subprocess + alert_manager
    mocked, so a missing python interpreter / DB / network can never
    flake this suite."""

    def _make_agent_stub(self, tmp_path: Path) -> TradingAgent:
        """Build a minimally-instantiated TradingAgent that only has the
        attributes needed by `_send_profit_diagnostic_email`. We bypass
        __init__ to avoid bringing up the full agent (config, broker,
        DB, scanner, ...) for a method-level test."""
        agent = TradingAgent.__new__(TradingAgent)
        agent.alert_manager = MagicMock()
        # __file__ for the trading_agent module is at repo root by design
        # (it's an entry point). Override repo_root resolution by patching
        # subprocess so it doesn't actually run.
        return agent

    def test_subprocess_failure_is_swallowed(self, tmp_path):
        agent = self._make_agent_stub(tmp_path)
        with patch("subprocess.run", side_effect=RuntimeError("boom")):
            # MUST NOT raise — the EOD path needs this to be best-effort.
            agent._send_profit_diagnostic_email("2026-05-09")
        # alert_manager must NOT be called when subprocess fails
        agent.alert_manager.send_alert.assert_not_called()

    def test_missing_report_does_not_send_email(self, tmp_path, monkeypatch):
        agent = self._make_agent_stub(tmp_path)
        # Patch repo_root resolution by making out_path always-missing
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            # Point Path.exists to False for the report file
            with patch.object(Path, "exists", return_value=False):
                agent._send_profit_diagnostic_email("2026-05-09")
        agent.alert_manager.send_alert.assert_not_called()

    def test_email_send_failure_is_logged_not_raised(self, tmp_path, monkeypatch):
        """If alert_manager.send_alert throws (e.g. network down at EOD),
        we log and move on; we do NOT crash the agent's EOD path."""
        agent = self._make_agent_stub(tmp_path)
        agent.alert_manager.send_alert.side_effect = RuntimeError("smtp dead")
        with patch("subprocess.run") as mock_run, \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value="| KILL |"), \
             patch.object(Path, "mkdir"):
            mock_run.return_value = MagicMock(returncode=0)
            # MUST NOT raise
            agent._send_profit_diagnostic_email("2026-05-09")
        # alert_manager WAS attempted (we got past the report-read)
        agent.alert_manager.send_alert.assert_called_once()
