"""Tests for the 2026-05-20 pre-flight validation in run_battery_queue.

Each `validate_job_args` check guards against a real failure mode we've
hit (or been one typo away from hitting) on the backtester VM:
  * unresolvable universe-file -> wasted ~30s of docker startup
  * non-positive `days` -> empty results, silent failure
  * non-integer / negative `workers` -> argv crash inside container
  * unknown `interval` -> yfinance returns nothing, also silent

These tests pin "fail at scheduler load time, not 60s into a docker run"
as the contract.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import run_battery_queue as q  # noqa: E402


def _write_queue(tmp_path: Path, jobs: list) -> Path:
    qp = tmp_path / "q.yaml"
    qp.write_text(yaml.safe_dump({
        "schema_version": 1,
        "jobs": jobs,
    }), encoding="utf-8")
    return qp


# ────────────────────── universe-file ──────────────────────
class TestUniverseFile:
    def test_missing_universe_file_rejected(self, tmp_path):
        qp = _write_queue(tmp_path, [{
            "name": "j1",
            "universe-file": "tests/fixtures/does_not_exist.json",
        }])
        with pytest.raises(SystemExit) as exc:
            q.load_queue(qp)
        msg = str(exc.value)
        assert "universe-file" in msg
        assert "does_not_exist" in msg

    def test_existing_universe_file_accepted(self, tmp_path):
        # Use a real fixture that ships with the repo.
        qp = _write_queue(tmp_path, [{
            "name": "j1",
            "universe-file": "tests/fixtures/nifty50_universe.json",
        }])
        jobs = q.load_queue(qp)
        assert len(jobs) == 1
        assert jobs[0]["universe-file"] == "tests/fixtures/nifty50_universe.json"

    def test_no_universe_file_is_fine(self, tmp_path):
        qp = _write_queue(tmp_path, [{"name": "j1", "days": 30}])
        jobs = q.load_queue(qp)
        assert jobs[0]["name"] == "j1"


# ────────────────────── days ──────────────────────
class TestDays:
    def test_zero_days_rejected(self, tmp_path):
        qp = _write_queue(tmp_path, [{"name": "j1", "days": 0}])
        with pytest.raises(SystemExit) as exc:
            q.load_queue(qp)
        assert "days" in str(exc.value)

    def test_negative_days_rejected(self, tmp_path):
        qp = _write_queue(tmp_path, [{"name": "j1", "days": -1}])
        with pytest.raises(SystemExit):
            q.load_queue(qp)

    def test_string_days_rejected(self, tmp_path):
        qp = _write_queue(tmp_path, [{"name": "j1", "days": "30"}])
        with pytest.raises(SystemExit):
            q.load_queue(qp)

    def test_positive_int_days_accepted(self, tmp_path):
        qp = _write_queue(tmp_path, [{"name": "j1", "days": 90}])
        jobs = q.load_queue(qp)
        assert jobs[0]["days"] == 90


# ────────────────────── workers ──────────────────────
class TestWorkers:
    def test_workers_auto_string_accepted(self, tmp_path):
        qp = _write_queue(tmp_path, [{"name": "j1", "workers": "auto"}])
        jobs = q.load_queue(qp)
        assert jobs[0]["workers"] == "auto"

    def test_workers_zero_rejected(self, tmp_path):
        qp = _write_queue(tmp_path, [{"name": "j1", "workers": 0}])
        with pytest.raises(SystemExit):
            q.load_queue(qp)

    def test_workers_negative_rejected(self, tmp_path):
        qp = _write_queue(tmp_path, [{"name": "j1", "workers": -2}])
        with pytest.raises(SystemExit):
            q.load_queue(qp)

    def test_workers_non_auto_string_rejected(self, tmp_path):
        qp = _write_queue(tmp_path, [{"name": "j1", "workers": "many"}])
        with pytest.raises(SystemExit):
            q.load_queue(qp)

    def test_workers_bool_rejected(self, tmp_path):
        # PyYAML can promote `true`/`false` to bool; both nonsense for workers.
        qp = _write_queue(tmp_path, [{"name": "j1", "workers": True}])
        with pytest.raises(SystemExit):
            q.load_queue(qp)


# ────────────────────── interval ──────────────────────
class TestInterval:
    def test_known_intervals_accepted(self, tmp_path):
        for iv in ("5m", "5min", "15m", "30m", "1h", "1d"):
            qp = _write_queue(tmp_path, [{"name": "j1", "interval": iv}])
            jobs = q.load_queue(qp)
            assert jobs[0]["interval"] == iv

    def test_unknown_interval_rejected(self, tmp_path):
        for bad in ("hourly", "5M ", "minute", "2m"):
            qp = _write_queue(tmp_path, [{"name": "j1", "interval": bad}])
            with pytest.raises(SystemExit):
                q.load_queue(qp)


# ────────────────────── compound rejection messages ──────────────────────
class TestErrorMessages:
    def test_error_includes_job_name(self, tmp_path):
        qp = _write_queue(tmp_path, [
            {"name": "first_ok", "days": 30},
            {"name": "second_bad", "days": 0},
        ])
        with pytest.raises(SystemExit) as exc:
            q.load_queue(qp)
        # The bad job's name must appear so operators know which one to fix.
        assert "second_bad" in str(exc.value)
