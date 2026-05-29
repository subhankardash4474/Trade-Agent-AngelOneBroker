"""HTML email rendering for ``packages/monitoring/alerts.py``.

Why this exists
---------------
Until 2026-05-19 every alert email -- profit diagnostic, EOD summary,
trade post-mortem, daily report -- went out as ``<pre>{body}</pre>``.
The bodies are authored in markdown (headings, tables, code blocks),
so on Gmail / Outlook they rendered as a wall of fixed-width text
that was difficult to scan from a phone.

The 2026-05-19 patch added ``_render_email_html`` and switched both
the SMTP and Resend paths to a multipart/alternative envelope so the
HTML side carries proper formatting and the text/plain side preserves
the raw markdown for CLI mail readers.

These tests pin the new contract:

  * markdown structure (headings, tables, fenced code, lists, bold/italic)
    survives the render pipeline as real HTML tags;
  * malicious-looking inline HTML inside the body is escaped, not
    propagated -- a profit-diagnostic email that happens to contain
    ``<script>`` text must NOT result in a live script tag downstream;
  * the level → accent color contract holds for the known severities;
  * SMTP path emits multipart/alternative with BOTH text/plain and
    text/html parts (regression guard for the "html-only" form);
  * Resend payload carries both ``html`` and ``text`` fields;
  * the renderer falls back to a styled ``<pre>`` if the ``markdown``
    library is missing -- minimal CI images shouldn't crash the daemon.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from packages.monitoring import alerts as alerts_module  # noqa: E402
from packages.monitoring.alerts import (  # noqa: E402
    AlertManager,
    _LEVEL_COLORS,
    _render_email_html,
)


# ────────────────────────────────────────────────────────────────────
# Renderer unit tests
# ────────────────────────────────────────────────────────────────────


def test_render_email_html_converts_markdown_headings_to_html():
    body = "# Daily Report\n\nClosed: 3 trades."
    html = _render_email_html(body, level="info", subject="EOD")
    assert "<h1>Daily Report</h1>" in html


def test_render_email_html_converts_markdown_tables():
    body = (
        "| Symbol | P&L |\n"
        "|--------|-----|\n"
        "| INFY   | +120 |\n"
        "| TCS    | -45  |\n"
    )
    html = _render_email_html(body, level="info")
    assert "<table" in html
    assert "<th" in html
    assert "<td" in html
    assert "INFY" in html and "TCS" in html


def test_render_email_html_renders_fenced_code_blocks():
    body = "```\norder_id=ABC123\nstatus=FILLED\n```"
    html = _render_email_html(body, level="info")
    assert "<code>" in html
    assert "order_id=ABC123" in html


def test_render_email_html_escapes_inline_html_inside_text():
    """A body that contains literal '<script>' must not become a real
    script tag in the rendered HTML. The markdown library escapes raw
    HTML by default; this test guards against a future regression
    where someone enables the `markdown` ``safe_mode``/``html_in_md``
    extension and accidentally lets HTML through.
    """
    body = "Status: <script>alert('x')</script> ok."
    html = _render_email_html(body, level="info")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize("level", list(_LEVEL_COLORS.keys()))
def test_render_email_html_uses_level_accent_color(level: str):
    expected_hex = _LEVEL_COLORS[level]
    html = _render_email_html("body", level=level)
    assert expected_hex.lower() in html.lower()


def test_render_email_html_unknown_level_uses_neutral_accent():
    """The default branch protects against e.g. ``level='debug'`` from
    an older caller — the renderer must not crash, just fall back to
    the neutral accent. ``#333333`` is what the implementation uses.
    """
    html = _render_email_html("body", level="totally-bogus-level")
    assert "#333333" in html.lower()


def test_render_email_html_includes_subject_strip_when_provided():
    html = _render_email_html("body", level="info", subject="Trade Closed: INFY")
    assert "Trade Closed: INFY" in html


def test_render_email_html_omits_subject_strip_when_empty():
    html = _render_email_html("body", level="info", subject="")
    # No bare-subject div should appear if no subject was passed.
    assert ">Trade Closed:" not in html


def test_render_email_html_falls_back_to_pre_when_markdown_missing(monkeypatch):
    """If the `markdown` package isn't installed (minimal CI image),
    the renderer must still produce something readable -- a styled
    ``<pre>`` block with the body HTML-escaped. The daemon should not
    crash just because an optional dep is absent.
    """
    monkeypatch.setattr(alerts_module, "_MARKDOWN_AVAILABLE", False)
    body = "# heading\n\nsome <tag> body"
    html = _render_email_html(body, level="info")
    assert "<pre" in html
    # Heading marker is NOT converted (markdown is disabled) -- we
    # just want the raw body to come through escaped.
    assert "# heading" in html
    assert "&lt;tag&gt;" in html


def test_render_email_html_includes_ist_timestamp_footer():
    html = _render_email_html("body", level="info")
    assert "Trading Agent" in html
    assert "IST" in html


# ────────────────────────────────────────────────────────────────────
# SMTP path: multipart/alternative integration
# ────────────────────────────────────────────────────────────────────


def _smtp_cfg(tmp_path: Path) -> dict:
    """Minimal alerts config for the SMTP path. ``dedup.state_path`` is
    pinned to a fresh file so the renderer test does not interact with
    a sibling test's dedup state.
    """
    return {
        "monitoring": {
            "alerts": {
                "enabled": True,
                "email": {
                    "enabled": True,
                    "provider": "smtp",
                    "smtp_server": "smtp.example.com",
                    "smtp_port": 587,
                    "sender": "from@example.com",
                    "recipient": "ops@example.com",
                    "password": "x",
                },
                "dedup": {
                    "ttl_minutes": 60,
                    "state_path": str(tmp_path / "dedup.json"),
                },
            }
        }
    }


def test_smtp_path_emits_multipart_alternative_with_text_and_html(tmp_path: Path):
    """The SMTP send must produce a multipart/alternative message that
    carries BOTH a text/plain part (the raw markdown body) and a
    text/html part (the rendered HTML). Operators using a CLI mail
    reader (mutt, neomutt) get the source; Gmail/Outlook get the
    rendered version. Regression guard for the pre-2026-05-19 form
    that sent html-only.
    """
    mgr = AlertManager(_smtp_cfg(tmp_path))

    captured = {}

    class _FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args, **kwargs):
            return False

        def starttls(self):
            pass

        def login(self, *args, **kwargs):
            pass

        def send_message(self, msg):
            captured["msg"] = msg

    with patch.object(alerts_module.smtplib, "SMTP", _FakeSMTP):
        ok = mgr._send_email_smtp(
            subject="Test",
            body="# Heading\n\n| col | val |\n|-----|-----|\n| a | 1 |",
            level="info",
        )

    assert ok is True
    msg = captured["msg"]
    assert msg.is_multipart()
    assert msg.get_content_type() == "multipart/alternative"

    parts = list(msg.walk())
    plain_parts = [p for p in parts if p.get_content_type() == "text/plain"]
    html_parts = [p for p in parts if p.get_content_type() == "text/html"]
    assert len(plain_parts) == 1, "expected exactly one text/plain part"
    assert len(html_parts) == 1, "expected exactly one text/html part"

    plain_body = plain_parts[0].get_payload(decode=True).decode("utf-8")
    html_body = html_parts[0].get_payload(decode=True).decode("utf-8")
    assert "# Heading" in plain_body, "text/plain must preserve raw markdown"
    assert "<h1>" in html_body, "text/html must carry rendered tags"
    assert "<table" in html_body, "table extension must render markdown tables"


# ────────────────────────────────────────────────────────────────────
# Resend path: payload contains both `html` and `text`
# ────────────────────────────────────────────────────────────────────


def _resend_cfg(tmp_path: Path) -> dict:
    # Bug M (2026-05-29): ``failed_alerts_dir`` MUST be set so the
    # production ``_spool_failed_alert`` helper writes into ``tmp_path``
    # instead of the repo's ``logs/failed_alerts/`` directory. Until this
    # field landed, tests like ``test_resend_spool_payload_persists_level``
    # leaked ~150 "Test/boom/critical" spool files onto the trader VM
    # over a 10-day window. See packages/monitoring/alerts.py:_FAILED_ALERTS_DIR.
    return {
        "monitoring": {
            "alerts": {
                "enabled": True,
                "failed_alerts_dir": str(tmp_path / "failed_alerts"),
                "email": {
                    "enabled": True,
                    "provider": "resend",
                    "resend_api_key": "test_key",
                    "sender": "from@example.com",
                    "recipient": "ops@example.com",
                },
                "dedup": {
                    "ttl_minutes": 60,
                    "state_path": str(tmp_path / "dedup.json"),
                },
            }
        }
    }


def test_resend_payload_includes_both_html_and_text(tmp_path: Path):
    """Resend accepts both ``html`` and ``text`` body fields. The
    pre-2026-05-19 code only sent ``html``, so clients that prefer
    text/plain (a small but real cohort -- some accessibility tools,
    text-only mail clients) got nothing. Pin the new contract.
    """
    mgr = AlertManager(_resend_cfg(tmp_path))

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = ""

    captured = {}

    def _fake_post(url, headers=None, data=None, **kwargs):
        import json as _json
        captured["payload"] = _json.loads(data)
        return fake_resp

    with patch.object(alerts_module.requests, "post", side_effect=_fake_post):
        ok = mgr._send_email_resend(
            subject="Test",
            body="# Heading\n\nbody text",
            level="warning",
        )

    assert ok is True
    payload = captured["payload"]
    assert "html" in payload
    assert "text" in payload
    assert "<h1>Heading</h1>" in payload["html"]
    assert "# Heading" in payload["text"]
    # Level-coloured accent threaded through:
    assert _LEVEL_COLORS["warning"].lower() in payload["html"].lower()


def test_resend_spool_payload_persists_level(tmp_path: Path):
    """When a Resend send fails after all retries, the spool file used
    to be missing ``level`` -- the drain path then defaulted to
    ``"info"`` and dropped the original severity (warning / error /
    critical) on replay. This test pins ``level`` into the spool.

    Bug M (2026-05-29): the wrapper below now forwards ``**kwargs`` so
    the ``spool_dir=self._failed_alerts_dir`` keyword threaded by
    ``AlertManager._send_email_resend`` reaches the real spool helper,
    and the resulting file lands inside ``tmp_path`` instead of the
    repo's ``logs/failed_alerts/`` tree.
    """
    mgr = AlertManager(_resend_cfg(tmp_path))

    fake_resp = MagicMock()
    fake_resp.status_code = 401  # non-retryable; spool path
    fake_resp.text = "invalid api key"

    captured_files = []
    original_spool = alerts_module._spool_failed_alert

    def _capture(payload, reason, **kwargs):
        captured_files.append(dict(payload))
        return original_spool(payload, reason, **kwargs)

    with patch.object(alerts_module.requests, "post", return_value=fake_resp), \
         patch.object(alerts_module, "_spool_failed_alert", side_effect=_capture):
        ok = mgr._send_email_resend(
            subject="Test",
            body="boom",
            level="critical",
        )

    assert ok is False
    assert captured_files, "spool helper should have been called once"
    assert captured_files[0]["level"] == "critical"


# ────────────────────────────────────────────────────────────────────
# Bug M (2026-05-29) — test pollution leak into production spool
# ────────────────────────────────────────────────────────────────────


def test_spool_lands_in_config_dir_not_cwd_bug_m(tmp_path: Path, monkeypatch):
    """Pin the Bug M fix: when ``monitoring.alerts.failed_alerts_dir`` is
    set, a Resend send-failure spool file MUST land in that dir and MUST
    NOT touch the legacy CWD-relative ``logs/failed_alerts/`` location.

    Until 2026-05-29 the spool helper resolved ``_FAILED_ALERTS_DIR =
    Path("logs") / "failed_alerts"`` directly, ignoring per-instance
    config. Running ``test_resend_spool_payload_persists_level`` on the
    trader VM leaked 150+ "Test/boom/critical" spool files into
    ``/opt/trading-agent/logs/failed_alerts/``. This test enforces the
    contract that closes that leak.
    """
    # Cd into a clean tmp dir so the legacy ``logs/failed_alerts/``
    # ALSO would be inside tmp_path -- any leak still ends up isolated
    # from the developer's repo. Belt + suspenders: we then assert the
    # configured dir got the file AND the legacy dir is empty.
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "isolated_spool"
    cfg = _resend_cfg(tmp_path)
    cfg["monitoring"]["alerts"]["failed_alerts_dir"] = str(cfg_dir)
    mgr = AlertManager(cfg)

    fake_resp = MagicMock()
    fake_resp.status_code = 401
    fake_resp.text = "invalid api key"

    with patch.object(alerts_module.requests, "post", return_value=fake_resp):
        ok = mgr._send_email_resend(subject="Test", body="boom", level="critical")

    assert ok is False
    cfg_files = list(cfg_dir.glob("*.json"))
    assert len(cfg_files) == 1, (
        f"expected exactly 1 spool file in {cfg_dir!s}, found {len(cfg_files)}"
    )
    legacy_dir = tmp_path / "logs" / "failed_alerts"
    legacy_files = list(legacy_dir.glob("*.json")) if legacy_dir.exists() else []
    assert legacy_files == [], (
        f"Bug M regression: spool helper wrote to legacy CWD-relative "
        f"path {legacy_dir!s} instead of the configured dir {cfg_dir!s}. "
        f"On the trader VM this re-introduces the test \u2192 prod spool leak."
    )


def test_drain_purges_test_pollution_payloads_bug_m(tmp_path: Path):
    """Pin the Bug M defense-in-depth guard: ``drain_failed_alerts`` MUST
    silently delete any spool file whose payload matches the known
    test-pollution fingerprint (subject="Test", body="boom") instead of
    replaying it through the live Resend/SMTP path.

    Without this guard, the next healthy drain after the trader VM
    re-deploys would push every survivor through to ops as a CRITICAL
    email -- exactly the spam scenario the path-fix above prevents
    going forward but does NOT clean up.
    """
    cfg = _resend_cfg(tmp_path)
    cfg_dir = tmp_path / "failed_alerts"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    # Seed two pollution files + one legitimate spooled alert.
    import json as _json
    pollution_a = cfg_dir / "2026-05-29T100000_Test_aaaaaa.json"
    pollution_b = cfg_dir / "2026-05-29T100001_Test_bbbbbb.json"
    legit = cfg_dir / "2026-05-29T100002_Daily_Report_cccccc.json"
    pollution_a.write_text(_json.dumps({
        "provider": "resend", "subject": "Test", "body": "boom",
        "level": "critical", "spooled_at": "2026-05-29T100000",
        "reason": "http_401: invalid api key",
    }), encoding="utf-8")
    pollution_b.write_text(_json.dumps({
        "provider": "resend", "subject": "Test", "body": "boom",
        "level": "critical", "spooled_at": "2026-05-29T100001",
        "reason": "http_401: invalid api key",
    }), encoding="utf-8")
    legit.write_text(_json.dumps({
        "provider": "resend", "subject": "Daily Report", "body": "Day PnL: +500",
        "level": "info", "spooled_at": "2026-05-29T100002",
        "reason": "network: ConnectionError: tmp dns flake",
    }), encoding="utf-8")

    mgr = AlertManager(cfg)

    # Mock the live send so the legitimate alert "succeeds" on replay.
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = ""

    with patch.object(alerts_module.requests, "post", return_value=fake_resp):
        result = mgr.drain_failed_alerts()

    assert result["purged_test"] == 2, (
        f"expected 2 test-pollution payloads purged, got {result['purged_test']}. "
        f"The (subject='Test', body='boom') guard must catch every legacy file."
    )
    assert result["sent"] == 1, (
        f"expected the 1 legitimate Daily Report to drain through, got "
        f"{result['sent']}."
    )
    # All three files removed from disk (2 purged, 1 successfully sent).
    remaining = list(cfg_dir.glob("*.json"))
    assert remaining == [], (
        f"drain should leave the spool dir empty after a clean run; "
        f"survivors: {[p.name for p in remaining]}"
    )


# ────────────────────────────────────────────────────────────────────
# Module-level import sanity (regression guard for the new optional dep)
# ────────────────────────────────────────────────────────────────────


def test_alerts_module_imports_when_markdown_missing(monkeypatch):
    """The ``markdown`` dep is OPTIONAL. Simulate its absence and
    confirm that re-importing ``packages.monitoring.alerts`` still
    succeeds (and just flips ``_MARKDOWN_AVAILABLE`` to False).
    """
    real_markdown = sys.modules.pop("markdown", None)
    monkeypatch.setitem(sys.modules, "markdown", None)
    try:
        # Force a fresh import path
        sys.modules.pop("packages.monitoring.alerts", None)
        mod = importlib.import_module("packages.monitoring.alerts")
        assert mod._MARKDOWN_AVAILABLE is False
        # Renderer still works:
        html = mod._render_email_html("hello", level="info")
        assert "hello" in html
    finally:
        # Restore module state for other tests
        if real_markdown is not None:
            sys.modules["markdown"] = real_markdown
        sys.modules.pop("packages.monitoring.alerts", None)
        importlib.import_module("packages.monitoring.alerts")
