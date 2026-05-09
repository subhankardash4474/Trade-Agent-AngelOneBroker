"""Unit tests for brokers/ abstraction layer.

We can't unit-test AngelOneBroker against real AngelOne servers without
credentials and network — that's what tools/angelone_smoke.py is for.
Here we cover:

  - get_broker() factory selects correct class by config
  - PaperBroker fulfills the Broker ABC and behaves correctly
  - AngelOneBroker init validates config without trying to connect
  - AngelOneBroker._diagnose hint generation for known error codes
"""
from __future__ import annotations

import pytest

from brokers import get_broker, BrokerLoginError, BrokerNotConnectedError
from brokers.base import Broker
from brokers.paper import PaperBroker
from brokers.angelone import AngelOneBroker


# ── Factory ───────────────────────────────────────────────────────────

def test_factory_returns_paper_when_mode_paper():
    cfg = {"broker": {"name": "angelone", "mode": "paper"}}
    b = get_broker(cfg)
    assert isinstance(b, PaperBroker)
    assert b.name == "paper"


def test_factory_returns_angelone_when_mode_live():
    cfg = {
        "broker": {
            "name": "angelone", "mode": "live",
            "api_key": "k", "client_id": "c", "password": "1234",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
    }
    b = get_broker(cfg)
    assert isinstance(b, AngelOneBroker)
    assert b.name == "angelone"


def test_factory_rejects_unknown_broker():
    cfg = {"broker": {"name": "fyers", "mode": "live"}}
    with pytest.raises(ValueError, match="Unknown broker"):
        get_broker(cfg)


def test_factory_rejects_kite_explicitly():
    """Kite was deprecated 2026-05-08. Must not silently fall through."""
    cfg = {"broker": {"name": "kite", "mode": "live"}}
    with pytest.raises(ValueError, match="Kite was deprecated"):
        get_broker(cfg)


def test_factory_defaults_to_angelone_when_name_missing():
    cfg = {"broker": {"mode": "live", "api_key": "k", "client_id": "c",
                      "password": "1234", "totp_secret": "JBSWY3DPEHPK3PXP"}}
    b = get_broker(cfg)
    assert isinstance(b, AngelOneBroker)


# ── PaperBroker ───────────────────────────────────────────────────────

def test_paper_broker_implements_full_abc():
    b = PaperBroker({"capital": {"initial_balance": 50000}})
    assert isinstance(b, Broker)
    assert not b.is_connected()
    b.connect()
    assert b.is_connected()
    b.disconnect()
    assert not b.is_connected()


def test_paper_broker_get_funds_uses_initial_balance():
    b = PaperBroker({"capital": {"initial_balance": 75000.0}})
    funds = b.get_funds()
    assert funds["available_cash"] == 75000.0
    assert funds["used_margin"] == 0.0
    assert funds["net"] == 75000.0


def test_paper_broker_profile_is_synthetic():
    b = PaperBroker({})
    p = b.get_profile()
    assert p["client_code"] == "PAPER"
    assert "nse_cm" in p["exchanges"]


def test_paper_broker_live_methods_raise_loudly():
    """Paper mode must NEVER silently no-op a live broker call — the paper
    fills happen in core/execution.ExecutionEngine, not here."""
    b = PaperBroker({})
    with pytest.raises(RuntimeError, match="paper-mode orders are simulated"):
        b.place_order({})
    with pytest.raises(RuntimeError):
        b.modify_order({})
    with pytest.raises(RuntimeError):
        b.cancel_order("ord-1")
    with pytest.raises(RuntimeError, match="paper mode uses yfinance"):
        b.get_ltp("NSE", "RELIANCE-EQ", "738561")


def test_paper_broker_raw_api_is_none():
    assert PaperBroker({}).raw_api is None


# ── AngelOneBroker (init-only, no connect) ────────────────────────────

def _angelone_min_cfg(**overrides):
    cfg = {
        "broker": {
            "name": "angelone", "mode": "live",
            "api_key": "k", "api_secret": "s", "client_id": "AACH",
            "password": "1234", "totp_secret": "JBSWY3DPEHPK3PXP",
        },
        "market": {"exchange": "NSE"},
    }
    cfg["broker"].update(overrides)
    return cfg


def test_angelone_init_does_not_connect():
    """Construction must not hit the network — only connect() does."""
    b = AngelOneBroker(_angelone_min_cfg())
    assert not b.is_connected()
    assert b.raw_api is None


def test_angelone_methods_raise_when_disconnected():
    b = AngelOneBroker(_angelone_min_cfg())
    for fn_name in ("get_profile", "get_funds", "get_positions", "get_orders"):
        with pytest.raises(BrokerNotConnectedError):
            getattr(b, fn_name)()
    with pytest.raises(BrokerNotConnectedError):
        b.get_ltp("NSE", "X", "0")
    with pytest.raises(BrokerNotConnectedError):
        b.place_order({})
    with pytest.raises(BrokerNotConnectedError):
        b.modify_order({})
    with pytest.raises(BrokerNotConnectedError):
        b.cancel_order("ord")


def test_angelone_connect_rejects_missing_creds():
    cfg = _angelone_min_cfg()
    cfg["broker"]["totp_secret"] = ""  # missing
    cfg["broker"]["password"] = ""
    b = AngelOneBroker(cfg)
    with pytest.raises(BrokerLoginError, match="missing config keys"):
        b.connect()


def test_angelone_disconnect_is_safe_when_never_connected():
    """No exception — calling disconnect on a fresh broker is a no-op."""
    AngelOneBroker(_angelone_min_cfg()).disconnect()


# ── _diagnose hint generation ─────────────────────────────────────────

class TestDiagnose:
    def test_totp_error_hint(self):
        out = AngelOneBroker._diagnose("AB1007", "totp invalid")
        assert "TOTP rejected" in out
        assert "clock" in out.lower()

    def test_credential_error_hint(self):
        out = AngelOneBroker._diagnose("AB1010", "Invalid client/password")
        assert "client_id or MPin" in out

    def test_credential_hint_via_message(self):
        out = AngelOneBroker._diagnose("OTHER", "wrong password supplied")
        assert "client_id or MPin" in out

    def test_ip_whitelist_hint_no_network(self, monkeypatch):
        # Block ipify lookup so we don't hit the network in tests.
        import urllib.request
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("no net"))
        )
        out = AngelOneBroker._diagnose("AB1050", "ip not whitelisted")
        assert "IP not whitelisted" in out
        assert "?" in out  # public IP unknown

    def test_unknown_error_returns_empty_hint(self):
        out = AngelOneBroker._diagnose("AB9999", "unknown error")
        assert out == ""
