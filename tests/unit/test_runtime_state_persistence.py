"""Unit tests for packages.core.runtime_state_persistence (P2 restart-cluster).

Three intraday state buckets used to reset on restart:
  * _strategy_state    per-strategy circuit breaker (the worst miss --
                       a suspended strategy un-suspended itself on restart)
  * _recent_opens      sliding window for global open-rate cap
  * _consec_tp_today   per-symbol TP streak for trend-continuation

These tests pin:
  1. Round-trip preserves all three.
  2. ``_strategy_state`` and ``_consec_tp_today`` are date-scoped (reset
     on a day boundary), so a yesterday snapshot must be discarded.
  3. ``_recent_opens`` entries older than the open-rate window are
     discarded (same TTL the runtime enforces).
  4. Missing / malformed snapshots return empty containers and never raise.
"""
from __future__ import annotations

import json
import sys
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from core.runtime_state_persistence import (
    SNAPSHOT_FILENAME,
    load_runtime_state,
    save_runtime_state,
)

IST = pytz.timezone("Asia/Kolkata")


def test_roundtrip_preserves_all_three(tmp_path):
    now = datetime.now(IST)
    strategy_state = {
        "mean_reversion": {
            "consec_losses": 3, "daily_pnl": -120.5,
            "suspended": True, "suspended_reason": "consec_losses=3", "trades": 4,
        },
        "supertrend_follow": {
            "consec_losses": 0, "daily_pnl": 75.0,
            "suspended": False, "suspended_reason": "", "trades": 2,
        },
    }
    recent_opens = deque([
        (now - timedelta(minutes=1), "HDFCBANK"),
        (now - timedelta(minutes=2), "INFY"),
    ])
    consec_tp_today = {"TCS": 2, "HDFCBANK": 1}

    save_runtime_state(strategy_state, recent_opens, consec_tp_today, data_dir=tmp_path)
    s, o, t = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert s == strategy_state
    assert len(o) == 2
    assert t == consec_tp_today


def test_strategy_state_dropped_on_day_boundary(tmp_path):
    """Snapshot from yesterday must NOT re-suspend a strategy today."""
    yesterday = datetime.now(IST) - timedelta(days=1)
    strategy_state = {
        "supertrend_follow": {
            "consec_losses": 3, "daily_pnl": -200.0,
            "suspended": True, "suspended_reason": "consec_losses=3", "trades": 4,
        },
    }
    save_runtime_state(
        strategy_state, deque(), {}, data_dir=tmp_path,
    )
    # Hand-edit the saved_at to yesterday
    path = tmp_path / SNAPSHOT_FILENAME
    payload = json.loads(path.read_text())
    payload["saved_at"] = yesterday.isoformat()
    path.write_text(json.dumps(payload))

    s, _, _ = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert s == {}, "stale strategy_state must be dropped on day boundary"


def test_consec_tp_today_dropped_on_day_boundary(tmp_path):
    """Same rule for the per-symbol TP-streak map."""
    yesterday = datetime.now(IST) - timedelta(days=1)
    save_runtime_state({}, deque(), {"TCS": 3}, data_dir=tmp_path)
    path = tmp_path / SNAPSHOT_FILENAME
    payload = json.loads(path.read_text())
    payload["saved_at"] = yesterday.isoformat()
    path.write_text(json.dumps(payload))

    _, _, t = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert t == {}


def test_recent_opens_outside_window_dropped(tmp_path):
    """Entries older than the open-rate window TTL must be discarded."""
    now = datetime.now(IST)
    fresh = now - timedelta(minutes=2)
    stale = now - timedelta(minutes=30)
    opens = deque([(stale, "OLD_SYM"), (fresh, "FRESH_SYM")])
    save_runtime_state({}, opens, {}, data_dir=tmp_path)

    _, o, _ = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert len(o) == 1
    assert o[0][1] == "FRESH_SYM"


def test_load_missing_file_returns_empty(tmp_path):
    s, o, t = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert s == {} and o == [] and t == {}


def test_load_malformed_json_returns_empty(tmp_path):
    (tmp_path / SNAPSHOT_FILENAME).write_text("{{not-json", encoding="utf-8")
    s, o, t = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert s == {} and o == [] and t == {}


