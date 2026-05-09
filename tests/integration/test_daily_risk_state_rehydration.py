"""Tests for daily risk-state rehydration across daemon restarts (2026-05-04 part 6).

Bug it fixes:
  Today's session crashed/restarted multiple times. The final daemon (PID 3352)
  booted at 14:38, AFTER all 4 closed trades had landed (10:29, 12:06, 12:16,
  13:49). Because RiskState.daily_pnl/daily_trades/consecutive_losses live
  only in memory, the new daemon initialised them to zero — so the EOD email
  reported "Day PnL: Rs +0.00" even though the trades table held Rs -2.25
  worth of realized P&L for the day.

  The risk circuit-breaker logic, daily-loss-limit gate, and
  consecutive_losses gate all key off these counters, so a mid-session
  restart effectively re-armed all of them too. That's not just a cosmetic
  reporting bug — it's a real risk gap.

Fix:
  RiskManager.rehydrate_daily_state(todays_trades) replays today's already-
  persisted closed trades into the in-memory counters. trading_agent.py
  invokes it once at boot using Database.load_trades_for_day(today_iso).

  current_balance and peak_balance are NOT touched — they're already
  resolved from the equity_curve snapshot which already reflects the
  post-trade cash. Re-adding pnl there would double-count.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.risk_manager import RiskManager


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def risk_mgr():
    """Fresh RiskManager with Rs 10k initial balance."""
    cfg = {
        "risk": {
            "max_drawdown_pct": 30.0,
            "consecutive_loss_limit": 3,
            "daily_loss_limit_pct": 5.0,
            "weekly_loss_limit_pct": 10.0,
        },
        "capital": {"initial_balance": 10_000.0},
    }
    return RiskManager(cfg, initial_balance=10_000.0, peak_balance=10_000.0)


def _trade(pnl: float, exit_time: str = "2026-05-04T10:00:00+05:30") -> dict:
    return {
        "symbol": "FOO",
        "side": "BUY",
        "entry_price": 100.0,
        "exit_price": 100.0 + pnl,
        "quantity": 1,
        "entry_time": exit_time,
        "exit_time": exit_time,
        "pnl": pnl,
        "pnl_pct": pnl,
        "strategy": "test",
    }


# ─────────────────────────────────────────────────────────────
# Counters update correctly
# ─────────────────────────────────────────────────────────────


def test_rehydrate_with_no_trades_is_a_noop(risk_mgr):
    """Empty trade list keeps counters at zero (clean morning boot)."""
    risk_mgr.rehydrate_daily_state([])
    assert risk_mgr.state.daily_pnl == 0.0
    assert risk_mgr.state.daily_trades == 0
    assert risk_mgr.state.consecutive_losses == 0
    assert len(risk_mgr.state.recent_trade_results) == 0


def test_rehydrate_sums_pnl_across_trades(risk_mgr):
    """daily_pnl matches the sum of all trade pnl values."""
    trades = [_trade(20.80), _trade(8.95), _trade(20.53), _trade(-52.53)]
    risk_mgr.rehydrate_daily_state(trades)
    assert risk_mgr.state.daily_pnl == pytest.approx(-2.25, abs=0.01)


def test_rehydrate_replays_actual_2026_05_04_session(risk_mgr):
    """Replay of the live bug — daemon crashed before EOD, daily_pnl shown
    as Rs 0 when reality was Rs -2.25 across 4 closed trades."""
    live_trades = [
        _trade(20.80, "2026-05-04T10:29:30+05:30"),   # IDEA short, +20.80
        _trade(8.95, "2026-05-04T12:06:40+05:30"),    # NIVABUPA short, +8.95
        _trade(20.53, "2026-05-04T12:16:58+05:30"),   # RAILTEL short, +20.53
        _trade(-52.53, "2026-05-04T13:49:14+05:30"),  # MEESHO short, -52.53
    ]
    risk_mgr.rehydrate_daily_state(live_trades)
    assert risk_mgr.state.daily_pnl == pytest.approx(-2.25, abs=0.01)
    assert risk_mgr.state.daily_trades == 4


def test_rehydrate_increments_daily_trades(risk_mgr):
    trades = [_trade(10.0), _trade(-5.0), _trade(2.5)]
    risk_mgr.rehydrate_daily_state(trades)
    assert risk_mgr.state.daily_trades == 3


def test_rehydrate_appends_to_recent_trade_results_deque(risk_mgr):
    trades = [_trade(10.0), _trade(-5.0)]
    risk_mgr.rehydrate_daily_state(trades)
    assert list(risk_mgr.state.recent_trade_results) == [10.0, -5.0]


# ─────────────────────────────────────────────────────────────
# Consecutive-loss tracking (powers the circuit breaker)
# ─────────────────────────────────────────────────────────────


def test_consecutive_losses_reflects_streak_at_time_of_boot(risk_mgr):
    """3 wins followed by 2 losses → consecutive_losses = 2."""
    trades = [_trade(10.0), _trade(5.0), _trade(8.0), _trade(-3.0), _trade(-7.0)]
    risk_mgr.rehydrate_daily_state(trades)
    assert risk_mgr.state.consecutive_losses == 2


def test_consecutive_losses_resets_on_a_winner(risk_mgr):
    """Losses then a winner — streak is reset, NOT carried forward."""
    trades = [_trade(-3.0), _trade(-7.0), _trade(15.0)]
    risk_mgr.rehydrate_daily_state(trades)
    assert risk_mgr.state.consecutive_losses == 0


def test_breakeven_trade_does_not_reset_or_increment_streak(risk_mgr):
    """pnl == 0 mirrors record_trade(): no reset, no increment."""
    trades = [_trade(-3.0), _trade(0.0), _trade(-5.0)]
    risk_mgr.rehydrate_daily_state(trades)
    # 2 actual losses, breakeven sandwich does not interrupt
    assert risk_mgr.state.consecutive_losses == 2


# ─────────────────────────────────────────────────────────────
# Idempotence and balance preservation
# ─────────────────────────────────────────────────────────────


def test_rehydrate_does_not_double_count_current_balance(risk_mgr):
    """current_balance must NOT change — it already reflects post-trade cash
    (loaded from equity_curve snapshot). Adding pnl again would double-count."""
    starting_balance = risk_mgr.state.current_balance
    trades = [_trade(20.80), _trade(-52.53)]
    risk_mgr.rehydrate_daily_state(trades)
    assert risk_mgr.state.current_balance == starting_balance


def test_rehydrate_does_not_change_peak_balance(risk_mgr):
    """peak_balance is sourced from the historical equity record and must
    not be perturbed by replaying trades."""
    starting_peak = risk_mgr.state.peak_balance
    trades = [_trade(100.0)]  # a winner — would normally bump peak
    risk_mgr.rehydrate_daily_state(trades)
    assert risk_mgr.state.peak_balance == starting_peak


# ─────────────────────────────────────────────────────────────
# Robustness: malformed rows
# ─────────────────────────────────────────────────────────────


def test_rehydrate_handles_missing_pnl_field(risk_mgr):
    """A trade row with no pnl key (legacy / corrupted) is treated as 0."""
    bad_trade = {"symbol": "FOO", "side": "BUY"}  # no pnl
    risk_mgr.rehydrate_daily_state([bad_trade, _trade(5.0)])
    assert risk_mgr.state.daily_pnl == 5.0
    assert risk_mgr.state.daily_trades == 2


def test_rehydrate_handles_none_pnl(risk_mgr):
    """A trade row with pnl=None is treated as 0 (paranoia for SQL NULLs)."""
    null_trade = _trade(0.0)
    null_trade["pnl"] = None
    risk_mgr.rehydrate_daily_state([null_trade, _trade(7.5)])
    assert risk_mgr.state.daily_pnl == 7.5


# ─────────────────────────────────────────────────────────────
# get_risk_summary integration
# ─────────────────────────────────────────────────────────────


def test_risk_summary_reflects_rehydrated_pnl(risk_mgr):
    """End-to-end: the EOD email reads from get_risk_summary(), so verify the
    user-facing 'Day PnL' field actually changes after rehydration."""
    pre = risk_mgr.get_risk_summary()
    assert pre["daily_pnl"] == 0.0  # before rehydrate

    risk_mgr.rehydrate_daily_state([_trade(20.80), _trade(-52.53)])
    post = risk_mgr.get_risk_summary()
    assert post["daily_pnl"] == pytest.approx(-31.73, abs=0.01)
    assert post["daily_trades"] == 2
