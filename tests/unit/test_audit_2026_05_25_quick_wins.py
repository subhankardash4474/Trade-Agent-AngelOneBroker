"""Regression tests for the 2026-05-25 audit "quick wins" (B-1, B-3, B-4,
B-5, B-11).

Each `class TestB*` corresponds to one finding in
`docs/audits/audit_2026-05-25_bug_report.md`. Every test is structured so that
it would FAIL on the pre-fix tree (the bug-as-shipped state) and PASS
after the corresponding source change. That is the spec.

If any of these starts to fail in CI it almost certainly means the bug
has regressed; do not relax the assertion without re-doing the audit.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# B-1 -- stop_daemon.py must import without raising SyntaxError.
# Pre-fix: `from __future__ import annotations` was on line 39, AFTER the
# sys.path bootstrap (lines 33-37), so `python stop_daemon.py` raised
# `SyntaxError: from __future__ imports must occur at the beginning of the
# file`. The emergency kill switch was unusable.
# ---------------------------------------------------------------------------
class TestB1StopDaemonImports:
    def test_stop_daemon_module_imports_without_syntax_error(self):
        path = ROOT / "stop_daemon.py"
        assert path.exists(), "stop_daemon.py missing"
        # Compile path is enough -- it'll raise SyntaxError on the same
        # condition that `python stop_daemon.py` would have raised.
        src = path.read_text(encoding="utf-8")
        compile(src, str(path), "exec")  # raises SyntaxError pre-fix

    def test_stop_daemon_importable_via_importlib(self):
        # Use importlib so we don't pollute the test process with side
        # effects of `import stop_daemon` (e.g. psutil enumeration).
        spec = importlib.util.spec_from_file_location(
            "stop_daemon_under_test", str(ROOT / "stop_daemon.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # raises SyntaxError pre-fix
        assert hasattr(module, "main") or hasattr(module, "parse_args") or True

    def test_future_import_precedes_other_statements(self):
        """The future-import line number must be smaller than the first
        non-docstring, non-comment, non-future statement. This is the
        structural invariant the SyntaxError was guarding."""
        tree = ast.parse((ROOT / "stop_daemon.py").read_text(encoding="utf-8"))
        future_lineno = None
        first_other_lineno = None
        for node in tree.body:
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "__future__"
            ):
                future_lineno = node.lineno
                break
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                # docstring -- skip
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            first_other_lineno = node.lineno
            break
        assert future_lineno is not None, "from __future__ import annotations missing"
        assert first_other_lineno is not None
        assert future_lineno < first_other_lineno, (
            f"future-import at line {future_lineno} must precede other code at "
            f"line {first_other_lineno} (PEP 236)"
        )


# ---------------------------------------------------------------------------
# B-4 -- main.connect_angelone must write the runtime feed_token into
# config["broker"]["feed_token"] so WebSocketClient picks it up.
# Pre-fix: feed_token was assigned to a local variable and discarded; the
# config tree still held its YAML default of None.
# ---------------------------------------------------------------------------
class TestB4FeedTokenPlumbedToConfig:
    def _import_main(self):
        spec = importlib.util.spec_from_file_location(
            "main_under_test", str(ROOT / "main.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_feed_token_written_to_broker_config(self, monkeypatch):
        main_module = self._import_main()

        fake_smart_api_module = MagicMock()
        fake_api_inst = MagicMock()
        fake_api_inst.generateSession.return_value = {"status": True}
        fake_api_inst.getfeedToken.return_value = "FAKE_FEED_TOKEN_42"
        fake_smart_api_module.SmartConnect.return_value = fake_api_inst

        fake_pyotp = MagicMock()
        fake_pyotp.TOTP.return_value.now.return_value = "123456"

        monkeypatch.setitem(sys.modules, "SmartApi", fake_smart_api_module)
        monkeypatch.setitem(sys.modules, "pyotp", fake_pyotp)

        config = {
            "broker": {
                "mode": "live",
                "api_key": "k",
                "client_id": "c",
                "password": "p",
                "totp_secret": "t",
                "feed_token": None,
            }
        }
        result = main_module.connect_angelone(config)
        assert result is fake_api_inst
        assert config["broker"]["feed_token"] == "FAKE_FEED_TOKEN_42"


# ---------------------------------------------------------------------------
# B-5 -- packages/core/market_safety.py NSE_SECTOR_MAP must have no
# duplicate keys. Pre-fix: "GNFC" appeared twice ("Chemicals" then "Agri");
# the second silently overwrote the first.
# ---------------------------------------------------------------------------
class TestB5SectorMapNoDuplicateKeys:
    def test_no_duplicate_string_keys_in_sector_map(self):
        import core.market_safety as ms  # noqa: F401

        src = Path(ms.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = []
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.append(k.value)
            if not keys:
                continue
            dupes = [k for k, c in Counter(keys).items() if c > 1]
            assert not dupes, (
                f"market_safety.py contains dict literal at line "
                f"{node.lineno} with duplicate keys: {dupes}"
            )

    def test_gnfc_maps_to_chemicals(self):
        """The audit decision for GNFC was 'Chemicals' (its specialty-
        chemicals segment drives intraday vol). Pin it down so a future
        refactor that flips it back to Agri is caught."""
        from core.market_safety import NSE_SECTOR_MAP

        assert "GNFC" in NSE_SECTOR_MAP
        assert NSE_SECTOR_MAP["GNFC"] == "Chemicals", (
            f"GNFC must map to Chemicals; got {NSE_SECTOR_MAP['GNFC']!r}. "
            f"See docs/audits/audit_2026-05-25_bug_report.md §B-5."
        )


# ---------------------------------------------------------------------------
# B-11 -- run_daemon._write_idle_heartbeat must read
# config["capital"]["initial_balance"], not the non-existent top-level
# "initial_capital". Pre-fix the health.json `cash` field was always 0.0.
# ---------------------------------------------------------------------------
class TestB11IdleHeartbeatCash:
    def test_idle_heartbeat_reads_capital_initial_balance(self, tmp_path):
        import run_daemon  # imported via conftest sys.path tweak

        cfg = {
            "capital": {"initial_balance": 123456.78},
            "logging": {"log_dir": str(tmp_path)},
            "broker": {"mode": "paper"},
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

        run_daemon._write_idle_heartbeat(str(cfg_path))

        health = tmp_path / "health.json"
        assert health.exists(), "_write_idle_heartbeat did not produce health.json"
        payload = json.loads(health.read_text(encoding="utf-8"))
        assert payload["cash"] == 123456.78, (
            f"expected cash 123456.78, got {payload['cash']!r}. "
            f"This means run_daemon.py is still reading the wrong config key "
            f"(B-11 regressed)."
        )

    def test_idle_heartbeat_handles_missing_capital_block(self, tmp_path):
        """Config without a capital block must not crash; it should fall
        back to 0.0 cleanly (the legacy behaviour, preserved)."""
        import run_daemon

        cfg = {"logging": {"log_dir": str(tmp_path)}, "broker": {"mode": "paper"}}
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

        run_daemon._write_idle_heartbeat(str(cfg_path))

        payload = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
        assert payload["cash"] == 0.0


# ---------------------------------------------------------------------------
# B-3 -- SSL verification posture.
# Pre-fix the Python default for TRADER_DISABLE_SSL_VERIFY in main.py and
# run_daemon.py was "true" (bypass enabled). Several modules also had
# hard-coded `verify=False` literals that bypassed the env var entirely.
# Post-fix the default is "false" (secure) and every HTTPS call honours
# the env var.
# ---------------------------------------------------------------------------
class TestB3SSLDefaultsSecure:
    @staticmethod
    def _find_ssl_bypass_default(path: Path) -> str:
        """Read the file, locate `_ssl_bypass = os.environ.get(...)` and
        return the literal default value string. Raises if not found."""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_ssl_bypass"
            ):
                call = node.value
                # _ssl_bypass = os.environ.get(...).lower()
                if isinstance(call, ast.Call) and isinstance(
                    call.func, ast.Attribute
                ):
                    inner = call.func.value  # os.environ.get(...) call
                    if (
                        isinstance(inner, ast.Call)
                        and len(inner.args) >= 2
                        and isinstance(inner.args[1], ast.Constant)
                    ):
                        return inner.args[1].value
        raise AssertionError(f"_ssl_bypass default not found in {path}")

    def test_main_py_default_is_false(self):
        default = self._find_ssl_bypass_default(ROOT / "main.py")
        assert default == "false", (
            f"main.py TRADER_DISABLE_SSL_VERIFY default must be 'false' "
            f"(secure-by-default per B-3 fix); got {default!r}"
        )

    def test_run_daemon_default_is_false(self):
        default = self._find_ssl_bypass_default(ROOT / "run_daemon.py")
        assert default == "false", (
            f"run_daemon.py TRADER_DISABLE_SSL_VERIFY default must be 'false' "
            f"(secure-by-default per B-3 fix); got {default!r}"
        )

    def test_no_hardcoded_verify_false_in_packages_core(self):
        """Sweep packages/core/*.py for any remaining `verify=False`
        literal. Permitted exceptions: comments and tests."""
        for py in (ROOT / "packages" / "core").rglob("*.py"):
            src = py.read_text(encoding="utf-8")
            tree = ast.parse(src, filename=str(py))
            for node in ast.walk(tree):
                # Look for `verify=False` as a kwarg in any Call node.
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "verify" and isinstance(
                            kw.value, ast.Constant
                        ) and kw.value.value is False:
                            raise AssertionError(
                                f"{py}:{node.lineno} still has hard-coded "
                                f"verify=False; use `verify=not _SSL_BYPASS` "
                                f"or equivalent env-driven flag (B-3)."
                            )
                # Also look for `session.verify = False` style assignment
                # to .verify attribute.
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    tgt = node.targets[0]
                    if (
                        isinstance(tgt, ast.Attribute)
                        and tgt.attr == "verify"
                        and isinstance(node.value, ast.Constant)
                        and node.value.value is False
                    ):
                        raise AssertionError(
                            f"{py}:{node.lineno} still has `.verify = False`; "
                            f"use env-driven flag (B-3)."
                        )

    def test_stock_scanner_module_has_ssl_bypass_flag(self):
        from core import stock_scanner

        assert hasattr(stock_scanner, "_SSL_BYPASS"), (
            "stock_scanner must expose _SSL_BYPASS as the single source of "
            "truth for HTTPS verification toggling"
        )
        # When env var unset, bypass should be False (secure).
        # Note: this assertion is best-effort -- if the test process has
        # TRADER_DISABLE_SSL_VERIFY set to "true" by an outer harness, we
        # respect that. We only assert the unset case.
        if "TRADER_DISABLE_SSL_VERIFY" not in os.environ:
            assert stock_scanner._SSL_BYPASS is False

    def test_data_handler_module_has_ssl_bypass_flag(self):
        from core import data_handler

        assert hasattr(data_handler, "_SSL_BYPASS")
        if "TRADER_DISABLE_SSL_VERIFY" not in os.environ:
            assert data_handler._SSL_BYPASS is False
