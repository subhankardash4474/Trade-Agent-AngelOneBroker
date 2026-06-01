"""Mode-flag enforcement layer (v4 charter §2.2, §2.3).

SKELETON — Phase 8 of v4 Mode A scaffolding.

This module is the SINGLE SOURCE OF TRUTH for "what runs and where
orders go" once the v4 charter cuts over to the mode-flag architecture
(Q5: hard cutover after Phase 1 passes). It sits BETWEEN the existing
signal generators and the order-placement layer.

What this skeleton DOES (implemented + tested):

    * Parses the ``strategies.modes`` config block into typed ``ModeSpec``
      objects.
    * Validates the config schema (missing fields, invalid mode types,
      unknown runtimes).
    * Validates capital gates — refuses ``mode: live`` if available
      capital is below the mode's threshold, unless the operator has
      typed the verbatim override string ``"I accept ruin risk"`` into
      ``mode_router.override_capital_gate``.
    * Validates allocation sum — sum of ``capital_allocation_pct`` of
      all enabled paper+live modes must be ≤ ``max_capital_allocation_pct``.
    * Resolves ``cost_model`` and ``signal_module`` references via
      ``importlib`` so the dispatcher can load any pod-compliant
      strategy + cost model.
    * ``active_modes()`` — stable-ordered list of enabled modes.
    * ``disable_mode(name, reason)`` — operator-callable kill switch
      with audit-log emission. In-memory toggle only; no DB write
      (the cutover commit will add DB-persistence).

What this skeleton does NOT do (deferred to the hard-cutover commit
per Q5):

    * ``route_order()`` is a stub. The real implementation requires
      ``PaperBroker`` + live-broker adapter wiring, neither of which
      exist yet. The stub validates the mode is not ``backtest_only``
      (which is enforceable today) and raises ``NotImplementedError``
      for the rest. This is intentional: the dispatcher being loadable
      and the order-routing being callable are SEPARATE failure modes,
      and the skeleton makes the first cheap to test.
    * ``kill_check()`` is a stub. Real kill-criteria evaluation requires
      reading rolling 30d/90d P&L windows from the equity_curve DB —
      not in scope for the skeleton.
    * No ``config.yaml`` modification. The dispatcher accepts a config
      dict; the caller decides where it came from. Until the cutover
      commit lands a ``strategies.modes`` block in the live config, the
      dispatcher only runs against in-memory test fixtures.
    * No DB migration (charter §7.6 backfill ``mode_tag = 'legacy_v2_1'``
      is part of the cutover commit, not the skeleton).

Pod boundaries (per packages/trader/__init__.py docstring):

    Imports allowed:  core, strategies, brokers
    Imports FORBIDDEN: research, ui, training

    This file imports only ``importlib`` + stdlib + (eventually)
    ``packages.core.*`` cost models and ``packages.strategies.*`` signal
    modules via importlib resolution. The pod-boundary test
    (tests/unit/test_pod_boundaries.py) will pick this up.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Public exception types
# ─────────────────────────────────────────────────────────────────────


class ModeConfigError(ValueError):
    """Raised when ``strategies.modes`` config fails schema validation."""


class CapitalGateError(ValueError):
    """Raised when a ``mode: live`` mode lacks the required capital and
    the operator has NOT typed the verbatim override string."""


class AllocationGateError(ValueError):
    """Raised when sum of capital_allocation_pct of enabled (paper|live)
    modes exceeds ``mode_router.max_capital_allocation_pct``."""


class ModeRoutingError(RuntimeError):
    """Raised when route_order is called on a ``backtest_only`` mode
    (which is structurally impossible — backtest_only modes never
    produce live signals)."""


# ─────────────────────────────────────────────────────────────────────
# Verbatim override string (charter §2.3 — typing-as-friction)
# ─────────────────────────────────────────────────────────────────────

OVERRIDE_RUIN_RISK = "I accept ruin risk"


# ─────────────────────────────────────────────────────────────────────
# Capital provider protocol
# ─────────────────────────────────────────────────────────────────────


class CapitalProvider(Protocol):
    """Abstraction over `data/self_sufficiency.json` cash_inr lookup so
    the dispatcher can be tested without filesystem dependencies."""

    def cash_inr(self) -> float: ...


@dataclass
class DictCapitalProvider:
    """Trivial CapitalProvider for tests / in-process callers."""

    _cash_inr: float

    def cash_inr(self) -> float:  # noqa: D401 — Protocol impl
        return self._cash_inr


# ─────────────────────────────────────────────────────────────────────
# Typed mode spec
# ─────────────────────────────────────────────────────────────────────


VALID_MODE_TYPES = frozenset({"backtest_only", "paper", "live"})
VALID_RUNTIMES = frozenset({
    "swing_cnc", "swing_fno_carry", "intraday_fno_options", "intraday_cash",
})


@dataclass
class ModeSpec:
    """Parsed, typed view of one entry under ``strategies.modes.*``."""

    name: str
    enabled: bool
    mode: str
    capital_allocation_pct: float
    runtime: str
    backtester_variant: str
    signal_module: str
    cost_model: str
    paper_to_live_threshold: Dict[str, Any] = field(default_factory=dict)
    kill_criteria: Dict[str, Any] = field(default_factory=dict)
    frozen_until: Optional[str] = None
    reason: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, d: Dict[str, Any]) -> "ModeSpec":
        try:
            mode_value = d["mode"]
        except KeyError as e:
            raise ModeConfigError(
                f"mode '{name}': missing required field 'mode'"
            ) from e
        if mode_value not in VALID_MODE_TYPES:
            raise ModeConfigError(
                f"mode '{name}': mode={mode_value!r} not in "
                f"{sorted(VALID_MODE_TYPES)}"
            )

        runtime_value = d.get("runtime", "swing_cnc")
        if runtime_value not in VALID_RUNTIMES:
            raise ModeConfigError(
                f"mode '{name}': runtime={runtime_value!r} not in "
                f"{sorted(VALID_RUNTIMES)}"
            )

        for required in ("signal_module", "cost_model", "backtester_variant"):
            if required not in d:
                # Legacy disabled-only modes (e.g. swing_combined_shorts_legacy)
                # don't need signal_module/cost_model — they're config-only
                # placeholders. Skip the strict check if enabled=False.
                if not d.get("enabled", False):
                    continue
                raise ModeConfigError(
                    f"mode '{name}': enabled mode missing required "
                    f"field '{required}'"
                )

        return cls(
            name=name,
            enabled=bool(d.get("enabled", False)),
            mode=mode_value,
            capital_allocation_pct=float(d.get("capital_allocation_pct", 0)),
            runtime=runtime_value,
            backtester_variant=d.get("backtester_variant", ""),
            signal_module=d.get("signal_module", ""),
            cost_model=d.get("cost_model", ""),
            paper_to_live_threshold=dict(d.get("paper_to_live_threshold", {})),
            kill_criteria=dict(d.get("kill_criteria", {})),
            frozen_until=d.get("frozen_until"),
            reason=d.get("reason"),
            raw=dict(d),
        )


# ─────────────────────────────────────────────────────────────────────
# Routing / kill-check result placeholders
# ─────────────────────────────────────────────────────────────────────


@dataclass
class RoutingDecision:
    """Result of ModeDispatcher.route_order. Skeleton placeholder."""

    mode_name: str
    target: str  # "paper_broker" | "live_broker"
    mode_tag: str
    cost_model_ref: str
    signal_module_ref: str


@dataclass
class KillCheckResult:
    """Result of ModeDispatcher.kill_check. Skeleton placeholder."""

    mode_name: str
    window: str  # "backtest" | "paper" | "live"
    passed: bool
    triggered_criteria: List[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# ModeDispatcher
# ─────────────────────────────────────────────────────────────────────


class ModeDispatcher:
    """Single source of truth for what runs and where orders go.

    Skeleton scope: schema validation + capital gate + allocation gate
    + cost/signal module resolution + active_modes() + disable_mode().
    route_order() and kill_check() are stubs to be filled in by the
    hard-cutover commit per Q5.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        capital_provider: CapitalProvider,
        *,
        module_resolver: Optional[Callable[[str], Any]] = None,
    ) -> None:
        """
        Args:
            config: A dict mirroring the live config.yaml shape; needs at
                least ``config["strategies"]["modes"]`` and
                ``config["mode_router"]``. We DO NOT read from disk here —
                the caller decides where the config came from.
            capital_provider: Lookup for current cash_inr (the live
                impl reads ``data/self_sufficiency.json``; tests pass
                a ``DictCapitalProvider``).
            module_resolver: Optional importlib.import_module replacement
                for tests. Defaults to ``importlib.import_module``.
        """
        self._config = config
        self._capital_provider = capital_provider
        self._module_resolver: Callable[[str], Any] = (
            module_resolver or importlib.import_module
        )

        self._modes: Dict[str, ModeSpec] = self._parse_modes(config)
        self._validate_capital_gates()
        self._validate_allocation_sum()

        self._cost_models: Dict[str, Any] = self._load_cost_models()
        self._signal_modules: Dict[str, Any] = self._load_signal_modules()

    # ── schema parsing ───────────────────────────────────────────────

    @staticmethod
    def _parse_modes(config: Dict[str, Any]) -> Dict[str, ModeSpec]:
        try:
            strategies = config["strategies"]
        except KeyError as e:
            raise ModeConfigError(
                "config missing top-level 'strategies' key"
            ) from e
        try:
            modes_dict = strategies["modes"]
        except KeyError as e:
            raise ModeConfigError(
                "config missing 'strategies.modes' block"
            ) from e
        if not isinstance(modes_dict, dict):
            raise ModeConfigError(
                f"'strategies.modes' must be a dict, got {type(modes_dict).__name__}"
            )
        return {name: ModeSpec.from_dict(name, d) for name, d in modes_dict.items()}

    # ── capital gate (charter §2.3) ──────────────────────────────────

    def _validate_capital_gates(self) -> None:
        router = self._config.get("mode_router", {})
        override = router.get("override_capital_gate", "")
        current_capital = self._capital_provider.cash_inr()

        for spec in self._modes.values():
            if not spec.enabled or spec.mode != "live":
                continue
            required = float(
                spec.paper_to_live_threshold.get("capital_inr", 0)
            )
            if current_capital >= required:
                continue
            if override != OVERRIDE_RUIN_RISK:
                raise CapitalGateError(
                    f"Mode {spec.name!r} requires capital_inr >= "
                    f"{required:,.0f}; have {current_capital:,.0f}. "
                    f"Either set mode to 'paper' or override "
                    f"mode_router.override_capital_gate to the exact "
                    f"string {OVERRIDE_RUIN_RISK!r}."
                )
            logger.critical(
                "[CAPITAL-GATE-OVERRIDE] mode=%s required=%s have=%s "
                "operator accepted ruin risk explicitly",
                spec.name, required, current_capital,
            )

    # ── allocation sum gate (charter §2.1, §2.3) ─────────────────────

    def _validate_allocation_sum(self) -> None:
        router = self._config.get("mode_router", {})
        max_pct = float(router.get("max_capital_allocation_pct", 100))
        total = sum(
            spec.capital_allocation_pct
            for spec in self._modes.values()
            if spec.enabled and spec.mode in ("paper", "live")
        )
        if total > max_pct + 1e-9:
            raise AllocationGateError(
                f"sum(capital_allocation_pct for enabled paper+live modes) "
                f"= {total:.2f}% > max_capital_allocation_pct = {max_pct:.2f}%"
            )

    # ── module resolution ────────────────────────────────────────────

    def _load_cost_models(self) -> Dict[str, Any]:
        """Resolve ``cost_model`` strings of form ``module.path:Class`` or
        ``module.path`` (module-level) into actual objects."""
        out: Dict[str, Any] = {}
        for spec in self._modes.values():
            if not spec.cost_model:
                continue
            out[spec.name] = self._resolve_reference(spec.cost_model)
        return out

    def _load_signal_modules(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for spec in self._modes.values():
            if not spec.signal_module:
                continue
            out[spec.name] = self._resolve_reference(spec.signal_module)
        return out

    def _resolve_reference(self, ref: str) -> Any:
        """Resolve ``a.b.c`` (module) or ``a.b.c:Symbol`` (attribute)."""
        if ":" in ref:
            module_path, attr = ref.split(":", 1)
            mod = self._module_resolver(module_path)
            try:
                return getattr(mod, attr)
            except AttributeError as e:
                raise ModeConfigError(
                    f"reference {ref!r}: module {module_path!r} has no "
                    f"attribute {attr!r}"
                ) from e
        return self._module_resolver(ref)

    # ── public API ───────────────────────────────────────────────────

    def active_modes(self) -> List[ModeSpec]:
        """Return enabled modes in stable (config insertion) order."""
        return [spec for spec in self._modes.values() if spec.enabled]

    def get_mode(self, name: str) -> ModeSpec:
        try:
            return self._modes[name]
        except KeyError as e:
            raise KeyError(
                f"unknown mode {name!r}; known: {sorted(self._modes)}"
            ) from e

    def cost_model_for(self, name: str) -> Any:
        return self._cost_models[name]

    def signal_module_for(self, name: str) -> Any:
        return self._signal_modules[name]

    # ── routing stub (charter §2.2) ──────────────────────────────────

    def route_order(self, signal: Any, mode_name: str) -> RoutingDecision:
        """STUB — full impl lands in the hard-cutover commit (Q5).

        Today the stub only enforces the structural rule that
        ``backtest_only`` modes never route. Anything beyond that
        (paper broker, live broker, mode_tag DB write) raises
        ``NotImplementedError`` until the cutover commit.
        """
        spec = self.get_mode(mode_name)
        if not spec.enabled:
            raise ModeRoutingError(
                f"route_order called on disabled mode {mode_name!r}"
            )
        if spec.mode == "backtest_only":
            raise ModeRoutingError(
                f"route_order called on mode {mode_name!r} with "
                f"mode=backtest_only; backtest_only modes never produce "
                f"routable orders"
            )
        raise NotImplementedError(
            f"route_order skeleton: PaperBroker + live-broker adapter "
            f"wiring lands in the hard-cutover commit (charter §2.2, "
            f"§2.4). Got mode={mode_name!r} mode_type={spec.mode!r}."
        )

    # ── kill check stub ──────────────────────────────────────────────

    def kill_check(self, mode_name: str, window: str) -> KillCheckResult:
        """STUB — full impl reads rolling-30d/90d windows from
        equity_curve DB; lands in the hard-cutover commit."""
        spec = self.get_mode(mode_name)
        if window not in ("backtest", "paper", "live"):
            raise ValueError(
                f"kill_check window must be 'backtest'|'paper'|'live'; "
                f"got {window!r}"
            )
        if window not in spec.kill_criteria:
            raise ValueError(
                f"mode {mode_name!r} has no kill_criteria for window "
                f"{window!r}; available: {sorted(spec.kill_criteria)}"
            )
        raise NotImplementedError(
            f"kill_check skeleton: rolling-window evaluation lands in "
            f"the hard-cutover commit (charter §3.10, §7.2). "
            f"mode={mode_name!r} window={window!r}."
        )

    # ── operator kill switch (in-memory; DB persistence later) ──────

    def disable_mode(self, mode_name: str, reason: str) -> None:
        spec = self.get_mode(mode_name)
        was_enabled = spec.enabled
        spec.enabled = False
        logger.critical(
            "[MODE-DISABLED] mode=%s was_enabled=%s reason=%s",
            mode_name, was_enabled, reason,
        )


__all__ = [
    "ModeDispatcher",
    "ModeSpec",
    "RoutingDecision",
    "KillCheckResult",
    "CapitalProvider",
    "DictCapitalProvider",
    "ModeConfigError",
    "CapitalGateError",
    "AllocationGateError",
    "ModeRoutingError",
    "OVERRIDE_RUIN_RISK",
    "VALID_MODE_TYPES",
    "VALID_RUNTIMES",
]