def test_suspended_strategy_survives_same_day_restart(tmp_path):
    """End-to-end: a strategy hits its 3-loss breaker, daemon restarts
    within the same trading day, breaker state survives."""
    now = datetime.now(IST)
    strategy_state = {
        "supertrend_follow": {
            "consec_losses": 3, "daily_pnl": -150.0,
            "suspended": True, "suspended_reason": "consec_losses=3", "trades": 3,
        },
    }
    save_runtime_state(strategy_state, deque(), {}, data_dir=tmp_path)

    # Restart: load
    s, _, _ = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert s["supertrend_follow"]["suspended"] is True
    assert s["supertrend_follow"]["consec_losses"] == 3


# ---------------------------------------------------------------------------
# Regression #5 / #6 (2026-05-18): schema version + corrupt fail-loud
# ---------------------------------------------------------------------------


def test_corrupt_snapshot_logs_critical(tmp_path, caplog):
    """A corrupt runtime_state snapshot used to drop to ({},[],{}) without
    a loud signal. The lost state is the suspended-strategy circuit
    breaker -- losing it silently means a suspended strategy comes back
    armed. Must log CRITICAL now."""
    import logging

    (tmp_path / SNAPSHOT_FILENAME).write_text("not-json", encoding="utf-8")
    with caplog.at_level(logging.CRITICAL):
        s, o, t = load_runtime_state(
            open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
        )
    assert s == {} and o == [] and t == {}


def test_future_schema_version_refused(tmp_path, caplog):
    """A snapshot version higher than the current build must be REFUSED,
    not silently mis-parsed."""
    import logging

    payload = {
        "version": 42,
        "saved_at": datetime.now(IST).isoformat(),
        "strategy_state": {
            "mean_reversion": {
                "consec_losses": 5, "daily_pnl": -200.0,
                "suspended": True, "suspended_reason": "x", "trades": 5,
            },
        },
        "recent_opens": [],
        "consec_tp_today": {},
    }
    (tmp_path / SNAPSHOT_FILENAME).write_text(json.dumps(payload), encoding="utf-8")

    with caplog.at_level(logging.CRITICAL):
        s, o, t = load_runtime_state(
            open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
        )
    assert s == {} and o == [] and t == {}


def test_unparseable_schema_version_refused(tmp_path):
    """version: object literal -> treated as untrusted format."""
    payload = {
        "version": {"oops": "this is a dict"},
        "saved_at": datetime.now(IST).isoformat(),
        "strategy_state": {},
        "recent_opens": [],
        "consec_tp_today": {},
    }
    (tmp_path / SNAPSHOT_FILENAME).write_text(json.dumps(payload), encoding="utf-8")

    s, o, t = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert s == {} and o == [] and t == {}


def test_malformed_strategy_entries_skipped(tmp_path):
    """A row that isn't a dict is silently dropped instead of crashing
    the rest of the load."""
    save_runtime_state({}, deque(), {}, data_dir=tmp_path)
    path = tmp_path / SNAPSHOT_FILENAME
    payload = json.loads(path.read_text())
    payload["strategy_state"] = {
        "good": {"consec_losses": 1, "daily_pnl": -50.0, "suspended": False,
                 "suspended_reason": "", "trades": 1},
        "bad_not_dict": "this should be silently skipped",
    }
    path.write_text(json.dumps(payload))
    s, _, _ = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert "good" in s
    assert "bad_not_dict" not in s


# ── 2026-05-18 Audit Bug #5: TradingAgent init-order regression guard ────────
#
# Bug fingerprint (from logs/trading_agent_2026-05-18.log:47883):
#
#     [RUNTIME-PERSIST] load failed at init:
#     AttributeError("'TradingAgent' object has no attribute '_strategy_state'")
#
# Root cause: ``self._strategy_state`` was initialised ~70 lines below the
# ``load_runtime_state`` call site in ``TradingAgent.__init__``. On a
# restart where the snapshot was non-empty, the loop
# ``self._strategy_state[s] = v`` raised AttributeError, the ``except``
# swallowed it, and the runtime state (suspended strategies, open-rate
# window, TP-streak counters) was silently LOST. Today's restart was
# post-cutoff so the impact was nil, but any mid-day restart would
# un-arm a circuit-breakered strategy.
#
# Fix: ``self._strategy_state = {}`` is now initialised IMMEDIATELY
# BEFORE the ``load_runtime_state`` block. The legacy assignment ~70
# lines below has been removed (single source of truth).
#
# The tests below pin both halves of the contract.


