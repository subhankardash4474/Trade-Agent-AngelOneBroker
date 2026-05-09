"""Broker abstraction layer.

A factory + thin abstract interface so the agent picks brokers via config:

    config.yaml:
        broker:
          name: angelone   # angelone | paper

    code:
        from brokers import get_broker
        broker = get_broker(config)
        broker.connect()

The legacy ``smart_api`` parameter pattern (passing ``SmartConnect`` directly
to ``ExecutionEngine``) still works during the transition — call
``broker.raw_api`` and pass that. Weekend cutover replaces that pattern with
the typed methods on the ``Broker`` class.
"""
from __future__ import annotations

from .base import Broker, BrokerLoginError, BrokerNotConnectedError

__all__ = ["Broker", "BrokerLoginError", "BrokerNotConnectedError", "get_broker"]


def get_broker(config: dict) -> Broker:
    """Construct (but do not connect) the broker named in ``config.broker.name``.

    Recognised names:
      - ``angelone`` (default)
      - ``paper``    (no broker — execution layer simulates fills)

    Raises ``ValueError`` for unknown broker names.
    """
    name = (config.get("broker", {}) or {}).get("name", "angelone").lower()
    mode = (config.get("broker", {}) or {}).get("mode", "paper").lower()

    if mode == "paper":
        # Paper mode never needs a real broker connection — return a stub
        # broker that errors loudly if anything tries to call live methods.
        from .paper import PaperBroker
        return PaperBroker(config)

    if name == "angelone":
        from .angelone import AngelOneBroker
        return AngelOneBroker(config)

    raise ValueError(
        f"Unknown broker name '{name}'. Supported: angelone, paper. "
        "Kite was deprecated 2026-05-08 -- see /docs/journal/engineering_journal_2026-05-08.md."
    )
