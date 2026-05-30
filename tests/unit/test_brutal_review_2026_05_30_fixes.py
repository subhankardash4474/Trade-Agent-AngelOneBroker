"""Regression guards for the 2026-05-30 brutal review (Session 2) live-bug
findings.

The Session 2 review (`docs/reviews/brutal_review_2026-05-30.md`) caught two
live bugs on the local-laptop daemon while the trader VM was supposed to be
in "museum mode" per `docs/freeze/freeze_v3.0_charter_2026-05-30.md` §6.1:

  * Bug A — xgboost zombie. `xgboost_classifier` fired BUYs on AAPL/MSFT
    36 minutes after commit c9d3936 ("retire xgboost_classifier") landed.
    Root cause: the retirement was at the V15 backtest-variant level, and
    the local daemon's stale config.yaml still listed it in
    `strategies.active`. There was no defence-in-depth gate in
    `_load_strategies` to refuse a retired name even when the config
    presents it.

  * Bug B — `_persist_runtime_state` AttributeError. The writer crashed
    every cycle on `'TradingAgent' object has no attribute '_strategy_state'`.
    Root cause was probably a stale checkout (the 2026-05-18 audit fix at
    line ~1090 was not deployed), but the symptom is silent ledger
    corruption: the swallowed AttributeError leaves the on-disk snapshot
    stale, and the warning is easy to miss in a busy log.

Each guard below pins the FIX, not the bug, so a future refactor that
drops the guard cannot silently re-open the bug.

Cross-references:
  * `docs/reviews/brutal_review_2026-05-30.md` §2 (Session 2).
  * `docs/findings/findings_log_2026-05-27.md` §29-30.
  * Source-tree fixes:
        - `trading_agent.py:DEPRECATED_STRATEGIES`
        - `trading_agent._load_strategies` denylist gate
        - `trading_agent._persist_runtime_state` hasattr guard
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages"))


# ── Bug A regression: deprecated-strategy denylist ──────────────────────


def test_deprecated_strategies_set_contains_xgboost():
    """The denylist must list `xgboost_classifier`. Removing it without
    documenting why the T1 retirement verdict was overturned re-opens
    the AAPL/MSFT zombie-firing path the brutal review caught."""
    from trading_agent import DEPRECATED_STRATEGIES

    assert "xgboost_classifier" in DEPRECATED_STRATEGIES, (
        "T1 retirement verdict (V15 PF=0.77 < 0.90 floor, commit c9d3936) "
        "removed xgboost_classifier from the active strategy list. The "
        "DEPRECATED_STRATEGIES denylist is the defence-in-depth backstop "
        "against a stale config.yaml silently reviving it. Removing the "
        "name from this set requires a clean retrain + held-out PF >= 0.90 "
        "validation; see DEPRECATED_STRATEGIES docstring."
    )


def test_load_strategies_denies_deprecated_name(monkeypatch, tmp_path):
    """Behavioural guard: a config.yaml that lists `xgboost_classifier` in
    `strategies.active` must NOT result in the strategy being loaded.
    Mirrors the local-laptop daemon scenario from the brutal review."""
    import trading_agent as ta_mod

    # Build a minimal config that exercises the denylist branch. We
    # synthesise the agent shape that `_load_strategies` reads: the
    # method only depends on `self.config["strategies"]`, so we can
    # bind it to a stub instead of constructing a full TradingAgent.
    class _AgentStub:
        config = {
            "strategies": {
                "active": ["xgboost_classifier", "rsi_momentum"],
                "rsi_momentum": {},
                "xgboost_classifier": {},
            },
        }

    stub = _AgentStub()
    loaded = ta_mod.TradingAgent._load_strategies(stub)
    loaded_names = {s.name for s in loaded}

    assert "xgboost_classifier" not in loaded_names, (
        "DEPRECATED_STRATEGIES denylist did not block xgboost_classifier "
        "from loading. A stale config.yaml is now silently reviving the "
        "T1-retired strategy -- the exact bug the brutal review caught."
    )
    assert "rsi_momentum" in loaded_names, (
        "Other (non-deprecated) strategies must still load -- the denylist "
        "is per-name, not whole-config."
    )


def test_load_strategies_denylist_logs_critical(monkeypatch):
    """The denylist branch must emit a CRITICAL log so an operator running
    a stale config sees the bug instead of silently losing the strategy.
    """
    import trading_agent as ta_mod
    from loguru import logger

    captured: list = []
    handler_id = logger.add(
        lambda msg: captured.append(msg.record),
        level="CRITICAL",
    )
    try:
        class _AgentStub:
            config = {
                "strategies": {
                    "active": ["xgboost_classifier"],
                    "xgboost_classifier": {},
                },
            }

        stub = _AgentStub()
        ta_mod.TradingAgent._load_strategies(stub)
    finally:
        logger.remove(handler_id)

    critical_msgs = [
        r for r in captured
        if r["level"].name == "CRITICAL"
        and "STRATEGY-DEPRECATED" in r["message"]
    ]
    assert critical_msgs, (
        "Denylist branch must emit a CRITICAL log tagged "
        "[STRATEGY-DEPRECATED]. Operators running stale configs need a "
        "loud signal, not a silent skip."
    )


# ── Bug B regression: _persist_runtime_state defensive guard ────────────


def test_persist_runtime_state_skips_save_when_attribute_missing(
    tmp_path, monkeypatch, caplog
):
    """When _persist_runtime_state is called on an object missing
    `_strategy_state` (e.g. a partially-constructed TradingAgent), the
    method MUST log CRITICAL and return without raising. This prevents
    two production failure modes:
      * The unguarded AttributeError silently swallows in the warning
        log and the on-disk snapshot drifts (Session 1 Finding 4).
      * A naive defensive default (e.g. `getattr(self, '_strategy_state', {})`)
        would clobber the disk snapshot with empty state -- worse than
        the swallow because we then can't recover the lost protective
        runtime state.

    The fix's contract: missing attribute -> CRITICAL log, no save, no
    raise, on-disk snapshot preserved.
    """
    from trading_agent import TradingAgent
    from loguru import logger

    captured: list = []
    handler_id = logger.add(
        lambda msg: captured.append(msg.record),
        level="CRITICAL",
    )

    save_called = {"flag": False}

    def fake_save_runtime_state(*args, **kwargs):
        save_called["flag"] = True

    monkeypatch.setattr(
        "trading_agent.save_runtime_state", fake_save_runtime_state,
    )

    try:
        # Partially-constructed object: no _strategy_state attribute.
        class _PartialAgent:
            pass

        # Bind the method to the partial object.
        TradingAgent._persist_runtime_state(_PartialAgent())
    finally:
        logger.remove(handler_id)

    assert save_called["flag"] is False, (
        "_persist_runtime_state called save_runtime_state despite missing "
        "_strategy_state attribute. The defensive guard is meant to skip "
        "the save and preserve the on-disk snapshot."
    )
    critical_msgs = [
        r for r in captured
        if r["level"].name == "CRITICAL"
        and "RUNTIME-PERSIST" in r["message"]
        and "SKIPPED" in r["message"]
    ]
    assert critical_msgs, (
        "Defensive guard must emit a CRITICAL log with 'RUNTIME-PERSIST' "
        "and 'SKIPPED'. Operators need to see the bug, not silent drift."
    )


def test_persist_runtime_state_succeeds_on_full_object(tmp_path, monkeypatch):
    """Inverse guard: a fully-initialised object (all three attrs present)
    routes to save_runtime_state normally. This pins that the defensive
    guard does NOT add overhead to the happy path."""
    from trading_agent import TradingAgent

    save_args = {}

    def fake_save_runtime_state(strategy_state, recent_opens, consec_tp_today):
        save_args["strategy_state"] = strategy_state
        save_args["recent_opens"] = recent_opens
        save_args["consec_tp_today"] = consec_tp_today

    monkeypatch.setattr(
        "trading_agent.save_runtime_state", fake_save_runtime_state,
    )

    class _FullAgent:
        pass

    agent = _FullAgent()
    agent._strategy_state = {"rsi_momentum": {"trades": 1}}
    agent._recent_opens = deque()
    agent._consec_tp_today = {"TCS": 2}

    TradingAgent._persist_runtime_state(agent)

    assert save_args["strategy_state"] == {"rsi_momentum": {"trades": 1}}, (
        "Defensive guard should pass through to save_runtime_state when "
        "all attrs exist. The guard added a partial-object skip path; it "
        "must NOT change happy-path semantics."
    )
    assert save_args["consec_tp_today"] == {"TCS": 2}


def test_persist_runtime_state_skips_when_recent_opens_missing(monkeypatch):
    """The guard checks all three required attributes, not just
    _strategy_state. A partial init that set _strategy_state but not
    _recent_opens / _consec_tp_today must still skip the save."""
    from trading_agent import TradingAgent

    save_called = {"flag": False}
    monkeypatch.setattr(
        "trading_agent.save_runtime_state",
        lambda *a, **kw: save_called.__setitem__("flag", True),
    )

    class _PartialAgent:
        pass

    agent = _PartialAgent()
    agent._strategy_state = {}
    # Deliberately omit _recent_opens and _consec_tp_today.

    TradingAgent._persist_runtime_state(agent)

    assert save_called["flag"] is False, (
        "Defensive guard must check ALL three required attrs, not just "
        "_strategy_state. A partial init missing _recent_opens / "
        "_consec_tp_today must also skip the save to avoid AttributeError."
    )
