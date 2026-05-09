"""Paper-mode "broker".

When ``broker.mode == 'paper'``, the agent never talks to a real broker —
fills are simulated locally inside ``core/execution.py``. This stub satisfies
the ``Broker`` ABC so callers that use ``get_broker(config)`` get a usable
object even in paper mode, and any accidental call to a live method raises
loudly instead of silently no-op'ing.
"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from .base import Broker


class PaperBroker(Broker):
    """No-op broker for paper trading. All live methods raise."""

    name = "paper"

    def __init__(self, config: dict):
        self._connected = False
        self._capital = float(config.get("capital", {}).get("initial_balance", 10000.0))

    # ── Lifecycle ────────────────────────────────────────────────────

    def connect(self) -> None:
        logger.info("[PaperBroker] connect() — paper mode, no real broker session")
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # ── Account info ─────────────────────────────────────────────────

    def get_profile(self) -> dict[str, Any]:
        return {
            "name":         "Paper Account",
            "client_code":  "PAPER",
            "email":        "",
            "exchanges":    ["nse_cm", "bse_cm"],
            "raw":          {},
        }

    def get_funds(self) -> dict[str, Any]:
        # Paper mode: report the configured initial balance. The real
        # cash-on-hand is tracked in core/portfolio.py.
        return {
            "available_cash": self._capital,
            "used_margin":    0.0,
            "net":            self._capital,
            "raw":            {},
        }

    def get_positions(self) -> Optional[dict]:
        return None  # tracked in core/portfolio.py for paper mode

    def get_orders(self) -> Optional[dict]:
        return None

    # ── Trading (no-op) ──────────────────────────────────────────────

    def place_order(self, params: dict) -> Optional[str]:
        raise RuntimeError(
            "PaperBroker.place_order() called — paper-mode orders are simulated "
            "in core/execution.ExecutionEngine, not via the broker."
        )

    def modify_order(self, params: dict) -> bool:
        raise RuntimeError("PaperBroker.modify_order() called — see paper executor")

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        raise RuntimeError("PaperBroker.cancel_order() called — see paper executor")

    def get_ltp(self, exchange: str, symbol: str, token: str) -> Optional[float]:
        raise RuntimeError(
            "PaperBroker.get_ltp() called — paper mode uses yfinance via core/data_handler.py."
        )

    @property
    def raw_api(self) -> Any:
        return None
