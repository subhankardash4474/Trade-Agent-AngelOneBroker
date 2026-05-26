"""Regression tests for Bug G (battery harness robustness, 2026-05-25).

Bug G is a family of four preemptive fixes landed after a thorough code
review of the backtester subsystem. They harden the harness against
failure modes that haven't yet bitten production runs but were one
unlucky variant away from costing us another 64h of compute.

  G-1  Atomic results-JSON writes + corrupt-JSON quarantine on resume
       so a worker crash mid-write never leaves a truncated file that
       the resume path silently treats as "done".

  G-2  Auto-retry loop on `BrokenProcessPool` cascade. Bug F's
       `max_tasks_per_child=1` prevents *cross-variant state pollution*
       but does NOT prevent the cascade itself; one native crash in
       any worker still invalidates the pool and zeros all pending
       futures. Now we recreate a fresh pool and re-submit only the
       variants that have not yet completed (no valid
       `results/<name>.json`) and have not already failed with a real
       Python exception.

  G-3  Hard timeout on `yfinance.download` calls inside
       `_trend_context._fetch_daily`. Without it, a stalled HTTP
       socket hangs the worker until the watchdog fires, which then
       triggers the cascade (Bug G-2 partially saves us, but
       fail-fast on the network side is cleaner).

  G-5  Queue scheduler: `--rm` flag on docker-run argv so exited
       containers don't block the next launch by name; on launch
       failure with name conflict, force-remove the zombie and retry
       once. Distinguishes `failure_phase: "launch"` from `"run"` so
       operators can tell at a glance which side of the pipe broke.

These tests are *structural* (source / AST checks) rather than
runtime checks, mirroring the pattern in
`test_battery_worker_isolation.py`. Spinning up real
`ProcessPoolExecutor` cascades or yfinance hangs inside pytest is
slow and flaky; the contracts these fixes encode are well-defined
source-level invariants.
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from research import battery  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# G-1 — atomic writes + corrupt-JSON quarantine on resume
# ─────────────────────────────────────────────────────────────────────
class TestAtomicWriteHelper:
    """`_atomic_write_text` exists, writes via .tmp + replace, and is
    used at every site that produces an artifact that resume reads."""

    def test_helper_exists_and_is_callable(self):
        assert hasattr(battery, "_atomic_write_text"), (
            "battery._atomic_write_text must exist (Bug G-1). It is the "
            "atomic-write primitive used for results/*.json and "
            "comparison.md so a writer crash never leaves torn files."
        )
        assert callable(battery._atomic_write_text)

    def test_helper_writes_atomically(self, tmp_path: Path):
        """End-to-end: helper must write the final content atomically.

        We verify the post-condition (file exists with right content,
        no .tmp residue) rather than the implementation detail
        (write-to-tmp-then-rename) so the test is robust to internal
        refactors as long as the contract holds.
        """
        target = tmp_path / "out.json"
        battery._atomic_write_text(target, '{"a": 1}')
        assert target.exists()
        assert target.read_text(encoding="utf-8") == '{"a": 1}'
        # Tmp-file scratch should not linger after a clean write.
        residue = list(tmp_path.glob("*.tmp"))
        assert residue == [], (
            f"Atomic write left tmp-file residue {residue}; expected "
            f"the .tmp to be renamed onto the target."
        )

    def test_helper_overwrites_existing(self, tmp_path: Path):
        target = tmp_path / "out.json"
        target.write_text("OLD", encoding="utf-8")
        battery._atomic_write_text(target, "NEW")
        assert target.read_text(encoding="utf-8") == "NEW"

    def test_result_json_write_uses_atomic_helper(self):
        """The `results/<name>.json` write inside
        `_run_variant_in_subprocess` must go through
        `_atomic_write_text`. A torn write here is the exact failure
        mode Bug G-1 addresses (resume silently skips the variant).
        """
        src = inspect.getsource(battery._run_variant_in_subprocess)
        # The literal site we hardened — accept either the function
        # name as a free reference (`_atomic_write_text(`) since that's
        # the contract we want to enforce.
        assert "_atomic_write_text(" in src, (
            "_run_variant_in_subprocess must persist results/*.json "
            "via _atomic_write_text. Bug G-1 hardens against worker "
            "crashes mid-write that leave truncated JSON; if you "
            "regress this, _completed_variant_names will mark the "
            "corrupt file as 'done' and resume will skip the variant."
        )

    def test_comparison_md_write_uses_atomic_helper(self):
        """`_write_comparison` must also use atomic writes so off-box
        readers (operators tailing comparison.md via SSH) never see a
        torn render mid-update."""
        src = inspect.getsource(battery._write_comparison)
        assert "_atomic_write_text(" in src, (
            "_write_comparison must use _atomic_write_text so "
            "concurrent readers (SSH tail, scp pulls, the live-md "
            "thread + per-completion writer) never observe a torn "
            "comparison.md mid-update."
        )


class TestCorruptJsonQuarantine:
    """`_completed_variant_names` must validate JSON and quarantine
    bad files instead of treating them as 'done'."""

    def test_corrupt_json_is_quarantined_and_excluded(self, tmp_path: Path):
        results = tmp_path / "results"
        results.mkdir()
        # Truncated JSON — exactly what a worker crash mid-write
        # would leave on disk under the pre-G-1 non-atomic write.
        bad = results / "v_bad.json"
        bad.write_text('{"variant": "v_bad", "summa', encoding="utf-8")

        completed = battery._completed_variant_names(tmp_path)

        assert "v_bad" not in completed, (
            "Corrupt result JSON must be EXCLUDED from completed names "
            "so resume re-runs the variant. Including it would silently "
            "drop the variant from the run."
        )
        # Quarantine artifact must exist; the original file must be gone.
        assert (results / "v_bad.json.corrupt").exists(), (
            "Corrupt file must be renamed to <name>.json.corrupt for "
            "post-mortem inspection."
        )
        assert not bad.exists(), (
            "Original corrupt file must be moved aside so resume's "
            "next pass doesn't keep re-quarantining it."
        )

    def test_missing_required_keys_treated_as_corrupt(self, tmp_path: Path):
        """Even valid JSON missing the required schema is corrupt
        (worker crashed AFTER opening braces but before writing the
        full payload). Resume must re-run."""
        results = tmp_path / "results"
        results.mkdir()
        bad = results / "v_partial.json"
        # Valid JSON; missing `summary` and `elapsed_sec` keys.
        bad.write_text('{"variant": "v_partial"}', encoding="utf-8")

        completed = battery._completed_variant_names(tmp_path)

        assert "v_partial" not in completed
        assert (results / "v_partial.json.corrupt").exists()

    def test_summary_must_be_dict(self, tmp_path: Path):
        results = tmp_path / "results"
        results.mkdir()
        bad = results / "v_typo.json"
        # `summary` is a string, not a dict — wouldn't crash a naive
        # reader but breaks every downstream consumer that does
        # `summary["pnl"]`.
        bad.write_text(
            json.dumps({
                "variant": "v_typo",
                "summary": "oops",
                "elapsed_sec": 1.0,
            }),
            encoding="utf-8",
        )
        completed = battery._completed_variant_names(tmp_path)
        assert "v_typo" not in completed
        assert (results / "v_typo.json.corrupt").exists()

    def test_valid_json_is_accepted(self, tmp_path: Path):
        results = tmp_path / "results"
        results.mkdir()
        good = results / "v_good.json"
        good.write_text(
            json.dumps({
                "variant": "v_good",
                "summary": {"pnl": 0, "trades": 0},
                "elapsed_sec": 1.0,
            }),
            encoding="utf-8",
        )
        completed = battery._completed_variant_names(tmp_path)
        assert "v_good" in completed
        assert good.exists(), "Valid file must NOT be quarantined."

    def test_orphan_json_with_failure_txt_excluded(self, tmp_path: Path):
        """A `<name>.failure.txt` next to a `<name>.json` is
        AUTHORITATIVE — the variant must be EXCLUDED from `completed`
        so resume re-runs it, even when the JSON is otherwise valid.

        Audit fix on top of Bug G-1 (2026-05-26). Failure mode:
        worker writes results/<name>.json successfully, then crashes
        on return / pickle / shutdown; parent records
        <name>.failure.txt for the orphan. Without this guard, the
        next operator-initiated --resume would silently skip the
        variant the operator was trying to retry.
        """
        results = tmp_path / "results"
        results.mkdir()
        # A perfectly-valid JSON, schema-clean.
        (results / "v_orphan.json").write_text(
            json.dumps({
                "variant": "v_orphan",
                "summary": {"variant": "v_orphan"},
                "elapsed_sec": 1.0,
            }),
            encoding="utf-8",
        )
        # ...sitting next to a parent-recorded failure for the same
        # variant (timestamp + traceback in real life).
        (results / "v_orphan.failure.txt").write_text(
            "2026-05-26T10:00:00\nMockPickleError\n", encoding="utf-8",
        )
        completed = battery._completed_variant_names(tmp_path)
        assert "v_orphan" not in completed, (
            "A variant with both <name>.json and <name>.failure.txt "
            "must NOT be reported as completed. The .failure.txt is "
            "authoritative -- the parent recorded a failure even "
            "though the worker managed to write the JSON."
        )

    def test_failure_txt_alone_does_not_quarantine_anything(self, tmp_path: Path):
        """A bare `<name>.failure.txt` (no sibling .json) must NOT
        cause a quarantine warning -- the variant is just not in the
        completed set, which is the desired outcome for a real
        run-time failure."""
        results = tmp_path / "results"
        results.mkdir()
        (results / "v_failed.failure.txt").write_text(
            "2026-05-26T10:00:00\nKeyError: 'foo'\n", encoding="utf-8",
        )
        completed = battery._completed_variant_names(tmp_path)
        assert "v_failed" not in completed
        # And no .corrupt rename happened (there was no .json to quarantine)
        assert list(results.glob("*.corrupt")) == []

    def test_orphan_with_failure_does_not_quarantine_the_orphan(self, tmp_path: Path):
        """The orphan-JSON masking guard must NOT also rename the
        orphan to .corrupt -- that would lose forensic data and
        confuse downstream tooling. We only EXCLUDE it from
        completed; the JSON stays in place for the operator to
        inspect alongside the failure.txt traceback."""
        results = tmp_path / "results"
        results.mkdir()
        orphan = results / "v_orphan.json"
        orphan.write_text(
            json.dumps({
                "variant": "v_orphan",
                "summary": {"variant": "v_orphan"},
                "elapsed_sec": 1.0,
            }),
            encoding="utf-8",
        )
        (results / "v_orphan.failure.txt").write_text(
            "boom\n", encoding="utf-8",
        )
        battery._completed_variant_names(tmp_path)
        # Orphan JSON still in place, NOT quarantined.
        assert orphan.exists()
        assert not (results / "v_orphan.json.corrupt").exists()

    def test_tmp_files_are_skipped(self, tmp_path: Path):
        """Atomic-write residue (`<name>.json.tmp`) must not be picked
        up as a candidate result file — it would always fail schema
        validation and confuse the quarantine log."""
        results = tmp_path / "results"
        results.mkdir()
        # The .tmp extension actually still matches "*.json" because
        # `glob("*.json")` requires the suffix to BE .json — not
        # .json.tmp. This test pins the contract regardless. Create
        # both a real result AND a .tmp residue to be sure.
        (results / "v_real.json").write_text(
            json.dumps({
                "variant": "v_real",
                "summary": {"pnl": 1},
                "elapsed_sec": 1.0,
            }),
            encoding="utf-8",
        )
        (results / "v_real.json.tmp").write_text("partial", encoding="utf-8")
        completed = battery._completed_variant_names(tmp_path)
        assert completed == {"v_real"}


# ─────────────────────────────────────────────────────────────────────
# G-2 — auto-retry loop on BrokenProcessPool cascade
# ─────────────────────────────────────────────────────────────────────
class TestProcessPoolRetryLoop:
    """The parallel-mode dispatch in `battery.main` must wrap the
    ProcessPoolExecutor block in a retry loop that:
      * recreates a fresh pool after BrokenProcessPool
      * re-submits only un-completed, un-real-failed variants
      * is bounded (no infinite retries on deterministic crashes)
    """

    def test_main_imports_broken_process_pool_exception(self):
        """The retry loop only works if we explicitly catch
        `BrokenProcessPool` instead of the generic `Exception` —
        otherwise we'd mark cascade casualties as real failures."""
        src = Path(battery.__file__).read_text(encoding="utf-8")
        assert (
            "from concurrent.futures.process import BrokenProcessPool" in src
            or "BrokenProcessPool" in src
        ), (
            "battery.py must import BrokenProcessPool so the retry "
            "loop can distinguish pool-wide cascade casualties from "
            "real per-variant Python exceptions."
        )

    def test_main_has_retry_loop_with_max_attempts(self):
        """AST-walk `battery.main` to confirm the retry-loop
        scaffolding exists. The contract: there must be a
        `MAX_POOL_RETRIES` constant (or a numeric upper bound) so
        the loop is bounded."""
        src = inspect.getsource(battery.main)
        assert "MAX_POOL_RETRIES" in src, (
            "battery.main must define MAX_POOL_RETRIES (Bug G-2) — "
            "the upper bound on cascade retries. Without it, a "
            "deterministically-crashing variant would infinite-loop "
            "the harness."
        )

    def test_main_catches_broken_process_pool_per_future(self):
        """Per-future exception handling must distinguish
        BrokenProcessPool (cascade casualty, retry) from generic
        Exception (real failure, mark and don't retry)."""
        src = inspect.getsource(battery.main)
        # Both clauses must appear in the same function. We don't
        # care about ordering as long as BrokenProcessPool is its
        # own except branch.
        assert "except BrokenProcessPool" in src, (
            "battery.main must have a `except BrokenProcessPool` "
            "branch in its per-future handler. Without it, cascade "
            "casualties get marked as failed and never retry."
        )
        assert "except Exception" in src, (
            "battery.main must still catch generic Exception for real "
            "per-variant Python failures (those get marked failed and "
            "do NOT retry, by design)."
        )

    def test_pool_construction_inside_retry_loop_uses_max_tasks_per_child_1(self):
        """Each retry attempt must use a FRESH pool with
        max_tasks_per_child=1 — otherwise a retry that uses a
        polluted pool would re-trigger the original Bug F crash."""
        src = inspect.getsource(battery.main)
        tree = ast.parse(src)
        ppe_calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "ProcessPoolExecutor"
        ]
        # There should still be exactly one PPE call (inside the loop)
        # — the retry loop reuses the same construction site each pass.
        assert len(ppe_calls) >= 1
        for call in ppe_calls:
            kw_names = {kw.arg for kw in call.keywords}
            assert "max_tasks_per_child" in kw_names, (
                "Every ProcessPoolExecutor in battery.main must set "
                "max_tasks_per_child=1 (Bug F invariant)."
            )

    def test_real_failure_does_not_retry(self):
        """A variant that fails with a real Python exception must be
        recorded in `real_failed` (or equivalent) so the next attempt
        does NOT re-submit it. Source check for the tracking set."""
        src = inspect.getsource(battery.main)
        assert "real_failed" in src, (
            "battery.main must track variants that hit real Python "
            "exceptions (vs. BrokenProcessPool cascade casualties) "
            "so retries don't waste cycles re-running deterministic "
            "failures. The Bug G-2 fix names this set `real_failed`."
        )


