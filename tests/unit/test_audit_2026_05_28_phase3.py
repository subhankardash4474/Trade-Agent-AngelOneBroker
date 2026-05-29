"""Regression tests for the 2026-05-28 audit follow-up Phase-3 fixes.

Each test maps 1:1 (or 1:few) to a finding ID in
``docs/audit_2026-05-28_followup.md``. Naming convention:
``test_<finding>_<one_line_intent>``.

Phase-3 scope (this file): concurrency + state hygiene.

  * ORD-06:   JWT refresh propagates to ``ws_client.update_broker_session``.
  * CONC-02:  WS-tick trail mutation routed through ``_exit_check_lock``.
  * CONC-04:  ``on_candle_close`` dispatched OUTSIDE the aggregator lock.
  * CONC-06:  ``_subscriptions`` iteration paths take the dedicated lock.
  * CONC-07:  Pre-existing ``_close_existing_ws`` helper used on reconnect.
  * CONC-08:  ``TradingAgent.run`` installs SIGTERM/SIGINT handlers.
  * CONC-09:  ``WebSocketClient.join`` waits for the worker thread.
  * STATE-03: Cooldown attributes initialised BEFORE boot reconcile.
  * STATE-04: ``Database.close_position_atomic`` runs DELETE + INSERT
              trade + INSERT equity in a single transaction.
  * STATE-06: cooldown / runtime / trail persistence retry on lock
              timeout instead of falling back to unlocked write.
  * STATE-08: Debounced trail-state persist on WS-tick mutation.
  * STATE-09: Corrupt cooldown JSON writes a sentinel that engages a
              fail-closed gate in ``TradingAgent``.
  * STATE-11: ``SignalAudit`` queues failed rows and re-flushes on the
              next ``log()`` call.
  * STATE-12: Daily reset sweeps ``open_positions`` rows whose
              ``entry_time`` is strictly before today.
  * CONC-05 / PERF-05:
              ``TradingAgent`` buffers ticks and flushes via
              ``store_ticks_batch`` instead of one INSERT per tick.

Tests are pure-Python and avoid any real broker / network I/O. Heavy
state (TradingAgent / Portfolio) uses source-level assertions where
constructing a full agent is impractical -- mirroring the
phase-1/phase-2 test style.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
if str(PACKAGES) not in sys.path:
    sys.path.insert(0, str(PACKAGES))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ───────────── ORD-06: JWT refresh wires WS reconnect ─────────────


def test_ord06_maybe_refresh_broker_session_calls_update_broker_session():
    """``_maybe_refresh_broker_session`` must call
    ``ws_client.update_broker_session`` after swapping ``execution._api``
    so the WS thread reconnects with the new auth/feed tokens.
    Source-level assertion: a previous version of the file simply
    forgot the WS hop -> tick feed silently died at JWT expiry."""
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    refresh = re.search(
        r"def _maybe_refresh_broker_session\(self.*?\n(    def |\Z)",
        src, re.DOTALL,
    )
    assert refresh, "_maybe_refresh_broker_session not found"
    body = refresh.group(0)
    assert "self.execution._api = api" in body
    assert "self.data_handler._api = api" in body
    assert "self.ws_client.update_broker_session(api" in body, (
        "ORD-06: JWT refresh must propagate to WS via update_broker_session"
    )


# ───────────── CONC-02: WS-tick trail mutation under exit lock ─────────────


def test_conc02_ws_trail_update_holds_exit_check_lock():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    on_tick = re.search(
        r"def _on_tick\(self.*?\n(    def |\Z)", src, re.DOTALL,
    )
    assert on_tick, "_on_tick not found"
    body = on_tick.group(0)
    update_call = body.find("update_trailing_stop(symbol, price)")
    assert update_call >= 0, "update_trailing_stop call missing"
    preceding = body[:update_call]
    last_with = preceding.rfind("with self._exit_check_lock")
    assert last_with >= 0, (
        "CONC-02: trail mutation in WS path must be under _exit_check_lock"
    )
    assert update_call - last_with < 800, (
        "CONC-02: with-block does not appear to wrap the mutation"
    )


# ───────────── CONC-04: candle close callback outside aggregator lock ─────────────


def test_conc04_process_tick_dispatches_callbacks_outside_lock():
    """When a candle boundary closes, the registered ``on_candle_close``
    must NOT be invoked while ``self._lock`` is held -- otherwise a DB
    write inside the callback blocks every other tick."""
    from core.tick_aggregator import TickAggregator

    agg = TickAggregator(["1min"])
    callback_calls = []
    seen_locked: list = []

    def on_close(symbol, interval, candle):
        # If the lock is held by us, ``acquire(blocking=False)`` would
        # succeed because RLock is reentrant. Use a plain mutex probe
        # via the internal flag instead.
        callback_calls.append((symbol, interval, dict(candle)))
        # Try to acquire the lock NON-blocking from a *different* thread
        # to verify the main thread doesn't hold it at this point.
        result = []

        def probe():
            result.append(agg._lock.acquire(blocking=False))
            if result[-1]:
                agg._lock.release()

        t = threading.Thread(target=probe)
        t.start()
        t.join(timeout=1.0)
        seen_locked.append(result[0] if result else False)

    agg.on_candle_close = on_close
    base = __import__("datetime").datetime(2026, 5, 28, 9, 15, 0,
                                            tzinfo=agg._get_candle_start.__globals__["IST"])
    agg.process_tick("RELIANCE", 100.0, 10, timestamp=base)
    later = base.replace(minute=16, second=1)
    agg.process_tick("RELIANCE", 101.0, 5, timestamp=later)
    assert callback_calls, "on_candle_close was never fired"
    assert seen_locked and all(seen_locked), (
        "CONC-04: callback was invoked while the aggregator lock was held"
    )


def test_conc04_process_tick_still_appends_history_on_callback_exception():
    from core.tick_aggregator import TickAggregator

    agg = TickAggregator(["1min"])

    def boom(symbol, interval, candle):
        raise RuntimeError("DB down")

    agg.on_candle_close = boom
    base = __import__("datetime").datetime(2026, 5, 28, 10, 0, 0,
                                            tzinfo=agg._get_candle_start.__globals__["IST"])
    agg.process_tick("INFY", 200.0, 1, timestamp=base)
    next_min = base.replace(minute=1, second=2)
    agg.process_tick("INFY", 201.0, 1, timestamp=next_min)
    history = agg.get_candle_history("INFY", "1min")
    assert not history.empty, "history must contain the boundary-closed candle even when callback raised"


# ───────────── CONC-06: subscription iterations under lock ─────────────


def test_conc06_token_to_symbol_iterates_under_lock():
    src = (ROOT / "packages" / "core" / "websocket_client.py").read_text(encoding="utf-8")
    func = re.search(
        r"def _token_to_symbol\(self.*?\n    def ", src, re.DOTALL,
    )
    assert func, "_token_to_symbol not found"
    body = func.group(0)
    assert "with self._subscriptions_lock" in body, (
        "CONC-06: _token_to_symbol must take the subscription lock before iterating"
    )


def test_conc06_run_simulation_snapshots_under_lock():
    src = (ROOT / "packages" / "core" / "websocket_client.py").read_text(encoding="utf-8")
    func = re.search(
        r"def _run_simulation\(self.*?\n    def ", src, re.DOTALL,
    )
    assert func, "_run_simulation not found"
    body = func.group(0)
    assert "with self._subscriptions_lock" in body, (
        "CONC-06: simulation loop must snapshot subscriptions under the lock"
    )


# ───────────── CONC-08: SIGTERM handler installed in run() ─────────────


def test_conc08_run_installs_sigterm_handler():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    run_block = re.search(
        r"def run\(self, poll_interval.*?\n    def ", src, re.DOTALL,
    )
    assert run_block, "run() body not located"
    body = run_block.group(0)
    assert "SIGTERM" in body, "CONC-08: run() must install SIGTERM handler"
    assert "SIGINT" in body, "CONC-08: run() must install SIGINT handler"
    assert "_running = False" in body, "shutdown handler must flip _running"


# ───────────── CONC-09: WS thread join in shutdown ─────────────


def test_conc09_websocket_client_exposes_join():
    from core.websocket_client import WebSocketClient
    assert hasattr(WebSocketClient, "join"), "CONC-09: WebSocketClient must expose join()"


def test_conc09_join_returns_true_when_no_thread_running():
    from core.websocket_client import WebSocketClient
    ws = WebSocketClient(broker="paper", config={"broker": {}})
    assert ws.join(timeout=0.1) is True, (
        "CONC-09: join() must return True when no worker thread was ever started"
    )


def test_conc09_shutdown_calls_ws_join():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    shutdown = re.search(
        r"def _shutdown\(self.*?\n    def |def _shutdown\(self.*?\Z",
        src, re.DOTALL,
    )
    assert shutdown, "_shutdown not found"
    body = shutdown.group(0)
    assert "self.ws_client.join(" in body, (
        "CONC-09: _shutdown must call ws_client.join() to drain WS threads"
    )


# ───────────── STATE-03: cooldown attrs initialised before reconcile ─────────────


def test_state03_stock_loss_today_init_precedes_reconcile_in_init():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    eager = src.find('self._stock_loss_today: Dict[str, int] = {}')
    reconcile = src.find("reconcile_positions_with_broker(")
    assert eager >= 0, "STATE-03: eager init of _stock_loss_today missing"
    assert reconcile >= 0, "reconcile call not found in trading_agent.py"
    assert eager < reconcile, (
        "STATE-03: _stock_loss_today must be initialised BEFORE reconcile is called "
        f"(eager={eager}, reconcile={reconcile})"
    )


def test_state03_late_init_does_not_clobber_pre_set_values():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    assert (
        "if not hasattr(self, \"_stock_loss_today\"):" in src
    ), (
        "STATE-03: late init of _stock_loss_today must guard against "
        "clobbering values already set by reconcile"
    )


# ───────────── STATE-04: atomic close_position ─────────────


def test_state04_database_exposes_close_position_atomic(tmp_path):
    from core.database import Database
    db = Database(db_path=str(tmp_path / "t.db"))
    assert hasattr(db, "close_position_atomic"), (
        "STATE-04: Database must expose close_position_atomic"
    )


def test_state04_close_position_atomic_writes_all_three_in_one_txn(tmp_path):
    from core.database import Database
    db = Database(db_path=str(tmp_path / "atomic.db"))
    db.save_open_position(
        symbol="RELIANCE", side="BUY", entry_price=2400.0, quantity=10,
        entry_time="2026-05-28T09:30:00+05:30",
    )
    trade = {
        "symbol": "RELIANCE", "side": "BUY",
        "entry_price": 2400.0, "exit_price": 2410.0,
        "quantity": 10,
        "entry_time": "2026-05-28T09:30:00+05:30",
        "exit_time": "2026-05-28T11:00:00+05:30",
        "pnl": 100.0, "pnl_pct": 0.4, "strategy": "test",
        "exit_reason": "tp", "commission": 5.0,
    }
    db.close_position_atomic(
        symbol="RELIANCE", trade=trade,
        equity=100100.0, cash=100100.0, positions=0,
    )
    assert db.load_open_positions() == []
    trades = db.load_trades()
    assert not trades.empty
    assert trades.iloc[0]["symbol"] == "RELIANCE"
    eq = db.load_equity_curve()
    assert not eq.empty


def test_state04_portfolio_close_position_uses_atomic_helper():
    src = (ROOT / "packages" / "core" / "portfolio.py").read_text(encoding="utf-8")
    close_idx = src.find("def close_position(")
    assert close_idx >= 0, "close_position body not found"
    next_def = src.find("\n    def ", close_idx + 1)
    body = src[close_idx:next_def] if next_def > 0 else src[close_idx:]
    assert "close_position_atomic(" in body, (
        "STATE-04: Portfolio.close_position must route through close_position_atomic"
    )


# ───────────── STATE-06: file_lock retry-with-backoff (no unlocked fallback) ─────────────


def test_state06_cooldown_save_retries_on_lock_timeout():
    src = (ROOT / "packages" / "core" / "cooldown_persistence.py").read_text(encoding="utf-8")
    save_block = re.search(
        r"def save_cooldown_state\(.*?def load_cooldown_state\(",
        src, re.DOTALL,
    )
    assert save_block, "save_cooldown_state body not found"
    body = save_block.group(0)
    assert "for attempt, timeout_s in enumerate((1.0, 3.0, 5.0)" in body, (
        "STATE-06: save_cooldown_state must retry with backoff on lock timeout"
    )
    # And it must NOT silently fall back to an unlocked write inside
    # the retry loop.
    assert (
        "Best-effort fallback: write without the lock" not in body
    ), (
        "STATE-06: cooldown save must not fall back to unlocked write"
    )


def test_state06_runtime_state_save_retries_on_lock_timeout():
    src = (ROOT / "packages" / "core" / "runtime_state_persistence.py").read_text(encoding="utf-8")
    assert "for attempt, timeout_s in enumerate((1.0, 3.0, 5.0)" in src, (
        "STATE-06: save_runtime_state must retry with backoff"
    )


def test_state06_trailing_state_save_retries_on_lock_timeout():
    src = (ROOT / "packages" / "core" / "trailing_stop_persistence.py").read_text(encoding="utf-8")
    assert "for attempt, timeout_s in enumerate((1.0, 3.0, 5.0)" in src, (
        "STATE-06: save_trailing_states must retry with backoff"
    )


# ───────────── STATE-08: debounced trail persist ─────────────


def test_state08_persist_trailing_states_debounced_exists():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    assert "def _persist_trailing_states_debounced(self" in src, (
        "STATE-08: TradingAgent must expose _persist_trailing_states_debounced"
    )


def test_state08_on_tick_persists_after_real_mutation():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    on_tick = re.search(r"def _on_tick\(self.*?\n    def ", src, re.DOTALL).group(0)
    assert "_persist_trailing_states_debounced()" in on_tick, (
        "STATE-08: WS tick must persist on trail mutation"
    )
    # Mutation gate prevents a no-op tick from burning debounce budget.
    assert "trail_mutated" in on_tick


# ───────────── STATE-09: corrupt cooldown JSON sentinel + gate ─────────────


def test_state09_loader_writes_corrupt_flag_on_bad_json(tmp_path):
    from core.cooldown_persistence import load_cooldown_state
    bad = tmp_path / "cooldowns.json"
    bad.write_text("{this is not json", encoding="utf-8")
    from datetime import timedelta
    cd, sl, rj, side = load_cooldown_state(
        reentry_cooldown=timedelta(minutes=30),
        rejection_cooldown=timedelta(minutes=30),
        data_dir=tmp_path,
    )
    assert cd == {} and sl == {} and rj == {} and side == {}
    flag = tmp_path / "cooldowns_corrupt.flag"
    assert flag.exists(), (
        "STATE-09: corrupt JSON load must write cooldowns_corrupt.flag sentinel"
    )
    body = flag.read_text(encoding="utf-8")
    assert "corrupt_at=" in body
    assert "action_required=" in body


def test_state09_trading_agent_open_new_position_blocks_on_corrupt_flag():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    idx = src.find("def _open_new_position(")
    assert idx >= 0, "_open_new_position not found"
    next_def = src.find("\n    def ", idx + 1)
    body = src[idx:next_def] if next_def > 0 else src[idx:]
    assert "_cooldown_state_corrupt" in body, (
        "STATE-09: _open_new_position must check the corrupt-state gate"
    )
    assert "cooldown_state_corrupt" in body, "audit-reject reason missing"


# ───────────── STATE-11: signal audit retry queue ─────────────


def test_state11_signal_audit_queues_failed_rows(tmp_path, monkeypatch):
    from core.signal_audit import SignalAudit

    sa = SignalAudit(log_dir=str(tmp_path))
    # Force the internal write to fail once by replacing the path
    # method to point at an unwritable directory.
    bad = tmp_path / "no" / "such" / "dir" / "x.csv"
    monkeypatch.setattr(sa, "_path_for_today", lambda: str(bad))
    sa.log(
        symbol="RELIANCE", direction="BUY", confidence=0.7,
        regime="bull", price=2400.0, strategy="rsi", contributing=None,
        outcome="ACCEPTED", reason="",
    )
    assert len(sa._retry_queue) == 1, (
        "STATE-11: failed CSV append must enqueue the row for retry"
    )


def test_state11_signal_audit_drains_on_next_log(tmp_path, monkeypatch):
    from core.signal_audit import SignalAudit
    sa = SignalAudit(log_dir=str(tmp_path))
    # 1) fail once
    bad = tmp_path / "no" / "such" / "dir" / "x.csv"
    monkeypatch.setattr(sa, "_path_for_today", lambda: str(bad))
    sa.log(
        symbol="A", direction="BUY", confidence=0.9, regime="bull",
        price=100.0, strategy="x", contributing=None, outcome="ACCEPTED",
    )
    assert len(sa._retry_queue) == 1
    # 2) restore, log again -> drain happens
    good = tmp_path / "ok.csv"
    # write the header so DictWriter doesn't have to handle the
    # missing-header edge case.
    import csv as _csv
    from core.signal_audit import _COLUMNS
    with open(good, "w", newline="", encoding="utf-8") as f:
        _csv.writer(f).writerow(_COLUMNS)
    monkeypatch.setattr(sa, "_path_for_today", lambda: str(good))
    sa.log(
        symbol="B", direction="SELL", confidence=0.85, regime="bear",
        price=200.0, strategy="y", contributing=None, outcome="ACCEPTED",
    )
    assert len(sa._retry_queue) == 0, (
        "STATE-11: queued row must be flushed on the next successful log() call"
    )
    rows = list(_csv.DictReader(open(good, "r", encoding="utf-8")))
    symbols = [r["symbol"] for r in rows]
    assert symbols == ["A", "B"], "FIFO order must be preserved"


# ───────────── STATE-12: stale MIS sweep on day boundary ─────────────


def test_state12_reset_daily_trackers_sweeps_stale_overnight_positions():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    reset = re.search(
        r"def _reset_daily_trackers\(self.*?\n    def ", src, re.DOTALL,
    )
    assert reset, "_reset_daily_trackers not found"
    body = reset.group(0)
    assert "stale_overnight_mis_sweep" in body, (
        "STATE-12: daily reset must sweep stale overnight positions"
    )
    assert "exit_reason=\"stale_overnight_mis_sweep\"" in body or \
           "exit_reason='stale_overnight_mis_sweep'" in body
    assert "entry_date < today" in body


# ───────────── CONC-05 / PERF-05: tick batching ─────────────


def test_perf05_trading_agent_has_buffered_tick_helpers():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    assert "def _buffer_tick(" in src, (
        "PERF-05: TradingAgent must expose _buffer_tick"
    )
    assert "def _flush_tick_buffer(self" in src, (
        "PERF-05: TradingAgent must expose _flush_tick_buffer"
    )


def test_perf05_on_tick_calls_buffer_not_store_tick():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    on_tick = re.search(r"def _on_tick\(self.*?\n    def ", src, re.DOTALL).group(0)
    assert "self._buffer_tick(" in on_tick, (
        "PERF-05: WS hot path must enqueue ticks via _buffer_tick"
    )
    assert "self.database.store_tick(" not in on_tick, (
        "PERF-05: per-tick DB write must be replaced by buffered batch"
    )


def test_perf05_shutdown_flushes_tick_buffer():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    shutdown = re.search(
        r"def _shutdown\(self.*?\n    def |def _shutdown\(self.*?\Z",
        src, re.DOTALL,
    )
    assert shutdown
    assert "_flush_tick_buffer(" in shutdown.group(0), (
        "PERF-05: _shutdown must flush the tick buffer before tearing down"
    )
