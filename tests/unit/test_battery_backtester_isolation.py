"""Tests for the backtester isolation startup assertion.

The battery harness now refuses to start when `BACKTESTER_MODE=1` is set
*and* any broker credential env var is present. This guard lives in
`research.battery._assert_backtester_isolation()` and runs as the very
first thing inside `main()` -- before argparse, before any data fetch.

These tests pin the contract so a refactor can't silently disable the
check (which would let the backtester host accidentally touch a live
broker socket).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Bootstrap sys.path so `research` resolves as a top-level package, same
# pattern as tools/run_battery.py.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from research import battery  # noqa: E402


@pytest.fixture
def clean_env(monkeypatch):
    """Strip any pre-existing broker-cred env vars before each test.

    The dev's host may have ANGELONE_API_KEY etc. exported globally; we
    want each test to start from a known-clean slate.
    """
    for key in list(os.environ):
        if any(
            key.startswith(p)
            for p in battery._BROKER_CRED_ENV_PREFIXES
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("BACKTESTER_MODE", raising=False)
    yield


class TestBacktesterIsolation:
    def test_noop_when_backtester_mode_unset(self, clean_env, monkeypatch):
        # Live trader VM path: BACKTESTER_MODE absent. Even if broker
        # creds are in env (which is normal on the trader), this guard
        # must not fire.
        monkeypatch.setenv("ANGELONE_API_KEY", "live-key-xyz")
        # Should return cleanly without raising.
        battery._assert_backtester_isolation()

    def test_noop_when_backtester_mode_unset_no_creds(self, clean_env):
        # Sanity: no flag, no creds, no fire.
        battery._assert_backtester_isolation()

    def test_clean_run_on_backtester_mode_with_no_creds(
        self, clean_env, monkeypatch,
    ):
        # The intended happy path on the backtester VM: flag is on,
        # zero broker creds anywhere. Should pass silently.
        monkeypatch.setenv("BACKTESTER_MODE", "1")
        battery._assert_backtester_isolation()

    @pytest.mark.parametrize(
        "var_name",
        [
            "ANGELONE_API_KEY",
            "ANGELONE_CLIENT_ID",
            "SMARTAPI_TOTP_SECRET",
            "BROKER_PASSWORD",
            "KITE_API_SECRET",
        ],
    )
    def test_fires_on_any_broker_cred_prefix(
        self, clean_env, monkeypatch, var_name,
    ):
        # If BACKTESTER_MODE=1 AND any prefix-matching env var is present,
        # we must SystemExit with code 9.
        monkeypatch.setenv("BACKTESTER_MODE", "1")
        monkeypatch.setenv(var_name, "leaked-value")
        with pytest.raises(SystemExit) as exc_info:
            battery._assert_backtester_isolation()
        assert exc_info.value.code == 9

    def test_error_message_mentions_leaked_var(
        self, clean_env, monkeypatch, capsys,
    ):
        # The fatal message must include the offending variable name so
        # the operator can see WHICH cred leaked, not just that something
        # leaked.
        monkeypatch.setenv("BACKTESTER_MODE", "1")
        monkeypatch.setenv("ANGELONE_API_KEY", "leaked-value")
        with pytest.raises(SystemExit):
            battery._assert_backtester_isolation()
        captured = capsys.readouterr()
        assert "ANGELONE_API_KEY" in captured.err

    @pytest.mark.parametrize(
        "truthy_value",
        ["1", "true", "yes", "on", "TRUE", "Yes", "ON"],
    )
    def test_accepts_common_truthy_flag_values(
        self, clean_env, monkeypatch, truthy_value,
    ):
        # The systemd unit, docker-compose YAML, and shell scripts will
        # spell the flag differently. Accept the standard truthy spellings.
        monkeypatch.setenv("BACKTESTER_MODE", truthy_value)
        monkeypatch.setenv("ANGELONE_API_KEY", "x")
        with pytest.raises(SystemExit) as exc_info:
            battery._assert_backtester_isolation()
        assert exc_info.value.code == 9

    @pytest.mark.parametrize(
        "falsy_value",
        ["0", "false", "no", "off", "", "   ", "anything-else"],
    )
    def test_disabled_for_falsy_flag_values(
        self, clean_env, monkeypatch, falsy_value,
    ):
        # Defensive: a malformed value shouldn't accidentally arm the
        # guard. If the operator types BACKTESTER_MODE=anything-else
        # they get the unguarded path -- no SystemExit.
        monkeypatch.setenv("BACKTESTER_MODE", falsy_value)
        monkeypatch.setenv("ANGELONE_API_KEY", "x")
        battery._assert_backtester_isolation()

    def test_main_calls_assertion_first(self, clean_env, monkeypatch):
        # Structural regression guard: the assertion must be invoked
        # before argparse, so an operator with leaked creds never even
        # gets a chance to pass --help, --resume, etc.
        import inspect
        src = inspect.getsource(battery.main)
        # We expect the very first executable line of main() to call
        # _assert_backtester_isolation. (Trivial whitespace tolerated.)
        lines = [
            ln.strip()
            for ln in src.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        # First line is the def, second line should be the assertion call.
        assert len(lines) >= 2
        assert lines[0].startswith("def main")
        assert "_assert_backtester_isolation" in lines[1], (
            f"Expected first body line of main() to invoke the isolation "
            f"assertion, got: {lines[1]!r}. If this guard moved, any "
            f"refactor that puts I/O or argparse first can leak broker "
            f"creds to the backtester host."
        )