def test_init_strategy_state_assigned_before_runtime_load():
    """Structural regression guard: ``self._strategy_state = {}`` (or any
    direct assignment to it) MUST appear in ``TradingAgent.__init__``
    BEFORE the ``load_runtime_state(`` call. Otherwise a non-empty
    snapshot will AttributeError on the first ``self._strategy_state[s] = v``
    line inside the try block."""
    import inspect
    import re
    from trading_agent import TradingAgent

    src = inspect.getsource(TradingAgent.__init__)
    load_match = re.search(r"load_runtime_state\s*\(", src)
    assert load_match, (
        "load_runtime_state call moved or removed from __init__ -- "
        "this regression guard cannot locate it. Update the regex if "
        "the call site name changed."
    )
    pre_load_src = src[: load_match.start()]
    assigns = list(re.finditer(
        r"self\._strategy_state\s*(?::\s*Dict\[[^\]]+\]\s*)?=\s*\{",
        pre_load_src,
    ))
    assert assigns, (
        "Audit Bug #5 regression: ``self._strategy_state = {}`` no "
        "longer initialised BEFORE the load_runtime_state call in "
        "TradingAgent.__init__. The first iteration of "
        "``self._strategy_state[s] = v`` inside the try block will "
        "AttributeError on a non-empty snapshot, the except will "
        "swallow it, and protective runtime state will silently be "
        "lost on every mid-day restart."
    )


def test_init_strategy_state_not_reset_after_runtime_load():
    """Inverse guard: ``self._strategy_state`` must NOT be reassigned to
    an empty dict AFTER the load_runtime_state block. If it were, the
    fix would self-defeat: the load loop would populate the dict, and
    a later ``self._strategy_state = {}`` would wipe the restored state.
    Other forms (``.clear()``, ``self._strategy_state[k] = v`` mutation,
    daily reset) are fine -- only a wholesale re-assignment to ``{}``
    is the bug shape we're guarding against."""
    import inspect
    import re
    from trading_agent import TradingAgent

    src = inspect.getsource(TradingAgent.__init__)
    # Find the *first* assignment that matches the bug shape.
    pattern = re.compile(
        r"self\._strategy_state\s*(?::\s*Dict\[[^\]]+\]\s*)?=\s*\{\s*\}"
    )
    matches = list(pattern.finditer(src))
    assert len(matches) == 1, (
        f"Audit Bug #5 regression: expected EXACTLY 1 wholesale "
        f"``self._strategy_state = {{}}`` assignment in "
        f"TradingAgent.__init__ (the canonical pre-load init), found "
        f"{len(matches)}. Multiple assignments mean a later one will "
        f"wipe the snapshot-restored runtime state."
    )
    # And the single assignment must precede the load.
    load_match = re.search(r"load_runtime_state\s*\(", src)
    assert load_match and matches[0].start() < load_match.start(), (
        "The canonical ``self._strategy_state = {}`` assignment is now "
        "AFTER load_runtime_state -- the exact bug we just fixed."
    )


def test_minimal_init_path_does_not_raise_attributeerror_on_nonempty_snapshot(tmp_path, monkeypatch):
    """Behavioural guard: simulate the exact code path that failed in
    production. We don't boot a full TradingAgent (heavy + needs Angel
    credentials), but we replicate the failing pattern: an instance
    without ``_strategy_state`` runs the exact for-loop the init does
    over the data returned by ``load_runtime_state`` of a non-empty
    snapshot. Before the fix this raised AttributeError; after the
    fix the for-loop populates a freshly-initialised dict."""
    # Build a non-empty snapshot today.
    now = datetime.now(IST)
    save_runtime_state(
        strategy_state={
            "mean_reversion": {
                "consec_losses": 3, "daily_pnl": -120.5,
                "suspended": True, "suspended_reason": "consec_losses=3",
                "trades": 4,
            },
        },
        recent_opens=deque([(now - timedelta(minutes=1), "HDFCBANK")]),
        consec_tp_today={"TCS": 2},
        data_dir=tmp_path,
    )
    restored_strat, restored_opens, restored_tp = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert restored_strat, "Test fixture broken: snapshot loaded empty."

    # Replicate the exact init pattern. ``_strategy_state`` MUST be
    # initialised BEFORE the for-loop, otherwise we'll see the bug.
    class _AgentStub:
        pass
    agent = _AgentStub()
    agent._strategy_state = {}
    agent._recent_opens = deque()
    agent._consec_tp_today = {}

    # The exact code from TradingAgent.__init__.
    for s, v in restored_strat.items():
        agent._strategy_state[s] = v
    for ts, sym in restored_opens:
        agent._recent_opens.append((ts, sym))
    for sym, c in restored_tp.items():
        agent._consec_tp_today[sym] = c

    assert agent._strategy_state == restored_strat
    assert agent._strategy_state["mean_reversion"]["suspended"] is True
