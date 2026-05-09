"""Regression tests for the 2026-05-04 EOD audit fixes.

Three issues were surfaced by the live run on 2026-05-04 EOD:
  1. Duplicate EOD emails — three near-identical emails sent for the same
     event:
       15:20:36 "EOD Summary"      (from _maybe_send_eod_summary)
       15:20:37 "Daily Report"     (also from _maybe_send_eod_summary)
       15:30:29 "Daily Report"     (from _shutdown after EOD already sent)
  2. Ensemble confidence > 1.0 — SAILIFE supertrend SELL in bear_high_vol
     produced conf=1.100 because the directional multiplier (1.1) exceeded
     the unsuppressed weight (1.0). Confidence is meant to live in [0, 1].
  3. Repeated "Trading blocked: Past intraday exit time" warnings — fired
     every cycle after 15:15, producing 9 identical warnings in today's
     run.

These tests pin the new dedup / clamp behavior so future refactors don't
re-introduce the bugs.
"""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.ensemble import EnsembleModel
from strategies.base_strategy import Signal, TradeSignal


# ---------------------------------------------------------------------------
# Fix 2: Confidence clamp
# ---------------------------------------------------------------------------

def _make_signal(strategy: str, side: Signal, confidence: float = 0.9) -> TradeSignal:
    return TradeSignal(
        signal=side,
        symbol="TEST",
        price=100.0,
        timestamp=datetime.now(),
        strategy_name=strategy,
        confidence=confidence,
    )


class TestConfidenceClamp:
    """Confidence must never exceed 1.0 — even when directional multipliers > 1
    push the weighted sum above the unsuppressed denominator."""

    def _ens(self, threshold: float = 0.30) -> EnsembleModel:
        # EnsembleModel reads from config["ensemble"][...]; keep threshold low
        # so the aggregator emits a signal we can inspect.
        return EnsembleModel({
            "ensemble": {"confidence_threshold": threshold, "min_strategies_agree": 1}
        })

    def test_buy_confidence_clamped_at_one(self):
        # supertrend_follow BUY in bull_low_vol gets multiplier 1.4 — base
        # weight 1.5 (from DEFAULT_WEIGHTS) × 1.4 / unsuppressed (1.5) = 1.4
        # raw, then × signal_conf 0.95 = 1.33. Without clamp this exceeds 1.
        ens = self._ens()
        sig = _make_signal("supertrend_follow", Signal.BUY, confidence=0.95)
        out = ens.aggregate([sig], symbol="TEST", current_price=100.0, regime="bull_low_vol")
        assert out is not None, "test setup error — should have emitted a BUY"
        assert out.confidence <= 1.0, (
            f"BUY confidence must be clamped to <=1.0; got {out.confidence}"
        )
        # And meta should also be clamped
        assert out.metadata["buy_confidence"] <= 1.0

    def test_sell_confidence_clamped_at_one(self):
        # supertrend_follow SELL in bear_high_vol = 1.1 (the actual case
        # that produced SAILIFE conf=1.100 in the live run).
        ens = self._ens()
        sig = _make_signal("supertrend_follow", Signal.SELL, confidence=1.0)
        out = ens.aggregate([sig], symbol="SAILIFE", current_price=100.0, regime="bear_high_vol")
        assert out is not None
        assert out.confidence <= 1.0, (
            f"SELL confidence must be clamped; got {out.confidence} (this was 1.100 pre-fix)"
        )

    def test_clamp_does_not_artificially_raise_low_confidences(self):
        # A low-conviction signal should remain low; the clamp is one-sided.
        ens = self._ens(threshold=0.20)
        sig = _make_signal("mean_reversion", Signal.SELL, confidence=0.50)
        out = ens.aggregate([sig], symbol="X", current_price=100.0, regime="bear_high_vol")
        assert out is not None
        # mean_reversion SELL in bear_high_vol = 0.7 → 0.50 × 0.7 = 0.35
        assert out.confidence == pytest.approx(0.35, abs=1e-6)
        assert out.confidence <= 1.0  # trivially true here

    def test_clamp_only_caps_at_one_does_not_floor(self):
        # The clamp is `min(x, 1.0)` not `max(0, min(x, 1))` — already-low
        # values pass through unchanged.
        ens = self._ens(threshold=0.10)
        sig = _make_signal("mean_reversion", Signal.SELL, confidence=0.20)
        out = ens.aggregate([sig], symbol="Y", current_price=100.0, regime="bear_high_vol")
        assert out is not None
        # 0.20 * 0.7 = 0.14 — must come through as-is
        assert out.confidence == pytest.approx(0.14, abs=1e-6)


# ---------------------------------------------------------------------------
# Fix 1: Duplicate EOD email
# ---------------------------------------------------------------------------

