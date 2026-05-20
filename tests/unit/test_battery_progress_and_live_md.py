"""Tests for the 2026-05-20 battery infrastructure hardening.

What this batch landed (all freeze-bypassed, "fix battery irrespective"):

  1. Tightened [BATTERY-PROGRESS] cadence in research.backtest_ensemble:
     emit on event-count OR wall-clock, whichever fires first. Pre-fix,
     a 5 ev/s VM emitted only every 33 min — unusable.

  2. Worker watchdog (research.battery): a daemon thread that suicides
     the worker via os._exit(124) if the worker's log file mtime stalls
     for > BATTERY_WATCHDOG_SILENCE_MIN minutes. Defends against
     deadlocked / GIL-stuck workers blocking the queue.

  3. Live comparison.md: parent's parallel path spawns a thread that
     writes a "Currently running" block every 60s, populated from the
     latest [BATTERY-PROGRESS] line in each worker's log.

  4. Worker `_parse_last_progress()` helper that the live thread (and
     the cloud status tool indirectly) uses to extract the most recent
     progress payload from a multi-megabyte worker log without reading
     the whole file.

These tests pin those contracts so a future "small" refactor doesn't
silently regress the operator's mid-run visibility.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from research import battery  # noqa: E402
from research import backtest_ensemble  # noqa: E402


# ────────────────────────── progress parsing ──────────────────────────
class TestParseLastProgress:
    def test_returns_none_when_log_missing(self, tmp_path):
        assert battery._parse_last_progress(tmp_path / "nope.log") is None

    def test_returns_none_when_log_has_no_progress_lines(self, tmp_path):
        log = tmp_path / "v.log"
        log.write_text(
            "2026-05-20 09:00:00 | INFO | x.y:z:1 - just startup chatter\n"
            "2026-05-20 09:00:01 | INFO | a.b:c:2 - more chatter\n",
            encoding="utf-8",
        )
        assert battery._parse_last_progress(log) is None

    def test_parses_a_real_line(self, tmp_path):
        log = tmp_path / "v.log"
        log.write_text(
            "2026-05-20 09:08:43 | INFO    | research.backtest_ensemble:run:225 - "
            "[BATTERY-PROGRESS] 610,000/975,292 ( 62.5%) | sim_date=2026-04-15 "
            "| rate=5 ev/s | elapsed=33.4h | ETA=20.0h\n",
            encoding="utf-8",
        )
        got = battery._parse_last_progress(log)
        assert got is not None
        assert got["done"] == 610_000
        assert got["total"] == 975_292
        assert got["pct"] == pytest.approx(62.5)
        assert got["sim_date"] == "2026-04-15"
        assert got["rate_ev_s"] == 5
        assert got["elapsed"] == "33.4h"
        assert got["eta"] == "20.0h"

    def test_returns_most_recent_progress_when_multiple(self, tmp_path):
        log = tmp_path / "v.log"
        log.write_text(
            "[BATTERY-PROGRESS] 100,000/975,292 ( 10.3%) | sim_date=2026-04-01 "
            "| rate=5 ev/s | elapsed=1.0h | ETA=8.0h\n"
            "[BATTERY-PROGRESS] 200,000/975,292 ( 20.5%) | sim_date=2026-04-04 "
            "| rate=5 ev/s | elapsed=2.0h | ETA=7.0h\n"
            "[BATTERY-PROGRESS] 300,000/975,292 ( 30.8%) | sim_date=2026-04-07 "
            "| rate=5 ev/s | elapsed=3.0h | ETA=6.0h\n",
            encoding="utf-8",
        )
        got = battery._parse_last_progress(log)
        assert got["done"] == 300_000
        assert got["pct"] == pytest.approx(30.8)
        assert got["sim_date"] == "2026-04-07"

    def test_only_reads_tail_for_very_large_files(self, tmp_path):
        """Sanity: stays cheap on multi-MB worker logs (helper reads last 32KB)."""
        log = tmp_path / "v.log"
        # 64KB of noise then one progress line — must still find the line
        # because it's within the last 32KB.
        noise = ("2026-05-20 09:00:00 | INFO | mod:fn:1 - chatter line\n" * 1000)
        progress = (
            "[BATTERY-PROGRESS] 999,999/1,000,000 ( 99.9%) | sim_date=2026-04-30 "
            "| rate=5 ev/s | elapsed=55.0h | ETA=0.1h\n"
        )
        # The progress line goes at the end — within the tail window.
        log.write_text(noise + progress, encoding="utf-8")
        got = battery._parse_last_progress(log)
        assert got is not None
        assert got["pct"] == pytest.approx(99.9)

    def test_handles_malformed_progress_line_gracefully(self, tmp_path):
        log = tmp_path / "v.log"
        # Looks like a progress line but the numbers are gibberish — the
        # regex shouldn't match, so we return None (not crash).
        log.write_text(
            "[BATTERY-PROGRESS] X/Y (?%) | sim_date=??? | rate=NaN ev/s "
            "| elapsed=? | ETA=?\n",
            encoding="utf-8",
        )
        assert battery._parse_last_progress(log) is None


# ────────────────────────── active worker scan ──────────────────────────
class TestReadActiveWorkers:
    def _make_worker_log(self, dir_path: Path, name: str, pct: float,
                          mtime_offset_sec: float = 0.0) -> Path:
        dir_path.mkdir(parents=True, exist_ok=True)
        log = dir_path / f"{name}.log"
        log.write_text(
            f"[BATTERY-PROGRESS] {int(pct*10000):,}/1,000,000 ( {pct:5.1f}%) "
            f"| sim_date=2026-04-15 | rate=10 ev/s | elapsed=1.0h | ETA=2.0h\n",
            encoding="utf-8",
        )
        if mtime_offset_sec:
            now = time.time()
            os.utime(log, (now + mtime_offset_sec, now + mtime_offset_sec))
        return log

    def test_empty_when_workers_dir_missing(self, tmp_path):
        out = battery._read_active_workers(tmp_path / "workers", set())
        assert out == []

    def test_returns_active_progress_payloads(self, tmp_path):
        wd = tmp_path / "workers"
        self._make_worker_log(wd, "V1", 25.0)
        self._make_worker_log(wd, "V2", 60.0)
        active = battery._read_active_workers(wd, completed_names=set())
        assert len(active) == 2
        # Sorted most-progressed-first.
        assert active[0]["variant"] == "V2"
        assert active[0]["pct"] == pytest.approx(60.0)
        assert active[1]["variant"] == "V1"

    def test_skips_completed_variants(self, tmp_path):
        wd = tmp_path / "workers"
        self._make_worker_log(wd, "V1", 25.0)
        self._make_worker_log(wd, "V2", 60.0)
        active = battery._read_active_workers(wd, completed_names={"V1"})
        assert len(active) == 1
        assert active[0]["variant"] == "V2"

    def test_skips_stale_logs(self, tmp_path):
        wd = tmp_path / "workers"
        # Stale: mtime 600s in the past, max_age default is 300s.
        self._make_worker_log(wd, "V1", 25.0, mtime_offset_sec=-600)
        self._make_worker_log(wd, "V2", 60.0, mtime_offset_sec=0)
        active = battery._read_active_workers(wd, completed_names=set())
        names = [w["variant"] for w in active]
        assert names == ["V2"]

    def test_skips_workers_without_progress_lines(self, tmp_path):
        wd = tmp_path / "workers"
        wd.mkdir()
        (wd / "V_noprog.log").write_text(
            "2026-05-20 | INFO | x:y:1 - no progress markers here\n",
            encoding="utf-8",
        )
        self._make_worker_log(wd, "V_progress", 50.0)
        active = battery._read_active_workers(wd, completed_names=set())
        assert [w["variant"] for w in active] == ["V_progress"]


# ────────────────────────── comparison.md rendering ──────────────────────────
class TestWriteComparisonActiveBlock:
    def _meta(self):
        return {
            "run_id": "test_run",
            "started": "2026-05-20T09:00:00",
            "finished": "2026-05-20T10:00:00",
            "symbols": ["RELIANCE"],
            "days": 30,
            "interval": "5m",
            "capital": 25000.0,
            "total_variants": 5,
        }

    def test_renders_currently_running_block(self, tmp_path):
        out = tmp_path / "comparison.md"
        active = [{
            "variant": "V1_baseline",
            "pct": 62.5,
            "sim_date": "2026-04-15",
            "rate_ev_s": 5,
            "elapsed": "33.4h",
            "eta": "20.0h",
            "log_age_sec": 30,
        }]
        battery._write_comparison(
            rows=[], out_path=out, meta=self._meta(),
            complete=False, failed=[], active_workers=active,
        )
        text = out.read_text(encoding="utf-8")
        assert "## Currently running" in text
        assert "V1_baseline" in text
        assert "62.5" in text
        assert "2026-04-15" in text

    def test_active_block_omitted_on_complete(self, tmp_path):
        out = tmp_path / "comparison.md"
        active = [{
            "variant": "V1", "pct": 100.0, "sim_date": "2026-05-01",
            "rate_ev_s": 5, "elapsed": "1.0h", "eta": "0.0h",
            "log_age_sec": 0,
        }]
        battery._write_comparison(
            rows=[], out_path=out, meta=self._meta(),
            complete=True, failed=[], active_workers=active,
        )
        text = out.read_text(encoding="utf-8")
        # COMPLETE marker present, currently-running block absent.
        assert "[COMPLETE]" in text
        assert "## Currently running" not in text

    def test_no_block_when_active_list_empty(self, tmp_path):
        out = tmp_path / "comparison.md"
        battery._write_comparison(
            rows=[], out_path=out, meta=self._meta(),
            complete=False, failed=[], active_workers=[],
        )
        text = out.read_text(encoding="utf-8")
        assert "## Currently running" not in text


# ────────────────────────── live-md thread ──────────────────────────
class TestLiveMdLoop:
    def _meta(self):
        return {
            "run_id": "test_run",
            "started": "2026-05-20T09:00:00",
            "finished": "2026-05-20T10:00:00",
            "symbols": ["RELIANCE"],
            "days": 30,
            "interval": "5m",
            "capital": 25000.0,
            "total_variants": 1,
        }

    def test_loop_writes_comparison_md_then_exits_on_event(self, tmp_path):
        out_root = tmp_path / "run"
        (out_root / "workers").mkdir(parents=True)
        # Drop a worker log with a progress line so the active block
        # actually has content to render.
        (out_root / "workers" / "V1.log").write_text(
            "[BATTERY-PROGRESS] 50,000/100,000 ( 50.0%) | sim_date=2026-04-15 "
            "| rate=10 ev/s | elapsed=1.0h | ETA=1.0h\n",
            encoding="utf-8",
        )

        meta_holder = self._meta()

        def _state():
            return ([], [], set(), meta_holder)

        stop_event = threading.Event()
        # Use a tiny interval so the test finishes fast. The first wait
        # is interval_sec so we set the event after a short sleep to
        # observe at least one write.
        t = threading.Thread(
            target=battery._live_md_loop,
            args=(out_root, _state, stop_event),
            kwargs={"interval_sec": 0.05},
            daemon=True,
        )
        t.start()
        time.sleep(0.25)  # let it tick a few times
        stop_event.set()
        t.join(timeout=2)
        assert not t.is_alive(), "live-md thread should exit promptly on event"

        comp = (out_root / "comparison.md").read_text(encoding="utf-8")
        assert "## Currently running" in comp
        assert "V1" in comp


# ────────────────────────── watchdog ──────────────────────────
class TestWatchdog:
    def test_silence_sec_default_is_30_min(self, monkeypatch):
        monkeypatch.delenv("BATTERY_WATCHDOG_SILENCE_MIN", raising=False)
        assert battery._watchdog_silence_sec() == 30 * 60

    def test_silence_sec_respects_env(self, monkeypatch):
        monkeypatch.setenv("BATTERY_WATCHDOG_SILENCE_MIN", "5")
        assert battery._watchdog_silence_sec() == 5 * 60

    def test_silence_sec_zero_disables(self, monkeypatch):
        monkeypatch.setenv("BATTERY_WATCHDOG_SILENCE_MIN", "0")
        assert battery._watchdog_silence_sec() == 0

    def test_silence_sec_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("BATTERY_WATCHDOG_SILENCE_MIN", "thirty")
        assert battery._watchdog_silence_sec() == 30 * 60

    def test_spawn_returns_none_when_disabled(self, tmp_path):
        log = tmp_path / "v.log"
        log.write_text("hi", encoding="utf-8")
        t = battery._spawn_progress_watchdog(log, "V1", max_silence_sec=0)
        assert t is None

    def test_spawn_returns_daemon_thread(self, tmp_path):
        log = tmp_path / "v.log"
        log.write_text("hi", encoding="utf-8")
        t = battery._spawn_progress_watchdog(log, "V1", max_silence_sec=3600)
        assert t is not None
        assert t.daemon is True
        assert "watchdog" in t.name.lower()


# ────────────────────────── progress emission cadence ──────────────────────────
class TestProgressEmissionCadence:
    """Pin the contract: progress emits on EVENTS or TIME, whichever first."""

    def test_constants_define_dual_thresholds(self):
        # If either threshold is removed, mid-run visibility regresses
        # on either fast (no event trigger) or slow (no time trigger) VMs.
        assert backtest_ensemble.PROGRESS_LOG_INTERVAL_EVENTS >= 1
        assert backtest_ensemble.PROGRESS_LOG_INTERVAL_SECONDS > 0

    def test_time_threshold_is_under_two_minutes(self):
        # Operator-facing UX contract: status tool refresh assumes
        # progress lines are no more than ~60s apart on the slow VM.
        assert backtest_ensemble.PROGRESS_LOG_INTERVAL_SECONDS <= 120


# ────────────────────────── workers='auto' resolution ──────────────────────────
class TestWorkersAutoFlag:
    """Pin the contract on _resolve_workers so the queue scheduler and
    the CLI both behave the same way."""

    def test_auto_resolves_to_cpu_minus_one(self):
        resolved, msg = battery._resolve_workers("auto", cpu_count=8)
        assert resolved == 7
        assert msg is not None
        assert "auto" in msg
        assert "resolved to 7" in msg
        assert "cpu_count=8" in msg

    def test_auto_clamps_to_one_on_single_core_box(self):
        # cpu_count=1 -> max(1, 0) = 1; never returns 0 (that'd disable
        # the parallel path entirely).
        resolved, _ = battery._resolve_workers("auto", cpu_count=1)
        assert resolved == 1

    def test_auto_case_insensitive(self):
        for variant in ("auto", "AUTO", "Auto", "  auto  "):
            resolved, msg = battery._resolve_workers(variant, cpu_count=4)
            assert resolved == 3
            assert msg is not None

    def test_integer_string_passes_through(self):
        resolved, msg = battery._resolve_workers("4", cpu_count=8)
        assert resolved == 4
        assert msg is None  # no announcement for explicit integers

    def test_actual_int_passes_through(self):
        resolved, msg = battery._resolve_workers(2, cpu_count=8)
        assert resolved == 2
        assert msg is None

    def test_invalid_string_raises_value_error(self):
        with pytest.raises(ValueError, match="must be 'auto' or an integer"):
            battery._resolve_workers("many", cpu_count=4)

    def test_negative_int_passes_through_for_caller_to_clamp(self):
        # The CLI applies its own sanity clamp; _resolve_workers's job is
        # just to convert the raw value to an int.
        resolved, _ = battery._resolve_workers("-3", cpu_count=8)
        assert resolved == -3
