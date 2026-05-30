"""Structural regression test for the supervisor's clean-exit behaviour.

Per the 2026-05-30 brutal review (Session 2 §3, Finding 6 follow-on)
``tools/run_daemon_resilient.ps1`` USED TO restart the daemon
unconditionally, including after a clean post-close shutdown
(intraday cutoff at 15:15 IST -> daemon returns 0). On 2026-05-30
the local laptop logged 21 restart cycles by 10:10 IST -- a
Saturday with the broker WebSocket closed -- generating dozens of
failed reconnect attempts. The fix exits the supervisor when
exit_code=0 unless the legacy behaviour is explicitly opted into
via ``SUPERVISOR_RESTART_ON_CLEAN_EXIT=1``.

This is a PowerShell script so we can't behaviourally test it from
pytest without launching the supervisor (which would actually run
the daemon -- destructive in CI). Instead we structurally pin:

  1. The clean-exit branch exists (``$exitCode -eq 0`` check).
  2. The CRITICAL log marker is present (``[SUPERVISOR-CLEAN-EXIT]``).
  3. The opt-out env var is documented (``SUPERVISOR_RESTART_ON_CLEAN_EXIT``).
  4. The legacy always-restart loop is no longer reachable on a
     0-exit path.

If anyone refactors the supervisor and accidentally drops the
clean-exit guard, these tests fail loudly.

Cross-references:
  * `docs/reviews/brutal_review_2026-05-30.md` Session 2 §3.
  * `tools/run_daemon_resilient.ps1`.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = ROOT / "tools" / "run_daemon_resilient.ps1"


def _src() -> str:
    return SUPERVISOR.read_text(encoding="utf-8")


def test_supervisor_script_exists():
    assert SUPERVISOR.is_file(), (
        f"{SUPERVISOR} not found. Did the supervisor move? Update the "
        f"path in this test."
    )


def test_supervisor_clean_exit_branch_present():
    """The fix's core: exit_code=0 -> exit supervisor. Without this branch
    the post-close flap loop (Finding 6) re-opens silently."""
    src = _src()
    # The marker we emit on clean exit. Both halves must be present:
    #   * the conditional check on exit_code 0
    #   * the supervisor-clean-exit log line
    assert re.search(r"\$exitCode\s*-eq\s*0", src), (
        "Supervisor must check exit_code=0 specifically. The brutal-review "
        "fix from 2026-05-30 hinges on distinguishing intentional clean "
        "shutdown from a crash."
    )
    assert "[SUPERVISOR-CLEAN-EXIT]" in src, (
        "Supervisor must emit a [SUPERVISOR-CLEAN-EXIT] log when it stops "
        "relaunching after a clean exit. Without this marker the operator "
        "can't grep for the new behaviour in daemon_supervisor.log."
    )


def test_supervisor_opt_out_env_var_documented():
    """The opt-out (``SUPERVISOR_RESTART_ON_CLEAN_EXIT=1``) must be:
      * Read from the environment.
      * Mentioned in the log line so an operator restoring legacy
        behaviour discovers it from the supervisor's own output.
    """
    src = _src()
    assert "SUPERVISOR_RESTART_ON_CLEAN_EXIT" in src, (
        "The opt-out env var name must appear in the supervisor source so "
        "an operator who needs the legacy always-restart behaviour can "
        "find the knob."
    )
    # The env var must be CONSULTED (Get/Read), not just mentioned.
    assert re.search(
        r"GetEnvironmentVariable\(\s*\"SUPERVISOR_RESTART_ON_CLEAN_EXIT\"\s*\)",
        src,
    ), (
        "The opt-out env var is documented but never consulted. The "
        "supervisor must actually read it via GetEnvironmentVariable."
    )


def test_supervisor_clean_exit_returns_before_relaunch():
    """Structural guard: the clean-exit branch must ``exit 0`` BEFORE the
    Start-Sleep + relaunch path. If the order ever flips, the fix
    becomes a no-op."""
    src = _src()
    # Anchor on the clean-exit log marker.
    clean_idx = src.find("[SUPERVISOR-CLEAN-EXIT]")
    assert clean_idx > 0, (
        "Clean-exit branch missing -- prerequisite of this test failed."
    )
    # Within ~500 chars after the log line, an `exit 0` must appear.
    window = src[clean_idx:clean_idx + 500]
    assert re.search(r"exit\s+0", window), (
        "Clean-exit branch logs the marker but does not exit. The "
        "supervisor will fall through to the cooldown-and-relaunch loop "
        "and the brutal-review Finding 6 fix is a no-op."
    )
