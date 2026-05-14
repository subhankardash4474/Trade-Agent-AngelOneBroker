"""Self-sufficiency tracker.

The agent's stated goal (per the account owner, 2026-05-14) is **passive
income that covers its own running costs**. This module captures the
fixed-cost floor and the cumulative realised P&L since deployment, then
exposes a small status surface so the audit checkpoint and any external
caller can answer the only question that really matters:

    "Has the agent paid for itself yet?"

The numbers are intentionally honest. We do NOT count unrealised P&L --
only what we have actually pulled out as broker-confirmed realised gains.
We compare that against the *full* monthly cost stack:
  * Broker API: Angel One SmartAPI is FREE (vs Kite's Rs 2000/mo) -- this
    is the single biggest line item we save by using Angel.
  * Per-trade brokerage + STT/exchange/SEBI/stamp/GST -- variable, NOT
    in this fixed-cost figure (it's already netted into trade-level pnl).
  * Cloud VM (OCI Always-Free tier = Rs 0; upgraded shapes Rs 400-800).
  * CDSL/DP charges (delivery only -- Rs 13.5 per ISIN per debit day;
    MIS-only = Rs 0).
  * Cursor Pro (~Rs 1700/mo at $20/mo, only if the operator pays for it
    out of trading P&L; many keep it as a personal expense).
  * Internet/electricity allocation (~Rs 500/mo).

Default-cost numbers are based on prevailing 2026 prices and the account
owner's deployment (single-VM OCI, Angel One SmartAPI, MIS-only). Any
operator can override via ``risk.self_sufficiency`` in ``config.yaml``::

    risk:
      self_sufficiency:
        enabled: true
        monthly_fixed_cost_inr: 2500
        trading_days_per_month: 20
        ledger_path: "data/self_sufficiency.json"
        # Optional warn-when-burning-cash floor: if cumulative realised
        # is more than `red_floor_inr` below break-even, surface RED.
        red_floor_inr: 5000

The ledger file is a flat JSON of the form::

    {
        "deployed_on": "2026-05-12",
        "cumulative_realised_inr": -745.36,
        "last_update": "2026-05-14T13:01:15+05:30"
    }

It is updated atomically (write-temp + rename) every time
``record_realised_pnl()`` is called from the post-close hook in
``trading_agent.py`` so a daemon crash mid-update can't corrupt it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


# Cost defaults reflect 2026-05 prices for an OCI Always-Free + Angel One
# SmartAPI (free) + Cursor Pro stack. Stack breakdown:
#   Angel SmartAPI: Rs 0 (the killer feature vs Kite's Rs 2000/mo)
#   Cursor Pro:     Rs 1700 (only if paid out of trading P&L)
#   OCI VM:         Rs 0    (Always-Free tier; upgraded shapes Rs 400-800)
#   CDSL/DP:        Rs 0    (MIS-only; delivery would add ~Rs 13.5/ISIN/day)
#   Internet/power: Rs 500  (fair-share allocation)
#   Misc/buffer:    Rs 300
#   ──────────────────────
#   Total:          Rs 2500/month  =>  ~Rs 125/trading-day breakeven
# Override per environment via risk.self_sufficiency in config.yaml.
DEFAULT_MONTHLY_FIXED_COST_INR = 2500.0
DEFAULT_TRADING_DAYS_PER_MONTH = 20
DEFAULT_LEDGER_PATH = "data/self_sufficiency.json"
DEFAULT_RED_FLOOR_INR = 5000.0


@dataclass
class SelfSufficiencyStatus:
    """Snapshot of the agent's self-sufficiency state at a point in time.

    All values are in INR. ``state`` is one of:
      * ``GREEN``  -- cumulative realised >= 0 (profitable since deployment)
      * ``YELLOW`` -- cumulative realised < 0 but within ``red_floor_inr``
      * ``RED``    -- cumulative realised <= -red_floor_inr (bleeding cash)
      * ``UNKNOWN``-- tracker disabled or no ledger yet
    """

    state: str
    cumulative_realised_inr: float
    monthly_fixed_cost_inr: float
    daily_breakeven_inr: float
    days_since_deployment: int
    cost_burned_to_date_inr: float
    coverage_pct: float
    note: str = ""


@dataclass
class SelfSufficiencyTracker:
    """In-memory tracker backed by a JSON ledger file.

    Designed to be cheap to read on every audit checkpoint and cheap to
    update on every closed trade. No external services, no DB locks.
    """

    enabled: bool = True
    monthly_fixed_cost_inr: float = DEFAULT_MONTHLY_FIXED_COST_INR
    trading_days_per_month: int = DEFAULT_TRADING_DAYS_PER_MONTH
    ledger_path: str = DEFAULT_LEDGER_PATH
    red_floor_inr: float = DEFAULT_RED_FLOOR_INR

    # In-memory copy of the on-disk ledger. Always treated as authoritative
    # after load() returns; writes go to disk first (write-temp + rename).
    _ledger: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "SelfSufficiencyTracker":
        cfg = (config or {}).get("risk", {}).get("self_sufficiency", {}) or {}
        tracker = cls(
            enabled=bool(cfg.get("enabled", True)),
            monthly_fixed_cost_inr=float(
                cfg.get("monthly_fixed_cost_inr", DEFAULT_MONTHLY_FIXED_COST_INR)
            ),
            trading_days_per_month=int(
                cfg.get("trading_days_per_month", DEFAULT_TRADING_DAYS_PER_MONTH)
            ),
            ledger_path=str(cfg.get("ledger_path", DEFAULT_LEDGER_PATH)),
            red_floor_inr=float(cfg.get("red_floor_inr", DEFAULT_RED_FLOOR_INR)),
        )
        tracker.load()
        return tracker

    @property
    def daily_breakeven_inr(self) -> float:
        if self.trading_days_per_month <= 0:
            return 0.0
        return self.monthly_fixed_cost_inr / self.trading_days_per_month

    def load(self) -> None:
        """Load ledger from disk. Missing file = first-run; we seed with
        ``deployed_on=today`` and zero realised. Corrupt file = log and
        keep the in-memory copy empty (we'll just under-count for one
        cycle until the next write fixes it)."""
        if not self.enabled:
            return
        path = Path(self.ledger_path)
        if not path.exists():
            self._ledger = {
                "deployed_on": date.today().isoformat(),
                "cumulative_realised_inr": 0.0,
                "last_update": datetime.now(IST).isoformat(),
            }
            self._persist()
            return
        try:
            with path.open("r", encoding="utf-8") as f:
                self._ledger = json.load(f)
        except Exception as e:
            logger.warning(
                f"[SELF-SUFF] Ledger {self.ledger_path} is unreadable ({e}); "
                "starting empty for this cycle (next write will repair)."
            )
            self._ledger = {}

    def _persist(self) -> None:
        """Atomic write: temp file in same directory, then rename."""
        if not self.enabled:
            return
        path = Path(self.ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # NamedTemporaryFile in same directory so os.replace is atomic on Win
        # and POSIX. delete=False because we hand control to os.replace().
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=".self_suff_", suffix=".tmp", delete=False,
            ) as tf:
                json.dump(self._ledger, tf, indent=2)
                tmp_name = tf.name
            os.replace(tmp_name, path)
        except Exception as e:
            logger.warning(f"[SELF-SUFF] Failed to persist ledger: {e}")

    def record_realised_pnl(self, pnl_inr: float) -> None:
        """Apply a realised P&L delta to the ledger and persist atomically.

        ``pnl_inr`` should already be NET of commissions and taxes (i.e.
        what the broker actually credited or debited). The trading agent
        passes the same number it logs to ``trades.csv`` so the two
        sources are always reconcilable.
        """
        if not self.enabled:
            return
        cur = float(self._ledger.get("cumulative_realised_inr", 0.0) or 0.0)
        self._ledger["cumulative_realised_inr"] = cur + float(pnl_inr)
        self._ledger["last_update"] = datetime.now(IST).isoformat()
        self._persist()

    def days_since_deployment(self) -> int:
        if not self._ledger:
            return 0
        try:
            deployed = date.fromisoformat(self._ledger.get("deployed_on", ""))
        except Exception:
            return 0
        delta = (date.today() - deployed).days
        return max(0, delta)

    def status(self) -> SelfSufficiencyStatus:
        """Compute and return the current status snapshot."""
        if not self.enabled:
            return SelfSufficiencyStatus(
                state="UNKNOWN",
                cumulative_realised_inr=0.0,
                monthly_fixed_cost_inr=self.monthly_fixed_cost_inr,
                daily_breakeven_inr=self.daily_breakeven_inr,
                days_since_deployment=0,
                cost_burned_to_date_inr=0.0,
                coverage_pct=0.0,
                note="self_sufficiency tracker disabled",
            )
        cum = float(self._ledger.get("cumulative_realised_inr", 0.0) or 0.0)
        days = self.days_since_deployment()
        # Cost burn is straight-line: full monthly cost amortised across
        # `trading_days_per_month` business days.
        cost_to_date = self.daily_breakeven_inr * days
        coverage_pct = 0.0
        if cost_to_date > 0:
            # Coverage is signed -- a negative coverage_pct means we're
            # not just behind on cost, we're actively bleeding capital.
            coverage_pct = (cum / cost_to_date) * 100
        if cum >= 0:
            state = "GREEN"
            note = f"profitable (covers {coverage_pct:.0f}% of cost-to-date)"
        elif cum > -self.red_floor_inr:
            state = "YELLOW"
            note = (
                f"behind cost: -Rs{abs(cum):,.0f} cumulative vs "
                f"Rs{cost_to_date:,.0f} cost-to-date"
            )
        else:
            state = "RED"
            note = (
                f"BLEEDING: -Rs{abs(cum):,.0f} cumulative breaches red_floor "
                f"Rs{self.red_floor_inr:,.0f}; consider stopping LIVE entries"
            )
        return SelfSufficiencyStatus(
            state=state,
            cumulative_realised_inr=cum,
            monthly_fixed_cost_inr=self.monthly_fixed_cost_inr,
            daily_breakeven_inr=self.daily_breakeven_inr,
            days_since_deployment=days,
            cost_burned_to_date_inr=cost_to_date,
            coverage_pct=coverage_pct,
            note=note,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialised status for the audit checkpoint JSON output."""
        s = self.status()
        return {
            "enabled": self.enabled,
            "state": s.state,
            "cumulative_realised_inr": round(s.cumulative_realised_inr, 2),
            "monthly_fixed_cost_inr": round(s.monthly_fixed_cost_inr, 2),
            "daily_breakeven_inr": round(s.daily_breakeven_inr, 2),
            "days_since_deployment": s.days_since_deployment,
            "cost_burned_to_date_inr": round(s.cost_burned_to_date_inr, 2),
            "coverage_pct": round(s.coverage_pct, 1),
            "note": s.note,
        }


__all__ = ["SelfSufficiencyTracker", "SelfSufficiencyStatus"]