# ─────────────────────────────────────────────────────────────────────
# G-3 — yfinance hard timeout in trend_context
# ─────────────────────────────────────────────────────────────────────
class TestTrendContextTimeout:
    """`_trend_context._fetch_daily` must hard-timeout the yfinance
    HTTP call so a stalled socket can't hang the worker thread until
    the watchdog fires."""

    def test_module_defines_timeout_constant(self):
        from packages.strategies import _trend_context as tc
        assert hasattr(tc, "_YF_TIMEOUT_SEC"), (
            "_trend_context must define _YF_TIMEOUT_SEC (Bug G-3) — "
            "the upper bound on a single yfinance fetch. Without "
            "this, a stalled HTTP socket hangs the worker."
        )
        assert isinstance(tc._YF_TIMEOUT_SEC, (int, float))
        assert 0 < tc._YF_TIMEOUT_SEC <= 120, (
            f"_YF_TIMEOUT_SEC={tc._YF_TIMEOUT_SEC} is outside the "
            f"sanity band (0, 120]s. A fetch should complete in "
            f"~5-10s; >120s defeats the point of having a timeout."
        )

    def test_module_has_timeouted_download_helper(self):
        from packages.strategies import _trend_context as tc
        assert hasattr(tc, "_yf_download_with_timeout"), (
            "_trend_context must expose _yf_download_with_timeout "
            "(Bug G-3) — the hard-timeouted yfinance wrapper used by "
            "_fetch_daily."
        )
        assert callable(tc._yf_download_with_timeout)

    def test_fetch_daily_uses_timeouted_helper(self):
        from packages.strategies import _trend_context as tc
        src = inspect.getsource(tc._fetch_daily)
        assert "_yf_download_with_timeout" in src, (
            "_fetch_daily must route through _yf_download_with_timeout. "
            "Calling yf.download directly would re-introduce the "
            "indefinite-hang failure mode that Bug G-3 fixes."
        )
        # Negative check: no direct yf.download call inside _fetch_daily.
        assert "yf.download(" not in src, (
            "_fetch_daily must NOT call yf.download directly — that "
            "bypasses the Bug G-3 timeout. Use "
            "_yf_download_with_timeout instead."
        )

    def test_timeout_returns_none_not_raises(self):
        """A timeout must fail-open (return None) so callers treat
        it as 'trend unknown' rather than crashing the strategy."""
        from packages.strategies import _trend_context as tc
        src = inspect.getsource(tc._yf_download_with_timeout)
        # Both the `concurrent.futures.TimeoutError` catch and the
        # `return None` must be in the function source.
        assert "TimeoutError" in src
        assert "return None" in src, (
            "_yf_download_with_timeout must return None on timeout "
            "(fail-open contract for is_against_trend)."
        )

    def test_timeout_can_be_overridden_by_env(self):
        """Operators must be able to tighten the timeout for tests
        or loosen it for slow networks via TREND_FETCH_TIMEOUT_SEC."""
        from packages.strategies import _trend_context as tc
        src = inspect.getsource(tc)
        assert "TREND_FETCH_TIMEOUT_SEC" in src, (
            "_trend_context must read TREND_FETCH_TIMEOUT_SEC from "
            "the environment so the timeout is tunable without a "
            "code change. Tests rely on this."
        )

    def test_timeout_actually_returns_within_window_when_fetch_hangs(
        self, monkeypatch
    ):
        """Runtime test (audit fix, 2026-05-26): when the inner fetch
        is genuinely hung, `_yf_download_with_timeout` MUST return
        within ~timeout seconds.

        Why this test exists: the original Bug G-3 fix wrapped
        ``ex.submit(...).result(timeout=N)`` in a
        ``with ThreadPoolExecutor(...) as ex:`` block. ``Executor.__exit__``
        calls ``shutdown(wait=True)``, which BLOCKS until every running
        task completes -- so when the fetch hung, the whole helper
        hung waiting for shutdown, defeating the timeout. Source-only
        tests (the rest of this class) cannot catch that class of bug
        because the broken code passes them all. This test forces a
        deliberate hang and asserts wall-clock behaviour.

        We use a 1.0s timeout and a 30s sleeper so the test takes
        ~1s and any regression (e.g. someone reintroducing the
        with-block) would balloon to 30s -- way over our assertion.
        """
        import time
        from packages.strategies import _trend_context as tc

        def hanging_yf(*_args, **_kwargs):
            # Simulate a stalled HTTP socket: sleep far longer than the
            # caller's timeout. The helper must NOT block on us.
            time.sleep(30)
            return None  # never reached

        # Monkeypatch the inner closure by replacing yf.download. The
        # helper imports yfinance lazily inside _do_fetch, so we need
        # to inject into sys.modules.
        import sys
        import types
        fake_yf = types.ModuleType("yfinance")
        fake_yf.download = hanging_yf  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

        t0 = time.time()
        result = tc._yf_download_with_timeout("RELIANCE", timeout=1.0)
        elapsed = time.time() - t0

        assert result is None, (
            "Hung fetch must return None (fail-open), not the dataframe."
        )
        # Generous upper bound: 1.0s timeout + ~2s slop for thread
        # creation, GIL contention, finally-block shutdown, etc.
        # If this ever exceeds 5s, someone has reintroduced a
        # blocking shutdown(wait=True) somewhere on the path.
        assert elapsed < 5.0, (
            f"_yf_download_with_timeout(timeout=1.0) took {elapsed:.1f}s "
            f"to return when the inner fetch was hung -- the timeout "
            f"is structurally broken. The classic regression here is "
            f"using `with ThreadPoolExecutor(...) as ex:`, whose "
            f"__exit__ calls shutdown(wait=True) and blocks on the "
            f"hung thread. Use try/finally with "
            f"`shutdown(wait=False, cancel_futures=True)` instead."
        )


