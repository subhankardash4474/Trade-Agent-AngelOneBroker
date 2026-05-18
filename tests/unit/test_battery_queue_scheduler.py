"""Unit tests for tools/run_battery_queue.py.

Scope: the parts that can be tested without a live docker daemon, namely
queue parsing, state I/O, docker argv composition, skip-already-done
logic, and the broker-cred safety guard.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# Make `tools` importable as a package even though it isn't packaged.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import run_battery_queue as q  # noqa: E402


# ──────────────────────── queue file parsing ────────────────────────
class TestQueueParsing:
    def test_minimal_valid_queue(self, tmp_path):
        qp = tmp_path / "q.yaml"
        qp.write_text(yaml.safe_dump({
            "schema_version": 1,
            "jobs": [{"name": "j1", "days": 30}],
        }), encoding="utf-8")
        jobs = q.load_queue(qp)
        assert len(jobs) == 1
        assert jobs[0]["name"] == "j1"
        assert jobs[0]["days"] == 30

    def test_missing_file_raises_systemexit(self, tmp_path):
        with pytest.raises(SystemExit):
            q.load_queue(tmp_path / "does_not_exist.yaml")

    def test_invalid_yaml_raises_systemexit(self, tmp_path):
        qp = tmp_path / "q.yaml"
        qp.write_text("not: valid: : :", encoding="utf-8")
        with pytest.raises(SystemExit):
            q.load_queue(qp)

    def test_wrong_schema_version_refused(self, tmp_path):
        qp = tmp_path / "q.yaml"
        qp.write_text(yaml.safe_dump({
            "schema_version": 99,
            "jobs": [{"name": "j1"}],
        }), encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            q.load_queue(qp)
        assert "schema_version" in str(exc.value)

    def test_jobs_not_a_list_refused(self, tmp_path):
        qp = tmp_path / "q.yaml"
        qp.write_text(yaml.safe_dump({
            "schema_version": 1,
            "jobs": {"not_a": "list"},
        }), encoding="utf-8")
        with pytest.raises(SystemExit):
            q.load_queue(qp)

    def test_job_missing_name_refused(self, tmp_path):
        qp = tmp_path / "q.yaml"
        qp.write_text(yaml.safe_dump({
            "schema_version": 1,
            "jobs": [{"days": 30}],   # no name
        }), encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            q.load_queue(qp)
        assert "name" in str(exc.value)

    def test_duplicate_job_names_refused(self, tmp_path):
        # Duplicate names would cause the state file to overwrite itself
        # silently -- refuse loudly instead.
        qp = tmp_path / "q.yaml"
        qp.write_text(yaml.safe_dump({
            "schema_version": 1,
            "jobs": [{"name": "j1"}, {"name": "j1"}],
        }), encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            q.load_queue(qp)
        assert "duplicate" in str(exc.value).lower()

    def test_packaged_example_queue_is_valid(self):
        # The example we ship in tests/fixtures must parse cleanly --
        # operators copy it verbatim.
        example = ROOT / "tests" / "fixtures" / "battery_queue_example.yaml"
        assert example.exists(), f"missing example queue at {example}"
        jobs = q.load_queue(example)
        assert len(jobs) >= 1
        names = {j["name"] for j in jobs}
        assert len(names) == len(jobs), "duplicate names in shipped example"


# ──────────────────────── state file I/O ────────────────────────
class TestStatePersistence:
    def test_load_missing_returns_empty_schema(self, tmp_path):
        s = q.load_state(tmp_path / "nope.json")
        assert s == {"schema_version": 1, "jobs": {}}

    def test_load_corrupt_returns_empty_schema(self, tmp_path):
        sp = tmp_path / "s.json"
        sp.write_text("{not json", encoding="utf-8")
        s = q.load_state(sp)
        assert s["jobs"] == {}

    def test_load_wrong_schema_returns_empty(self, tmp_path):
        sp = tmp_path / "s.json"
        sp.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
        s = q.load_state(sp)
        assert s["jobs"] == {}

    def test_save_then_load_round_trip(self, tmp_path):
        sp = tmp_path / "s.json"
        original = {
            "schema_version": 1,
            "jobs": {"j1": {"status": "completed", "exit_code": 0}},
        }
        q.save_state(original, sp)
        reloaded = q.load_state(sp)
        assert reloaded == original

    def test_save_is_atomic(self, tmp_path):
        # A failed write should not corrupt the on-disk file. We can't
        # easily simulate a crash, but we can verify the tmp file gets
        # cleaned up by checking save uses os.replace (atomic rename).
        sp = tmp_path / "s.json"
        original = {"schema_version": 1, "jobs": {"a": {}}}
        q.save_state(original, sp)
        # No .tmp leftovers.
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == [], f"tmp leftovers: {leftovers}"


# ──────────────────────── docker argv ────────────────────────
class TestDockerArgvComposition:
    def _job(self, **overrides):
        base = {
            "name": "j_test",
            "days": 90,
            "workers": 2,
            "interval": "5m",
            "universe-file": "tests/fixtures/battery_v2_universe.json",
        }
        base.update(overrides)
        return base

    def test_basic_argv_shape(self):
        job = self._job()
        argv = q.build_docker_run_argv(job, "battery_x_T1", "trading-agent:latest")
        # Core docker run skeleton present
        assert argv[:3] == ["sudo", "docker", "run"]
        assert "--name" in argv
        assert "battery_x_T1" in argv
        assert "--no-healthcheck" in argv
        assert "trading-agent:latest" in argv
        # BACKTESTER_MODE wired
        idx = argv.index("-e")
        assert argv[idx + 1] == "BACKTESTER_MODE=1"

    def test_args_forwarded_as_long_flags(self):
        job = self._job()
        argv = q.build_docker_run_argv(job, "rid", "img")
        # --days 90, --workers 2, --interval 5m must all appear AFTER
        # the python script name (tools/run_battery.py).
        post = argv[argv.index("tools/run_battery.py"):]
        assert "--days" in post and "90" in post
        assert "--workers" in post and "2" in post
        assert "--interval" in post and "5m" in post
        assert "--universe-file" in post

    def test_run_id_appended_last(self):
        # Scheduler controls the run_id. If the queue accidentally
        # specifies run-id, ours must still win.
        job = self._job(**{"run-id": "operator_attempt"})
        argv = q.build_docker_run_argv(job, "scheduler_rid", "img")
        post = argv[argv.index("tools/run_battery.py"):]
        # Only ONE --run-id arg and value, and it's ours.
        rid_indices = [i for i, v in enumerate(post) if v == "--run-id"]
        assert len(rid_indices) == 1
        assert post[rid_indices[0] + 1] == "scheduler_rid"

    def test_name_key_is_internal_not_forwarded(self):
        # `name` is a scheduler concept; it must not leak as --name to
        # run_battery.py (which doesn't take it).
        job = self._job()
        argv = q.build_docker_run_argv(job, "rid", "img")
        post = argv[argv.index("tools/run_battery.py"):]
        # `--name` is the docker arg before the image; argparse for
        # run_battery doesn't know `--name`.
        assert post.count("--name") == 0

    def test_list_value_expands(self):
        job = self._job(variants=["V1", "V2", "V3"])
        argv = q.build_docker_run_argv(job, "rid", "img")
        post = argv[argv.index("tools/run_battery.py"):]
        idx = post.index("--variants")
        assert post[idx + 1:idx + 4] == ["V1", "V2", "V3"]

    def test_none_value_dropped(self):
        # Operators may YAML-null an entry to "use the harness default".
        # Must not become "--key None".
        job = self._job(capital=None)
        argv = q.build_docker_run_argv(job, "rid", "img")
        post = argv[argv.index("tools/run_battery.py"):]
        assert "--capital" not in post

    def test_bind_mounts_match_launch_battery_sh(self):
        # The mount layout is fragile -- a typo here ends with the
        # battery unable to read its universe file (exact bug we hit
        # on first deploy). Pin the three mount strings.
        argv = q.build_docker_run_argv(self._job(), "rid", "img")
        v_pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        assert any("/logs:/app/logs" in v for v in v_pairs)
        assert any("/data:/app/data" in v for v in v_pairs)
        assert any("/tests/fixtures:/app/tests/fixtures:ro" in v for v in v_pairs)


# ──────────────────────── run_id derivation ────────────────────────
class TestRunIdDerivation:
    def test_fresh_job_gets_new_run_id(self):
        rid, resuming = q._run_id_for({"name": "alpha"}, prior_state=None)
        assert rid.startswith("battery_alpha_")
        assert resuming is False

    def test_prior_run_id_is_reused_for_resume(self):
        rid, resuming = q._run_id_for(
            {"name": "alpha"},
            prior_state={"run_id": "battery_alpha_T1", "status": "running"},
        )
        assert rid == "battery_alpha_T1"
        assert resuming is True

    def test_prior_completed_job_still_returns_its_run_id(self):
        # The orchestrator's main loop is what decides to skip completed
        # jobs; _run_id_for doesn't filter on status. This guarantees that
        # if completed jobs ever DO get reprocessed (e.g. operator clears
        # the status), we don't generate a brand-new run_id (which would
        # lose all the artefacts).
        rid, resuming = q._run_id_for(
            {"name": "alpha"},
            prior_state={"run_id": "battery_alpha_T1", "status": "completed"},
        )
        assert rid == "battery_alpha_T1"


# ──────────────────────── safety: broker cred guard ────────────────────────
class TestBrokerCredGuard:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for k in list(os.environ):
            if k.startswith(("ANGELONE_", "SMARTAPI_", "BROKER_", "KITE_")):
                monkeypatch.delenv(k, raising=False)
        yield

    def test_main_refuses_to_run_with_broker_creds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANGELONE_API_KEY", "leaked")
        rc = q.main([
            "--queue", str(tmp_path / "nope.yaml"),  # would fail later but we exit first
            "--dry-run",
        ])
        assert rc == 9

    def test_main_proceeds_with_no_broker_creds(self, tmp_path, monkeypatch):
        # Build a minimal valid queue + state and confirm --dry-run works.
        qp = tmp_path / "q.yaml"
        qp.write_text(yaml.safe_dump({
            "schema_version": 1,
            "jobs": [{"name": "smoke", "days": 1}],
        }), encoding="utf-8")
        sp = tmp_path / "s.json"
        # Patch shutil.which to claim docker exists (the function does
        # a binary-on-PATH check before processing).
        with patch.object(q.shutil, "which", return_value="/usr/bin/docker"):
            rc = q.main([
                "--queue", str(qp),
                "--state", str(sp),
                "--log-dir", str(tmp_path / "logs"),
                "--dry-run",
                "--no-wait-pre-existing",
            ])
        assert rc == 0


# ──────────────────────── orchestrator: skip-completed ────────────────────────
class TestSkipCompleted:
    def test_completed_jobs_are_not_re_run(self, tmp_path):
        # Two-job queue; first marked completed in state. process_queue
        # should skip it and only attempt the second. Since --dry-run is
        # on, neither actually runs docker.
        qp = tmp_path / "q.yaml"
        qp.write_text(yaml.safe_dump({
            "schema_version": 1,
            "jobs": [
                {"name": "done_already", "days": 1},
                {"name": "fresh", "days": 1},
            ],
        }), encoding="utf-8")
        sp = tmp_path / "s.json"
        sp.write_text(json.dumps({
            "schema_version": 1,
            "jobs": {
                "done_already": {
                    "status": "completed",
                    "exit_code": 0,
                    "finished_at": "2026-05-18T18:00:00Z",
                    "run_id": "battery_done_already_X",
                },
            },
        }), encoding="utf-8")

        # Capture stdout to verify the "SKIP" line surfaces.
        from io import StringIO
        buf = StringIO()
        with patch("sys.stdout", buf):
            n = q.process_queue(
                queue_path=qp, state_path=sp, log_dir=tmp_path / "lg",
                image="trading-agent:latest", dry_run=True,
                wait_pre_existing=False,
            )
        out = buf.getvalue()
        assert "SKIP 'done_already'" in out
        # Dry-run counts as 0 processed (nothing actually ran).
        assert n == 0
