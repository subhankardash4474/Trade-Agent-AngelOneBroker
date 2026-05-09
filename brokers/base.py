"""Abstract broker interface for the trading agent.

Every concrete broker (AngelOne, Kite, paper) implements this interface so
the agent can swap brokers via configuration without touching strategy or
execution logic.

Design notes:
- All public methods MUST be safe to call on a disconnected broker (return
  None or raise BrokerNotConnectedError, never crash the calling code).
- Methods return plain dicts, not broker-specific objects. Subclasses are
  responsible for translating broker-native shapes into the canonical
  shapes documented below.
- This is a thin abstraction. We don't try to hide rare broker-specific
  features (e.g. AngelOne's bracket "ROBO" variety) — instead, the
  implementation can expose them via the `raw_api` accessor.

Canonical shapes:

  funds = {
      "available_cash":   float,    # cash you can deploy now
      "used_margin":      float,    # currently locked in open positions
      "net":              float,    # total net (cash + open MTM)
      "raw":              dict,     # broker-native payload, optional
  }

  profile = {
      "name":             str,
      "client_code":      str,
      "email":            str,
      "exchanges":        list[str],
      "raw":              dict,
  }
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class BrokerNotConnectedError(RuntimeError):
    """Raised when a method requires a live session but the broker is offline."""


class BrokerLoginError(RuntimeError):
    """Raised when login is rejected by the broker (bad creds, IP not whitelisted, etc.)."""


class Broker(ABC):
    """Abstract base for all broker implementations."""

    # Subclasses set this to a short identifier (e.g. "angelone", "paper").
    name: str = "abstract"

    # ── Lifecycle ────────────────────────────────────────────────────

    @abstractmethod
    def connect(self) -> None:
        """Establish a session with the broker. Idempotent — calling on an
        already-connected broker is a no-op. Raises ``BrokerLoginError`` on
        authentication or whitelist failures."""

    @abstractmethod
    def disconnect(self) -> None:
        """Tear down the session cleanly. Safe to call multiple times."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the session is still valid (token not expired)."""

    # ── Account info (read-only) ─────────────────────────────────────

    @abstractmethod
    def get_profile(self) -> dict[str, Any]:
        """Fetch account profile (name, client_code, exchanges)."""

    @abstractmethod
    def get_funds(self) -> dict[str, Any]:
        """Fetch available cash, used margin, net account value."""

    @abstractmethod
    def get_positions(self) -> Optional[dict]:
        """Fetch open positions held with the broker."""

    @abstractmethod
    def get_orders(self) -> Optional[dict]:
        """Fetch the day's order book."""

    # ── Trading ──────────────────────────────────────────────────────

    @abstractmethod
    def place_order(self, params: dict) -> Optional[str]:
        """Place an order. Params are broker-native (passed through). Returns
        order_id on success, None on failure. Concrete brokers document the
        shape of `params`; the canonical agent code in core/execution.py
        builds AngelOne-shaped params today."""

    @abstractmethod
    def modify_order(self, params: dict) -> bool:
        """Modify an existing order (e.g. update SL trigger price)."""

    @abstractmethod
    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        """Cancel a pending order."""

    # ── Market data ──────────────────────────────────────────────────

    @abstractmethod
    def get_ltp(self, exchange: str, symbol: str, token: str) -> Optional[float]:
        """Fetch the last-traded price for a single symbol."""

    # ── Escape hatch ─────────────────────────────────────────────────

    @property
    @abstractmethod
    def raw_api(self) -> Any:
        """Return the underlying broker SDK object for code paths that need
        broker-native methods. Use sparingly — anything reachable through
        ``raw_api`` should ideally have a typed wrapper here too."""