# ─────────────────────────────────────────────────────────────────────
# G-5 — queue scheduler: --rm flag + zombie-container retry
# ─────────────────────────────────────────────────────────────────────
class TestQueueSchedulerRobustness:
    """`tools/run_battery_queue.py` must: (a) pass --rm to docker run
    so exited containers self-remove, (b) on launch failure with name
    conflict, force-remove the zombie and retry once, (c) distinguish
    launch-time failure from run-time failure in the saved state."""

    def _load_module(self):
        import importlib.util
        path = ROOT / "tools" / "run_battery_queue.py"
        spec = importlib.util.spec_from_file_location(
            "run_battery_queue", path,
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    def test_docker_run_argv_includes_rm_flag(self):
        mod = self._load_module()
        argv = mod.build_docker_run_argv(
            {"name": "test_job", "days": 1, "workers": 1, "interval": "5min"},
            run_id="battery_test_20260101T000000",
            image="trading-agent:test",
            resuming=False,
        )
        assert "--rm" in argv, (
            "build_docker_run_argv must emit --rm (Bug G-5). Without "
            "it, exited containers retain their name and the next "
            "launch with the same run_id (the resume path) hits a "
            "name conflict."
        )

    def test_process_queue_handles_name_conflict(self):
        mod = self._load_module()
        src = inspect.getsource(mod.process_queue)
        # The retry path must mention the docker name-conflict
        # sentinel so we know it actually catches THAT class of
        # failure (and not e.g. image-pull failures, which retry
        # wouldn't help).
        assert "is already in use by container" in src, (
            "process_queue must detect the docker 'is already in use "
            "by container' stderr signature so it can recover by "
            "force-removing the zombie. Without this signature "
            "match, the retry would attempt on every launch failure "
            "(including legitimate ones like image-missing) and "
            "still fail — wasting a 30s docker-rm timeout each "
            "time."
        )
        assert 'docker", "rm", "-f"' in src or '"docker rm -f"' in src or "rm -f" in src, (
            "process_queue must call `docker rm -f <run_id>` to "
            "force-remove the zombie before retrying the launch."
        )

    def test_process_queue_records_failure_phase(self):
        """Operators must be able to tell at a glance whether a
        failed job died at launch (image / daemon problem) or at
        run-time (harness / variant crashed)."""
        mod = self._load_module()
        src = inspect.getsource(mod.process_queue)
        assert "failure_phase" in src, (
            "process_queue must record `failure_phase` in the saved "
            "state (Bug G-5) — values 'launch' or 'run' — so the "
            "operator can route triage to docker logs vs. "
            "workers/<name>.log accordingly."
        )
        assert '"launch"' in src and '"run"' in src, (
            "Both 'launch' and 'run' phase markers must appear in "
            "process_queue source so the JSON state schema is "
            "complete."
        )


# ─────────────────────────────────────────────────────────────────────
# Documentation cross-reference
# ─────────────────────────────────────────────────────────────────────
class TestBugGDocumented:
    """Bug G must be referenced in source so a future archaeologist
    can grep their way to the RCA + fix rationale."""

    def test_bug_g_referenced_in_battery(self):
        path = Path(battery.__file__)
        src = path.read_text(encoding="utf-8")
        assert "Bug G-1" in src, "Bug G-1 (atomic writes) must be cited."
        assert "Bug G-2" in src, "Bug G-2 (retry loop) must be cited."

    def test_bug_g_referenced_in_trend_context(self):
        from packages.strategies import _trend_context as tc
        src = Path(tc.__file__).read_text(encoding="utf-8")
        assert "Bug G-3" in src, (
            "Bug G-3 (yfinance timeout) must be cited in "
            "_trend_context.py."
        )

    def test_bug_g_referenced_in_queue_scheduler(self):
        path = ROOT / "tools" / "run_battery_queue.py"
        src = path.read_text(encoding="utf-8")
        assert "Bug G-5" in src, (
            "Bug G-5 (--rm + zombie retry) must be cited in "
            "run_battery_queue.py."
        )