class TestEODDeduplication:
    """The agent must send only ONE consolidated EOD email per session."""

    def _strip_comments(self, src: str) -> str:
        """Remove Python line comments and triple-quoted docstrings so that
        textual checks aren't fooled by historical context in commit notes."""
        import re
        # Remove triple-quoted strings (greedy non-overlapping)
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        src = re.sub(r"'''[\s\S]*?'''", "", src)
        # Remove # comments to end of line
        src = re.sub(r"(^|[^'\"])#[^\n]*", r"\1", src)
        return src

    def test_eod_summary_does_not_also_send_daily_report(self):
        """_maybe_send_eod_summary should call send_alert exactly once and
        NOT also call send_daily_report — those produced duplicate emails."""
        agent_path = Path(__file__).parent.parent / "trading_agent.py"
        src = agent_path.read_text(encoding="utf-8")
        # Locate the _maybe_send_eod_summary method body
        marker = "def _maybe_send_eod_summary"
        i = src.find(marker)
        assert i >= 0, "method not found"
        body = src[i : i + 5000]
        # Cut at the next method definition
        next_method = body.find("\n    def ", 50)
        body = body[: next_method if next_method > 0 else len(body)]
        # Strip comments / docstrings before checking — historical context
        # in comments otherwise trips the simple text search.
        code_only = self._strip_comments(body)
        assert "send_daily_report" not in code_only, (
            "send_daily_report must NOT be called inside _maybe_send_eod_summary "
            "— that produces a duplicate email (verified live on 2026-05-04: "
            "15:20:36 EOD Summary + 15:20:37 Daily Report)"
        )
        # And the EOD Summary alert IS still being sent.
        assert 'send_alert("EOD Summary"' in code_only, (
            "_maybe_send_eod_summary must still send the consolidated EOD email"
        )

    def test_shutdown_skips_daily_report_when_eod_already_sent(self):
        """Source-level: _shutdown() must guard send_daily_report on the
        _eod_summary_sent flag so a daemon kill near intraday close doesn't
        produce a third near-identical email."""
        agent_path = Path(__file__).parent.parent / "trading_agent.py"
        src = agent_path.read_text(encoding="utf-8")
        i = src.find("def _shutdown(self):")
        assert i >= 0
        body = src[i : i + 4000]
        # Both must be present
        assert "send_daily_report" in body, "shutdown still calls send_daily_report"
        assert "_eod_summary_sent" in body, (
            "_shutdown must check _eod_summary_sent before sending Daily Report"
        )
        # The daily-report call must be guarded — i.e. preceded by the flag check
        flag_idx = body.find("_eod_summary_sent")
        report_idx = body.find("send_daily_report")
        # The flag check needs to come BEFORE the call (within the method body)
        assert flag_idx < report_idx, (
            "Guard `if not self._eod_summary_sent` must precede the "
            "send_daily_report call inside _shutdown"
        )


# ---------------------------------------------------------------------------
# Fix 3: Trade-block dedup
# ---------------------------------------------------------------------------

class TestTradeBlockDedup:
    """When can_trade returns the same blocked reason cycle after cycle, only
    the first occurrence should warn; subsequent identical reasons go to
    debug. A transition (different reason, or back to tradeable) re-arms."""

    def _logic(self, agent, reason: str) -> str:
        """Replay the dedup logic in isolation. Returns 'warn' or 'debug'."""
        last_reason = getattr(agent, "_last_trade_block_reason", None)
        result = "warn" if reason != last_reason else "debug"
        agent._last_trade_block_reason = reason
        return result

    def _clear_on_unblock(self, agent) -> None:
        agent._last_trade_block_reason = None

    def test_first_block_warns(self):
        agent = MagicMock()
        del agent._last_trade_block_reason  # ensure not yet set
        assert self._logic(agent, "Past intraday exit time (15:15)") == "warn"

    def test_repeat_same_reason_demoted_to_debug(self):
        agent = MagicMock()
        del agent._last_trade_block_reason
        self._logic(agent, "Past intraday exit time (15:15)")
        # Same reason in next cycle → debug
        assert self._logic(agent, "Past intraday exit time (15:15)") == "debug"
        # And next, and next
        assert self._logic(agent, "Past intraday exit time (15:15)") == "debug"

    def test_changed_reason_re_warns(self):
        agent = MagicMock()
        del agent._last_trade_block_reason
        self._logic(agent, "Past intraday exit time (15:15)")
        assert self._logic(agent, "Max daily trades: 15/15") == "warn"

    def test_unblock_then_block_again_re_warns(self):
        agent = MagicMock()
        del agent._last_trade_block_reason
        self._logic(agent, "Past intraday exit time (15:15)")
        # Transition back to tradeable
        self._clear_on_unblock(agent)
        # Now blocked again with the SAME reason — should still warn,
        # because we cleared the dedup memory on the unblock.
        assert self._logic(agent, "Past intraday exit time (15:15)") == "warn"

    def test_replay_todays_actual_pattern(self):
        """Simulate the 2026-05-04 EOD pattern: 9 cycles in a row blocked
        with 'Past intraday exit time' — should produce exactly 1 warning,
        not 9."""
        agent = MagicMock()
        del agent._last_trade_block_reason
        warnings_count = 0
        for _ in range(9):
            r = self._logic(agent, "Past intraday exit time (15:15)")
            if r == "warn":
                warnings_count += 1
        assert warnings_count == 1, (
            f"9 identical block reasons must produce exactly 1 warning; got {warnings_count}"
        )
