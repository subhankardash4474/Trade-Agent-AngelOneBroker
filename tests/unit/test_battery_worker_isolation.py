"""Regression tests for Bug F (worker isolation against state-pollution
cascade-fail in the battery harness).

Background — 2026-05-25 nifty50_60d incident
--------------------------------------------
Run `battery_nifty50_60d_20260525T105637` ran V1+V2 successfully (~3.18h
each in fresh `ProcessPoolExecutor` worker subprocesses), then V3 died at
~30 min after starting in a *re-used* worker. With no Python traceback
written to V3's worker log, no kernel OOM in journalctl, and
`OOMKilled=false` on the docker container, the only signal the parent
process saw was `concurrent.futures.process.BrokenProcessPool`. That
exception cascaded to ALL 17 pending futures (V4-V19) — only V3 had
actually crashed, but the harness reported "17 variants failed" because
`ProcessPoolExecutor` invalidates the entire pool on a single uncaught
worker death.

Two fixes landed in `packages/research/battery.py`:

1. **Worker isolation** — `ProcessPoolExecutor(max_tasks_per_child=1)`
   forces a brand-new subprocess for every variant. Eliminates
   cross-variant native-code state pollution (the leading hypothesis
   for V3's silent death — survived state includes
   `_trend_context._cache`, yfinance connection pools, xgboost native
   handles, loguru sinks, numpy/pandas internal caches). V1+V2 ran in
   *fresh* workers and passed; V3+V4 ran in *re-used* workers and died
   at the same elapsed time. With `max_tasks_per_child=1`, V3 is now a
   first-task-of-fresh-worker run, identical in process state to how
   V1 and V2 ran successfully.

2. **Fault diagnostics** — `faulthandler.enable(file=<workers/<name>.fault.log>)`
   is wired up at the very top of `_run_variant_in_subprocess`. Any
   future native-code crash (segfault / abort / bus error) will dump a
   Python traceback to the per-variant fault file before the process
   dies — converting "BrokenProcessPool with no info" into a real
   diagnosis.

These tests pin both fixes against accidental regression. They are
*structural* (source-text / AST / signature checks) rather than runtime
checks because:
- Spinning up real `ProcessPoolExecutor` subprocesses inside pytest is
  slow, flaky on Windows (spawn-pickling, sys.path), and would require
  a full battery scaffold.
- The fixes are one-line, well-defined invocations whose presence in
  source is exactly the contract we want to enforce.
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from research import battery  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# Fix #1 — max_tasks_per_child=1 on the ProcessPoolExecutor
# ─────────────────────────────────────────────────────────────────────
class TestProcessPoolMaxTasksPerChild:
    """The orchestrator must construct ProcessPoolExecutor with
    max_tasks_per_child=1 so that no two variants ever share a worker
    subprocess. Anything else would re-introduce the V3 cascade-fail
    failure mode.
    """

    def test_main_constructs_pool_with_max_tasks_per_child_eq_1(self):
        """AST-walk `battery.main` and find the ProcessPoolExecutor
        construction. Assert it has the kwarg `max_tasks_per_child=1`.
        """
        src = inspect.getsource(battery.main)
        tree = ast.parse(src)

        ppe_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ProcessPoolExecutor"
        ]
        assert len(ppe_calls) == 1, (
            f"Expected exactly 1 ProcessPoolExecutor() call in battery.main, "
            f"found {len(ppe_calls)}. If this changed intentionally, update "
            f"the test — but make sure max_tasks_per_child=1 is preserved."
        )

        call = ppe_calls[0]
        kw_names = {kw.arg for kw in call.keywords}
        assert "max_tasks_per_child" in kw_names, (
            "ProcessPoolExecutor in battery.main is missing max_tasks_per_child. "
            "This kwarg is the Bug F fix — it forces a fresh subprocess per "
            "variant, eliminating cross-variant native-code state pollution. "
            "Without it, the V3-V19 mass cascade-fail can recur."
        )

        # Pull out the literal value of max_tasks_per_child=…
        max_tasks_kw = next(
            kw for kw in call.keywords if kw.arg == "max_tasks_per_child"
        )
        assert isinstance(max_tasks_kw.value, ast.Constant), (
            "max_tasks_per_child must be a literal int constant (not an "
            "expression / variable) so the contract is unambiguous."
        )
        assert max_tasks_kw.value.value == 1, (
            f"max_tasks_per_child must be 1 (one variant per subprocess). "
            f"Found {max_tasks_kw.value.value!r}. If you need to bump this for "
            f"a perf reason, also restore the per-variant state cleanup "
            f"(trend_context.clear_cache, gc.collect, loguru.remove) inside "
            f"_run_variant_in_subprocess."
        )

    def test_main_passes_max_workers_alongside_max_tasks_per_child(self):
        """Defensive check: max_workers must still be wired through from
        args.workers. Otherwise the pool defaults to os.cpu_count() and a
        battery launched with --workers=1 silently runs N parallel."""
        src = inspect.getsource(battery.main)
        tree = ast.parse(src)
        ppe_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ProcessPoolExecutor"
        ]
        call = ppe_calls[0]
        kw_names = {kw.arg for kw in call.keywords}
        assert "max_workers" in kw_names, (
            "ProcessPoolExecutor must explicitly receive max_workers; "
            "relying on the default os.cpu_count() breaks --workers=N."
        )


# ─────────────────────────────────────────────────────────────────────
# Fix #2 — faulthandler enabled in the worker, writing to per-variant
# fault.log
# ─────────────────────────────────────────────────────────────────────
class TestWorkerFaulthandler:
    """`_run_variant_in_subprocess` must enable `faulthandler` writing
    to a `<workers_dir>/<name>.fault.log` file BEFORE any heavy work.
    Without this, a native-code crash gives the parent only a generic
    BrokenProcessPool with zero info, exactly what made Bug F take hours
    to root-cause."""

    def test_worker_source_imports_and_enables_faulthandler(self):
        """Source-level check: faulthandler is imported and enabled
        inside `_run_variant_in_subprocess`."""
        src = inspect.getsource(battery._run_variant_in_subprocess)

        assert "import faulthandler" in src, (
            "_run_variant_in_subprocess must import faulthandler so a "
            "segfault / abort can dump a Python traceback before the "
            "worker dies."
        )
        # The actual enable call — accept either the aliased form
        # `_fh.enable(...)` or `faulthandler.enable(...)`.
        assert (".enable(" in src), (
            "_run_variant_in_subprocess must call faulthandler.enable() "
            "so signal handlers are installed for SIGSEGV/SIGABRT/SIGFPE/"
            "SIGBUS/SIGILL."
        )

    def test_worker_writes_fault_log_to_per_variant_path(self):
        """The fault log must be PER VARIANT (so multiple workers don't
        race on the same file). Source check: filename derived from
        variant `name` argument and lives under `workers_dir`."""
        src = inspect.getsource(battery._run_variant_in_subprocess)
        # Sentinel: the f-string template that produces the fault path.
        # We look for any of the common spellings that would route the
        # output under workers_dir keyed by variant name.
        sentinel_a = '"{name}.fault.log"'
        sentinel_b = "'{name}.fault.log'"
        sentinel_c = '"%s.fault.log"'
        assert (
            sentinel_a in src
            or sentinel_b in src
            or sentinel_c in src
        ), (
            "Per-variant fault log file must be named "
            "'<variant>.fault.log' under the run's workers/ directory. "
            "Sharing a single fault log across variants would race on "
            "concurrent worker death and corrupt the diagnostic trail."
        )

    def test_worker_swallows_faulthandler_init_failure(self):
        """Best-effort contract: if faulthandler init fails for any
        reason (rare — would be a permission issue on the workers dir
        or a Python build without faulthandler), the worker MUST still
        proceed to do useful work. Otherwise we'd have introduced a new
        single-point-of-failure where the diagnostic tooling crashes
        the run it was meant to instrument."""
        src = inspect.getsource(battery._run_variant_in_subprocess)
        # Find the faulthandler block and assert it's wrapped in a
        # try/except that swallows.
        # Cheap structural proxy: there must be `except Exception:` or
        # `except BaseException:` followed by `pass` or comment within
        # the first 60 lines of the function (where the init lives).
        head = "\n".join(src.splitlines()[:60])
        assert (
            "except Exception" in head and "pass" in head
        ), (
            "faulthandler init must be best-effort (try/except wrapping "
            "the import + enable call). Otherwise a faulthandler open "
            "failure would kill the very run it's meant to diagnose."
        )


# ─────────────────────────────────────────────────────────────────────
# Smoke test: doc commentary references Bug F so a future archaeologist
# can grep their way to context.
# ─────────────────────────────────────────────────────────────────────
class TestDocumentation:
    def test_bug_f_referenced_in_battery_module(self):
        """The fix must be self-documenting: `Bug F` must appear in
        `battery.py` source so a future grepper can trace the change."""
        path = Path(battery.__file__)
        src = path.read_text(encoding="utf-8")
        assert "Bug F" in src, (
            "Bug F (worker isolation / cascade-fail) fix must reference "
            "its bug ID in code comments so the change is traceable."
        )
