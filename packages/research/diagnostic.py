"""
Profit Diagnostic — empirical edge analysis from real DB trades.
================================================================
Pulls every closed trade from `data/trading_agent.db` and asks the
question that matters most: WHICH STRATEGIES HAVE POSITIVE EDGE
AFTER REAL CHARGES, AND WHICH ARE BLEEDING US?

Computes the four things that actually drive profitability:
  1. Profit Factor (gross_wins / gross_losses)        → > 1.0 = edge
  2. Win Rate vs breakeven WR                         → spread = edge in %pts
  3. Expectancy per trade (Rs)                        → > 0 = edge
  4. Kelly fraction = (p·b - q)/b                     → > 0 = bet, < 0 = don't bet

Also slices by:
  - exit_reason (signal vs sl vs tp vs trailing vs peak_giveback vs time)
  - regime
  - hour-of-day
  - side (LONG vs SHORT)
  - last 7d / last 30d / all-time

Outputs:
  - Console table (per-strategy verdict)
  - logs/diagnostics/profit_diagnostic_YYYYMMDD_HHMMSS.md (full report)
  - JSON dump for scripted use

Usage:
  python tools/profit_diagnostic.py                  # all-time
  python tools/profit_diagnostic.py --days 30        # last 30 days
  python tools/profit_diagnostic.py --strategy mean_reversion  # one strategy
  python tools/profit_diagnostic.py --json out.json  # machine-readable

The output is the basis for a "kill / keep / scale" decision.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Phase 1 layout: this file lives at packages/research/diagnostic.py
#   parents[1] = packages/        (sys.path bootstrap so `core`, `strategies` resolve)
#   parents[2] = project root     (where data/, logs/, config.yaml actually live)
PKG_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PKG_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DB_PATH = PROJECT_ROOT / "data" / "trading_agent.db"
OUT_DIR = PROJECT_ROOT / "logs" / "diagnostics"
OUT_DIR.mkdir(parents=True, exist_ok=True)
# Back-compat alias: a few helpers downstream reference ROOT for relative
# path display in the saved markdown report header. Point it at the
# project root (the user-visible workspace), not packages/.
ROOT = PROJECT_ROOT


# ─────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────
@dataclass
class StratStats:
    strategy: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    gross_win_pnl: float = 0.0
    gross_loss_pnl: float = 0.0  # stored positive (absolute value of losses)
    total_pnl: float = 0.0
    total_commission: float = 0.0
    total_notional: float = 0.0
    holding_minutes_sum: float = 0.0
    longs: int = 0
    shorts: int = 0
    long_pnl: float = 0.0
    short_pnl: float = 0.0
    by_exit_reason: dict = field(default_factory=lambda: defaultdict(lambda: {"n": 0, "pnl": 0.0}))
    by_regime: dict = field(default_factory=lambda: defaultdict(lambda: {"n": 0, "pnl": 0.0}))
    by_hour: dict = field(default_factory=lambda: defaultdict(lambda: {"n": 0, "pnl": 0.0}))
    pnl_series: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades) if self.trades else 0.0

    @property
    def avg_win(self) -> float:
        return (self.gross_win_pnl / self.wins) if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return (self.gross_loss_pnl / self.losses) if self.losses else 0.0

    @property
    def rr_ratio(self) -> float:
        """Reward:Risk = avg_win / avg_loss. Higher is better."""
        return (self.avg_win / self.avg_loss) if self.avg_loss > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        return (self.gross_win_pnl / self.gross_loss_pnl) if self.gross_loss_pnl > 0 else (
            float("inf") if self.gross_win_pnl > 0 else 0.0
        )

    @property
    def expectancy(self) -> float:
        return (self.total_pnl / self.trades) if self.trades else 0.0

    @property
    def breakeven_wr(self) -> float:
        """WR needed to break even given current R:R: 1/(1+b)."""
        return 1.0 / (1.0 + self.rr_ratio) if self.rr_ratio > 0 else 1.0

    @property
    def edge_pct_pts(self) -> float:
        """Win-rate edge in percentage points: actual − breakeven."""
        return (self.win_rate - self.breakeven_wr) * 100

    @property
    def kelly_fraction(self) -> float:
        """Kelly fraction f* = (p·b − q)/b. Positive = positive edge."""
        if self.rr_ratio <= 0:
            return -1.0
        p = self.win_rate
        q = 1.0 - p
        b = self.rr_ratio
        return (p * b - q) / b

    @property
    def avg_holding_min(self) -> float:
        return (self.holding_minutes_sum / self.trades) if self.trades else 0.0

    @property
    def commission_pct_of_notional(self) -> float:
        return (self.total_commission / self.total_notional * 100) if self.total_notional else 0.0

    @property
    def verdict(self) -> str:
        """Decision rule: KILL / WATCH / KEEP / SCALE based on stat-significance + edge."""
        if self.trades < 10:
            return "INSUFFICIENT_DATA"
        if self.profit_factor < 0.85 and self.kelly_fraction < -0.10:
            return "KILL"
        if self.profit_factor < 1.0:
            return "WATCH"
        if self.kelly_fraction > 0.10 and self.profit_factor > 1.3:
            return "SCALE"
        return "KEEP"


# ─────────────────────────────────────────────────────────────────────
# Statistical-significance helpers (2026-05-19, freeze-v2.1 observability)
# ─────────────────────────────────────────────────────────────────────
# These are observability additions in service of the
# `freeze_contingencies.md` §C2 "statistical artefacts that look like
# signal" failure mode. None of them change trade selection or behaviour
# -- they only add columns / sections to the diagnostic report so the
# operator can spot the distortion patterns the external verdict flagged:
# one-lucky-trade PF, sector concentration, time-of-day clustering,
# and small-sample noise dressed up as edge.

import random as _random


def bootstrap_pf_ci(
    pnls: list[float],
    n_resamples: int = 1000,
    seed: int = 20260519,
    ci_percent: float = 95.0,
) -> tuple[float, float]:
    """Bootstrap 95 % CI for profit factor from a per-trade PnL list.

    Method: resample-with-replacement ``n_resamples`` times, compute PF
    on each resample, return the (lower, upper) percentile band.

    Why this matters
    ----------------
    A point-estimate PF on N=20 trades is statistically useless. A
    bootstrap CI tells you what you actually know: if the lower CI is
    above 1.0, the strategy is profitable with the stated confidence,
    *given the trade distribution we've observed so far*. If the lower
    CI is below 1.0, you cannot claim edge from this sample regardless
    of where the point estimate landed.

    The freeze exit-criteria (`FREEZE_v2.1_revision.md`) gate the edge
    claim on lower-CI > 1.0, not on point PF.

    Returns ``(lower, upper)``. Special cases:
      * 0 trades  -> (0.0, 0.0)
      * < 5 trades -> (0.0, inf) -- CI is uninformative, caller treats as INSUFFICIENT
      * gross losses == 0 across all resamples -> (inf, inf) -- caller renders as ">inf"
    """
    if not pnls:
        return (0.0, 0.0)
    n = len(pnls)
    if n < 5:
        # Bootstrapping on < 5 trades is not statistically meaningful;
        # explicitly signal that the CI is uninformative. Callers can
        # treat any lower_ci == 0.0 + upper_ci == inf as INSUFFICIENT.
        return (0.0, float("inf"))

    rng = _random.Random(seed)
    pfs: list[float] = []
    for _ in range(n_resamples):
        sample = [pnls[rng.randint(0, n - 1)] for _ in range(n)]
        gw = sum(p for p in sample if p > 0)
        gl = -sum(p for p in sample if p < 0)
        if gl > 0:
            pfs.append(gw / gl)
        elif gw > 0:
            pfs.append(float("inf"))
        else:
            pfs.append(0.0)

    finite_pfs = [p for p in pfs if p != float("inf")]
    if not finite_pfs:
        return (float("inf"), float("inf"))

    finite_pfs.sort()
    alpha = (100.0 - ci_percent) / 2.0 / 100.0
    lower_idx = max(0, int(len(finite_pfs) * alpha) - 1)
    upper_idx = min(len(finite_pfs) - 1, int(len(finite_pfs) * (1 - alpha)))

    # If a meaningful fraction of resamples had zero losses (PF=inf),
    # the upper CI is genuinely unbounded -- report inf rather than the
    # finite tail.
    inf_share = (len(pfs) - len(finite_pfs)) / len(pfs)
    upper = float("inf") if inf_share >= alpha else finite_pfs[upper_idx]
    return (finite_pfs[lower_idx], upper)


def pf_excluding_max_trade(pnls: list[float]) -> tuple[float, float]:
    """Profit factor with the single largest-PnL trade removed.

    Returns ``(pf_full, pf_excl_max)``. The delta between the two
    answers the question "is this strategy's edge one lucky trade?"

    Decision rule (`freeze_contingencies.md` §C2):
      PF > 1.0 AND PF-excl-max < 0.8  -->  verdict downgraded to INSUFFICIENT.
    """
    if not pnls:
        return (0.0, 0.0)
    gw_full = sum(p for p in pnls if p > 0)
    gl_full = -sum(p for p in pnls if p < 0)
    pf_full = (gw_full / gl_full) if gl_full > 0 else (float("inf") if gw_full > 0 else 0.0)

    if len(pnls) < 2:
        return (pf_full, pf_full)

    max_pnl = max(pnls)
    # Only meaningful to "exclude max" if max was actually a win
    if max_pnl <= 0:
        return (pf_full, pf_full)

    pnls_excl = list(pnls)
    pnls_excl.remove(max_pnl)
    gw = sum(p for p in pnls_excl if p > 0)
    gl = -sum(p for p in pnls_excl if p < 0)
    pf_excl = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    return (pf_full, pf_excl)


# Hour-of-day bucket boundaries for the entry-time histogram. These match
# the IST market segments most strategies treat as meaningfully different:
# opening burst (09:15-10:00), early morning (10-11), mid morning (11-12),
# midday (12-13), early afternoon (13-14), pre-close (14-15:30).
ENTRY_TIME_BUCKETS: list[tuple[str, int, int]] = [
    ("09:15-10:00",  9, 10),
    ("10:00-11:00", 10, 11),
    ("11:00-12:00", 11, 12),
    ("12:00-13:00", 12, 13),
    ("13:00-14:00", 13, 14),
    ("14:00-15:30", 14, 16),
]


def entry_time_histogram(stats_by_hour: dict) -> dict[str, dict]:
    """Bucket a strategy's ``by_hour`` map into the 6 IST market segments.

    Input is the existing ``StratStats.by_hour`` dict (hour-int -> {n, pnl}).
    Output is bucket-name -> {n, pnl}, suitable for table rendering.
    """
    out: dict[str, dict] = {label: {"n": 0, "pnl": 0.0} for label, _, _ in ENTRY_TIME_BUCKETS}
    for hr, b in stats_by_hour.items():
        try:
            hr_int = int(hr)
        except (TypeError, ValueError):
            continue
        for label, lo, hi in ENTRY_TIME_BUCKETS:
            if lo <= hr_int < hi:
                out[label]["n"] += int(b.get("n", 0))
                out[label]["pnl"] += float(b.get("pnl", 0.0))
                break
    return out


def load_contaminated_days(project_root: Path | None = None) -> set[str]:
    """Read ``logs/contaminated_days.csv`` and return the set of ISO dates.

    File format (CSV, header required):
      ``date,vix,nifty_pct,reason``
    The ``date`` column must be ``YYYY-MM-DD``. Any row whose ``date``
    parses is included; ``vix``/``nifty_pct``/``reason`` are informational
    (operator-set; the existence of the row is what excludes the day).

    Defined in `freeze_contingencies.md` §C8: any day with India VIX > 25
    OR |NIFTY %| > 2.5 is marked CONTAMINATED. Phase A edge decision uses
    the *exclusive* (contaminated-removed) PF.

    Returns an empty set if the file is missing or unreadable -- the
    diagnostic must keep working on a fresh checkout that has never
    declared a contaminated day.
    """
    root = project_root or PROJECT_ROOT
    path = root / "logs" / "contaminated_days.csv"
    if not path.exists():
        return set()
    out: set[str] = set()
    try:
        import csv as _csv
        with path.open(newline="", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                d = (row.get("date") or "").strip()
                if len(d) >= 10 and d[4] == "-" and d[7] == "-":
                    out.add(d[:10])
    except Exception as e:
        print(f"[WARN] contaminated_days.csv unreadable: {e}", file=sys.stderr)
    return out


def filter_trades_excluding_contaminated(
    trades: list[dict], contaminated: set[str]
) -> list[dict]:
    """Return only trades whose exit_time date is NOT in the contaminated set."""
    if not contaminated:
        return list(trades)
    out: list[dict] = []
    for t in trades:
        ts = t.get("exit_time") or t.get("entry_time") or ""
        d = str(ts)[:10] if ts else ""
        if d and d in contaminated:
            continue
        out.append(t)
    return out


# ─────────────────────────────────────────────────────────────────────
# Loaders
# ─────────────────────────────────────────────────────────────────────
def load_trades(db_path: Path, days: int | None = None,
                strategy_filter: str | None = None) -> list[dict]:
    """Pull closed trades from DB. Returns list of dicts."""
    if not db_path.exists():
        print(f"[ERROR] DB not found: {db_path}")
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    sql = "SELECT * FROM trades WHERE 1=1"
    params: list[Any] = []
    if days is not None:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        sql += " AND exit_time >= ?"
        params.append(cutoff)
    if strategy_filter:
        sql += " AND strategy = ?"
        params.append(strategy_filter)
    sql += " ORDER BY exit_time ASC"

    rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    conn.close()
    return rows


# ─────────────────────────────────────────────────────────────────────
# Analytics
# ─────────────────────────────────────────────────────────────────────
def aggregate(trades: list[dict]) -> dict[str, StratStats]:
    """Group trades by strategy and compute all stats."""
    stats: dict[str, StratStats] = {}
    for t in trades:
        s = (t.get("strategy") or "unknown").strip() or "unknown"
        if s not in stats:
            stats[s] = StratStats(strategy=s)
        st = stats[s]
        st.trades += 1
        pnl = float(t.get("pnl") or 0.0)
        st.total_pnl += pnl
        st.pnl_series.append(pnl)

        if pnl > 0.01:
            st.wins += 1
            st.gross_win_pnl += pnl
        elif pnl < -0.01:
            st.losses += 1
            st.gross_loss_pnl += abs(pnl)
        else:
            st.breakeven += 1

        st.total_commission += float(t.get("commission") or 0.0)
        ent_p = float(t.get("entry_price") or 0.0)
        qty = int(t.get("quantity") or 0)
        st.total_notional += ent_p * qty * 2  # round-trip notional

        st.holding_minutes_sum += float(t.get("holding_minutes") or 0.0)
        side = (t.get("side") or "").upper()
        if side == "BUY":
            st.longs += 1
            st.long_pnl += pnl
        elif side == "SELL":
            st.shorts += 1
            st.short_pnl += pnl

        # Slices
        reason = (t.get("exit_reason") or "unknown").strip() or "unknown"
        bucket = st.by_exit_reason[reason]
        bucket["n"] += 1
        bucket["pnl"] += pnl

        regime = (t.get("regime") or "unknown").strip() or "unknown"
        rb = st.by_regime[regime]
        rb["n"] += 1
        rb["pnl"] += pnl

        try:
            ts = t.get("entry_time") or t.get("exit_time")
            if ts:
                hr = datetime.fromisoformat(ts.split("+")[0].split(".")[0]).hour
                hb = st.by_hour[hr]
                hb["n"] += 1
                hb["pnl"] += pnl
        except Exception:
            pass
    return stats


def portfolio_summary(stats: dict[str, StratStats]) -> dict:
    """Aggregate across all strategies."""
    total_trades = sum(s.trades for s in stats.values())
    total_wins = sum(s.wins for s in stats.values())
    total_losses = sum(s.losses for s in stats.values())
    gross_win = sum(s.gross_win_pnl for s in stats.values())
    gross_loss = sum(s.gross_loss_pnl for s in stats.values())
    total_pnl = sum(s.total_pnl for s in stats.values())
    total_comm = sum(s.total_commission for s in stats.values())

    wr = (total_wins / total_trades) if total_trades else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    avg_w = (gross_win / total_wins) if total_wins else 0.0
    avg_l = (gross_loss / total_losses) if total_losses else 0.0
    rr = (avg_w / avg_l) if avg_l > 0 else 0.0
    be_wr = 1.0 / (1.0 + rr) if rr > 0 else 1.0
    expectancy = (total_pnl / total_trades) if total_trades else 0.0
    kelly = ((wr * rr - (1 - wr)) / rr) if rr > 0 else -1.0

    return {
        "trades": total_trades,
        "wins": total_wins,
        "losses": total_losses,
        "win_rate": wr,
        "profit_factor": pf,
        "avg_win": avg_w,
        "avg_loss": avg_l,
        "rr_ratio": rr,
        "breakeven_wr": be_wr,
        "edge_pct_pts": (wr - be_wr) * 100,
        "kelly": kelly,
        "expectancy": expectancy,
        "total_pnl": total_pnl,
        "total_commission": total_comm,
        "commission_drag_pct_pnl": (total_comm / abs(total_pnl) * 100) if total_pnl else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────
# Formatters
# ─────────────────────────────────────────────────────────────────────
def fmt_table(stats: dict[str, StratStats]) -> str:
    """One-line-per-strategy verdict table for stdout & markdown."""
    if not stats:
        return "(no trades)"
    rows = []
    rows.append("| Strategy | N | WR% | PF | R:R | BE WR% | Edge pp | Expectancy | Total PnL | Kelly | Verdict |")
    rows.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    ordered = sorted(stats.values(), key=lambda s: s.total_pnl, reverse=True)
    for s in ordered:
        rows.append(
            f"| {s.strategy} | {s.trades} | {s.win_rate*100:.1f} | {s.profit_factor:.2f} | "
            f"1:{s.rr_ratio:.2f} | {s.breakeven_wr*100:.1f} | {s.edge_pct_pts:+.1f} | "
            f"Rs {s.expectancy:+.2f} | Rs {s.total_pnl:+.0f} | {s.kelly_fraction:+.3f} | {s.verdict} |"
        )
    return "\n".join(rows)


def fmt_exit_reason_breakdown(stats: dict[str, StratStats]) -> str:
    rows = ["| Strategy | Exit Reason | N | Total PnL | Avg PnL |", "|---|---|---:|---:|---:|"]
    for s in sorted(stats.values(), key=lambda x: x.total_pnl, reverse=True):
        for reason, b in sorted(s.by_exit_reason.items(), key=lambda kv: kv[1]["pnl"], reverse=True):
            rows.append(f"| {s.strategy} | {reason} | {b['n']} | Rs {b['pnl']:+.0f} | "
                        f"Rs {b['pnl']/max(b['n'],1):+.2f} |")
    return "\n".join(rows)


def aggregate_by_supersector(trades: list[dict]) -> dict[str, dict]:
    """Group trades by supersector and compute PF/PnL per group.

    Detects the §C2 sector-concentration artefact -- the verdict's
    warning that "all winners in financials, all losers in IT" looks
    like edge but is a regime rotation.

    Returns supersector_name -> {n, wins, losses, gross_win, gross_loss,
    pnl, pf}. Trades without a resolvable supersector go into the
    "UNKNOWN" bucket so they're not silently lost.
    """
    try:
        from core.market_safety import get_supersector
    except Exception:
        # If the symbol-mapping module isn't importable from this
        # context (e.g. running diagnostic.py as a bare script outside
        # the agent root), degrade gracefully -- emit an UNKNOWN-only
        # bucket so the rest of the report still renders.
        def get_supersector(symbol: str) -> str:  # type: ignore[unused-ignore]
            return "UNKNOWN"

    buckets: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "wins": 0, "losses": 0,
        "gross_win": 0.0, "gross_loss": 0.0, "pnl": 0.0,
    })
    for t in trades:
        sym = (t.get("symbol") or "").upper().strip()
        if not sym:
            ss = "UNKNOWN"
        else:
            try:
                ss = get_supersector(sym) or "UNKNOWN"
            except Exception:
                ss = "UNKNOWN"
        pnl = float(t.get("pnl") or 0.0)
        b = buckets[ss]
        b["n"] += 1
        b["pnl"] += pnl
        if pnl > 0.01:
            b["wins"] += 1
            b["gross_win"] += pnl
        elif pnl < -0.01:
            b["losses"] += 1
            b["gross_loss"] += abs(pnl)

    for b in buckets.values():
        gw, gl = b["gross_win"], b["gross_loss"]
        b["pf"] = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    return dict(buckets)


def fmt_per_supersector(buckets: dict[str, dict]) -> str:
    """Render the per-supersector PF table."""
    if not buckets:
        return "_(no supersector data)_"
    rows = [
        "| Supersector | N | Wins | Losses | PF | PnL |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for ss in sorted(buckets.keys(), key=lambda k: -buckets[k]["pnl"]):
        b = buckets[ss]
        pf_disp = f"{b['pf']:.2f}" if b["pf"] != float("inf") else "INF"
        rows.append(
            f"| {ss} | {b['n']} | {b['wins']} | {b['losses']} | "
            f"{pf_disp} | Rs {b['pnl']:+,.0f} |"
        )
    return "\n".join(rows)


def fmt_pf_excl_max(stats: dict[str, StratStats]) -> str:
    """Render the PF / PF-excl-max-trade comparison table.

    Detects the §C2 "one lucky trade" pattern. A strategy with PF > 1.0
    but PF-excl-max < 0.8 is a single-trade illusion.
    """
    if not stats:
        return "_(no trades)_"
    rows = [
        "| Strategy | N | PF | PF-excl-max | Δ | One-lucky-trade? |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for s in sorted(stats.values(), key=lambda x: -x.total_pnl):
        if s.trades < 2:
            continue
        pf_full, pf_excl = pf_excluding_max_trade(s.pnl_series)
        delta = pf_full - pf_excl if (pf_full != float("inf") and pf_excl != float("inf")) else None
        is_lucky = pf_full > 1.0 and pf_excl < 0.8
        pf_full_d = f"{pf_full:.2f}" if pf_full != float("inf") else "INF"
        pf_excl_d = f"{pf_excl:.2f}" if pf_excl != float("inf") else "INF"
        delta_d = f"{delta:+.2f}" if delta is not None else "n/a"
        flag = "**YES**" if is_lucky else "no"
        rows.append(
            f"| {s.strategy} | {s.trades} | {pf_full_d} | {pf_excl_d} | "
            f"{delta_d} | {flag} |"
        )
    return "\n".join(rows)


def fmt_pf_ci(stats: dict[str, StratStats]) -> str:
    """Render the bootstrap PF lower-95-CI table.

    The freeze-revision exit criteria gate on lower-CI > 1.0, not on
    point PF. This is the small-sample-trap guardrail the May-12 PF=1.20
    reading would have failed.
    """
    if not stats:
        return "_(no trades)_"
    rows = [
        "| Strategy | N | PF (point) | PF lower-95-CI | PF upper-95-CI | Verdict |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for s in sorted(stats.values(), key=lambda x: -x.total_pnl):
        lo, hi = bootstrap_pf_ci(s.pnl_series)
        pf_point = s.profit_factor
        pf_point_d = f"{pf_point:.2f}" if pf_point != float("inf") else "INF"
        lo_d = f"{lo:.2f}" if lo not in (float("inf"), float("-inf")) else "INF"
        hi_d = f"{hi:.2f}" if hi != float("inf") else "INF"
        if s.trades < 5:
            ci_verdict = "INSUFFICIENT (N<5)"
        elif lo > 1.0:
            ci_verdict = "**EDGE confirmed** (lower-CI > 1.0)"
        elif hi < 1.0:
            ci_verdict = "**no edge** (upper-CI < 1.0)"
        else:
            ci_verdict = "inconclusive (CI straddles 1.0)"
        rows.append(
            f"| {s.strategy} | {s.trades} | {pf_point_d} | {lo_d} | {hi_d} | {ci_verdict} |"
        )
    return "\n".join(rows)


def fmt_entry_time_histogram(stats: dict[str, StratStats]) -> str:
    """Render the per-strategy entry-time histogram across 6 IST buckets.

    Detects the §C2 time-of-day clustering artefact. If > 70 % of a
    strategy's winning trades fire in a single 90-minute window, the
    "edge" is time-dependent.
    """
    if not stats:
        return "_(no trades)_"
    bucket_labels = [lbl for lbl, _, _ in ENTRY_TIME_BUCKETS]
    header = "| Strategy | " + " | ".join(bucket_labels) + " | Concentration |"
    align = "|---|" + "|".join(["---:"] * len(bucket_labels)) + "|---|"
    rows = [header, align]
    for s in sorted(stats.values(), key=lambda x: -x.total_pnl):
        hist = entry_time_histogram(s.by_hour)
        cells = [f"{hist[lbl]['n']}" for lbl in bucket_labels]
        # Concentration = share of trades in the most-populated bucket
        total = sum(hist[lbl]["n"] for lbl in bucket_labels)
        if total >= 5:
            max_share = max(hist[lbl]["n"] for lbl in bucket_labels) / total
            flag = (f"**{max_share*100:.0f}% in one bucket**"
                    if max_share >= 0.70 else f"{max_share*100:.0f}%")
        else:
            flag = "n/a (<5 trades)"
        rows.append(f"| {s.strategy} | " + " | ".join(cells) + f" | {flag} |")
    return "\n".join(rows)


def fmt_long_short(stats: dict[str, StratStats]) -> str:
    rows = ["| Strategy | Longs N | Long PnL | Shorts N | Short PnL |",
            "|---|---:|---:|---:|---:|"]
    for s in sorted(stats.values(), key=lambda x: x.total_pnl, reverse=True):
        rows.append(f"| {s.strategy} | {s.longs} | Rs {s.long_pnl:+.0f} | "
                    f"{s.shorts} | Rs {s.short_pnl:+.0f} |")
    return "\n".join(rows)


def make_recommendations(stats: dict[str, StratStats], port: dict) -> list[str]:
    """Convert verdicts to actionable config changes."""
    recs: list[str] = []
    kills = [s for s in stats.values() if s.verdict == "KILL"]
    scales = [s for s in stats.values() if s.verdict == "SCALE"]
    watches = [s for s in stats.values() if s.verdict == "WATCH"]
    insufficient = [s for s in stats.values() if s.verdict == "INSUFFICIENT_DATA"]

    if kills:
        recs.append(
            f"**KILL ({len(kills)}):** disable in `config.yaml` → "
            + ", ".join(f"`{s.strategy}` (PF {s.profit_factor:.2f}, Kelly {s.kelly_fraction:+.3f}, "
                        f"PnL Rs {s.total_pnl:+.0f})" for s in kills)
        )
    if scales:
        recs.append(
            f"**SCALE ({len(scales)}):** raise allocation / weight → "
            + ", ".join(f"`{s.strategy}` (PF {s.profit_factor:.2f}, Kelly {s.kelly_fraction:+.3f}, "
                        f"PnL Rs {s.total_pnl:+.0f})" for s in scales)
        )
    if watches:
        recs.append(
            f"**WATCH ({len(watches)}):** marginal — keep but monitor closely → "
            + ", ".join(f"`{s.strategy}` (PF {s.profit_factor:.2f})" for s in watches)
        )
    if insufficient:
        recs.append(
            f"**INSUFFICIENT_DATA ({len(insufficient)}):** need more trades to decide → "
            + ", ".join(f"`{s.strategy}` (n={s.trades})" for s in insufficient)
        )
    # Long vs short asymmetry
    for s in stats.values():
        if s.longs >= 5 and s.shorts >= 5:
            l_pnl_per = s.long_pnl / max(s.longs, 1)
            sh_pnl_per = s.short_pnl / max(s.shorts, 1)
            if abs(l_pnl_per - sh_pnl_per) > 30 and (l_pnl_per * sh_pnl_per) < 0:
                better = "LONG" if l_pnl_per > sh_pnl_per else "SHORT"
                worse = "SHORT" if better == "LONG" else "LONG"
                recs.append(
                    f"`{s.strategy}` — strong asymmetry: {better} avg Rs {max(l_pnl_per, sh_pnl_per):+.1f}/trade, "
                    f"{worse} avg Rs {min(l_pnl_per, sh_pnl_per):+.1f}/trade. "
                    f"Consider disabling {worse}-side."
                )
    # Portfolio-level
    if port["kelly"] < 0:
        recs.append(
            f"**PORTFOLIO Kelly is NEGATIVE ({port['kelly']:+.3f}).** "
            f"Mathematically the agent should not be betting at current WR ({port['win_rate']*100:.1f}%) "
            f"and R:R (1:{port['rr_ratio']:.2f}). Need to either kill bleeding strategies or "
            f"raise R:R via wider TPs / tighter SLs."
        )
    if port["commission_drag_pct_pnl"] > 50:
        recs.append(
            f"Commission drag is **{port['commission_drag_pct_pnl']:.0f}% of |PnL|** — "
            f"trades are too small or too frequent. Raise `min_trade_notional` or "
            f"`min_profit_to_charges_ratio`."
        )
    return recs


# ─────────────────────────────────────────────────────────────────────
# Report builder
# ─────────────────────────────────────────────────────────────────────
def build_phase_a_rolling_section(
    db_path: Path,
    window_days: int,
    target_pf: float,
    floor_pf: float,
) -> str:
    """Phase-A validation tracker: PF day-by-day over the last N TRADING days.

    "Trading days" = distinct ISO-date values in the trades table (so weekends
    and holidays naturally fall out, no calendar gymnastics required).

    Outputs a per-day table + a rolling-PF verdict line designed to make the
    Friday EOD email's pass/fail call mechanical:

      PASS:     rolling PF >= target_pf
      INCONCLUSIVE: floor_pf <= rolling PF < target_pf  (extend Phase A by 1 wk)
      FAIL:     rolling PF < floor_pf                   (stop, run postmortem)

    The thresholds are passed in (default 1.5 / 1.0) so the same function is
    reusable for a Phase-B re-check with stricter gates later.
    """
    if not db_path.exists():
        return ""

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # date(exit_time) gives ISO yyyy-mm-dd; SQLite handles this natively for
    # ISO-format timestamps. Group by day, aggregate per-day stats.
    # NOTE: strip the +05:30 offset before passing to date() -- SQLite
    # converts IST timestamps to UTC otherwise, which is harmless during
    # market hours (09:15-15:30 IST never crosses UTC midnight) but would
    # silently bucket a 23:55 IST audit-time exit into the next day.
    # substr(1, 10) keeps just "YYYY-MM-DD" which is what we want.
    rows = conn.execute("""
        SELECT substr(exit_time, 1, 10) AS day,
               COUNT(*) AS trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_win,
               SUM(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END) AS gross_loss,
               SUM(pnl) AS net_pnl
        FROM trades
        WHERE exit_time IS NOT NULL
        GROUP BY day
        ORDER BY day DESC
        LIMIT ?
    """, (window_days,)).fetchall()
    conn.close()

    if not rows:
        return "\n## Phase A — Rolling validation tracker\n\n_No closed trades yet to validate._\n"

    # Reverse to chronological order for the table (oldest first), keeps the
    # eye trained on trend rather than recency bias.
    day_rows = list(reversed(rows))

    lines = [
        "",
        f"## Phase A — Rolling {window_days}-day validation tracker",
        "",
        f"_Phase-A gate: rolling PF >= **{target_pf:.2f}** = PASS, "
        f">= **{floor_pf:.2f}** = INCONCLUSIVE (extend), "
        f"< {floor_pf:.2f} = FAIL (stop + postmortem)._",
        "",
        "| Day | Trades | Wins | Gross Win | Gross Loss | Day PF | Day PnL |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    total_gw = 0.0
    total_gl = 0.0
    total_pnl = 0.0
    total_trades = 0
    for r in day_rows:
        gw = float(r["gross_win"] or 0.0)
        gl = float(r["gross_loss"] or 0.0)
        net = float(r["net_pnl"] or 0.0)
        day_pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
        day_pf_disp = f"{day_pf:.2f}" if day_pf != float("inf") else "INF"
        lines.append(
            f"| {r['day']} | {r['trades']} | {r['wins']} | "
            f"Rs {gw:,.0f} | Rs {gl:,.0f} | {day_pf_disp} | Rs {net:+,.0f} |"
        )
        total_gw += gw
        total_gl += gl
        total_pnl += net
        total_trades += int(r["trades"] or 0)

    rolling_pf = (total_gw / total_gl) if total_gl > 0 else (
        float("inf") if total_gw > 0 else 0.0
    )
    rolling_pf_disp = f"{rolling_pf:.2f}" if rolling_pf != float("inf") else "INF"

    if rolling_pf >= target_pf:
        verdict = "**PASS**"
        action = (f"rolling PF {rolling_pf_disp} >= target {target_pf:.2f}. "
                  f"Phase A confirmed -- proceed to Phase B (hourly blackouts "
                  f"+ quarter-Kelly sizing).")
    elif rolling_pf >= floor_pf:
        verdict = "**INCONCLUSIVE**"
        action = (f"rolling PF {rolling_pf_disp} is between floor ({floor_pf:.2f}) "
                  f"and target ({target_pf:.2f}). Extend Phase A by another "
                  f"{window_days} trading days before deciding.")
    else:
        verdict = "**FAIL**"
        action = (f"rolling PF {rolling_pf_disp} < floor {floor_pf:.2f}. STOP. "
                  f"Run profit_diagnostic.py --days {window_days * 2} for a "
                  f"full postmortem before making any further changes.")

    lines.extend([
        "",
        f"**Window totals:** {len(day_rows)} day(s) | "
        f"{total_trades} trades | "
        f"PnL Rs {total_pnl:+,.0f} | "
        f"Rolling PF: **{rolling_pf_disp}**",
        "",
        f"### Verdict: {verdict}",
        "",
        action,
        "",
    ])
    return "\n".join(lines)


def build_report(trades: list[dict], stats: dict[str, StratStats],
                 port: dict, args: argparse.Namespace) -> str:
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    period = f"last {args.days}d" if args.days else "all-time"

    # Contaminated-days inclusive/exclusive PF (freeze-v2.1 §C8).
    # We compute both numbers up-front so the report can lead with the
    # comparison rather than scattering it through later sections.
    contaminated = load_contaminated_days()
    clean_trades = filter_trades_excluding_contaminated(trades, contaminated)
    excluded_n = len(trades) - len(clean_trades)
    clean_stats = aggregate(clean_trades) if excluded_n else stats
    clean_port = portfolio_summary(clean_stats) if excluded_n else port

    # Per-supersector aggregation (freeze-v2.1 §C2).
    supersector_buckets = aggregate_by_supersector(trades)

    lines = [
        f"# Profit Diagnostic — {period}",
        f"_Generated {when}_  |  DB: `{DB_PATH.name}`  |  Trades analyzed: **{len(trades)}**",
        "",
        "## TL;DR (portfolio level)",
        "",
        f"- **Total PnL (net of charges):** Rs {port['total_pnl']:+.0f}",
        f"- **Trades:** {port['trades']}  |  Wins: {port['wins']}  |  Losses: {port['losses']}",
        f"- **Win Rate:** {port['win_rate']*100:.1f}%  |  Breakeven WR: {port['breakeven_wr']*100:.1f}%  |  "
        f"**Edge: {port['edge_pct_pts']:+.1f} pp**",
        f"- **Profit Factor:** {port['profit_factor']:.2f}  (>1 = profitable, <1 = bleeding)",
        f"- **R:R:** 1:{port['rr_ratio']:.2f}  |  Expectancy: Rs {port['expectancy']:+.2f}/trade",
        f"- **Kelly fraction:** {port['kelly']:+.3f}  ({'POSITIVE EDGE' if port['kelly'] > 0 else 'NEGATIVE -- math says do not bet'})",
        f"- **Commission paid:** Rs {port['total_commission']:.0f}  "
        f"(= {port['commission_drag_pct_pnl']:.0f}% of |PnL|)",
        "",
        # Contaminated-days inclusive/exclusive comparison (§C8). Only
        # rendered when at least one day has been declared contaminated;
        # otherwise the line would be noise. The edge-claim should use
        # the EXCLUSIVE PF per the freeze contract.
        *(
            [
                f"### Contaminated-days adjustment ({excluded_n} trade(s) on {len(contaminated)} day(s) excluded)",
                "",
                "| Window | Trades | PnL | PF | WR % | Kelly |",
                "|---|---:|---:|---:|---:|---:|",
                f"| Inclusive (all days)   | {port['trades']}       | Rs {port['total_pnl']:+,.0f}       | {port['profit_factor']:.2f}       | {port['win_rate']*100:.1f}       | {port['kelly']:+.3f}       |",
                f"| Exclusive (clean days) | {clean_port['trades']} | Rs {clean_port['total_pnl']:+,.0f} | {clean_port['profit_factor']:.2f} | {clean_port['win_rate']*100:.1f} | {clean_port['kelly']:+.3f} |",
                "",
                "_Edge claim uses **Exclusive**; risk-tolerance check uses Inclusive. Contamination is pre-defined as VIX > 25 OR |NIFTY %| > 2.5 (see `docs/freeze_contingencies.md` §C8 / `logs/contaminated_days.csv`)._",
                "",
            ] if excluded_n > 0 else []
        ),
        # Phase-A rolling tracker -- only meaningful if the caller asked for
        # a recent window (otherwise it duplicates the TL;DR). Always emit
        # for short-window reports (days <= 30) so the daily EOD email leads
        # with a clear pass/fail call.
        (build_phase_a_rolling_section(
            DB_PATH,
            window_days=getattr(args, "phase_a_window", 5),
            target_pf=getattr(args, "phase_a_target_pf", 1.5),
            floor_pf=getattr(args, "phase_a_floor_pf", 1.0),
         ) if (args.days is None or args.days <= 30) else ""),
        "## Per-strategy verdict",
        "",
        fmt_table(stats),
        "",
        "**Verdict legend:**",
        "- `SCALE` -- strong edge, raise allocation",
        "- `KEEP` -- net positive, hold steady",
        "- `WATCH` -- marginal, keep monitoring",
        "- `KILL` -- bleeding, disable in config",
        "- `INSUFFICIENT_DATA` -- need >=10 trades to decide",
        "",
        "## Statistical-significance check (freeze-v2.1 §C2)",
        "",
        "_The point-PF is uninformative on N<30. The bootstrap lower-CI is what gates an honest verdict. The freeze-v2.1 revision exit-criteria require **lower-CI > 1.0** to claim edge, not just point-PF > 1.0._",
        "",
        fmt_pf_ci(stats),
        "",
        "### PF excluding maximum-PnL trade",
        "",
        "_Detects \"edge is one lucky trade\" pattern. If PF > 1.0 AND PF-excl-max < 0.8, the verdict should be downgraded to INSUFFICIENT regardless of point PF._",
        "",
        fmt_pf_excl_max(stats),
        "",
        "### Entry-time histogram (IST buckets)",
        "",
        "_Detects time-of-day clustering. If > 70 % of a strategy's trades fire in one 90-minute window, the apparent edge is time-dependent._",
        "",
        fmt_entry_time_histogram(stats),
        "",
        "## Per-supersector PF",
        "",
        "_Detects sector-concentration artefacts. If PF varies by > 50 % across supersectors with N >= 5 each, the edge is sector-dependent (not generalisable)._",
        "",
        fmt_per_supersector(supersector_buckets),
        "",
        "## Long vs Short asymmetry",
        "",
        fmt_long_short(stats),
        "",
        "## Exit-reason breakdown (where PnL is leaking or earned)",
        "",
        fmt_exit_reason_breakdown(stats),
        "",
        "## Recommendations",
        "",
    ]
    recs = make_recommendations(stats, port)
    if recs:
        for r in recs:
            lines.append(f"- {r}")
    else:
        lines.append("_No actionable recommendations -- agent is in a stable, profitable regime._")
    lines.append("")

    # ── What-if: project portfolio metrics if we KILL all KILL-verdict strategies
    kills = [s for s in stats.values() if s.verdict == "KILL"]
    if kills:
        lines.append("## What-if: KILL all bleeding strategies")
        lines.append("")
        lines.append("If every `KILL`-verdict strategy were disabled retroactively:")
        lines.append("")
        kept_trades = sum(s.trades for s in stats.values() if s.verdict != "KILL")
        kept_pnl = sum(s.total_pnl for s in stats.values() if s.verdict != "KILL")
        kept_wins = sum(s.wins for s in stats.values() if s.verdict != "KILL")
        kept_losses = sum(s.losses for s in stats.values() if s.verdict != "KILL")
        kept_gw = sum(s.gross_win_pnl for s in stats.values() if s.verdict != "KILL")
        kept_gl = sum(s.gross_loss_pnl for s in stats.values() if s.verdict != "KILL")
        new_wr = (kept_wins / kept_trades) if kept_trades else 0.0
        new_pf = (kept_gw / kept_gl) if kept_gl > 0 else (float("inf") if kept_gw > 0 else 0.0)
        new_avg_w = (kept_gw / kept_wins) if kept_wins else 0.0
        new_avg_l = (kept_gl / kept_losses) if kept_losses else 0.0
        new_rr = (new_avg_w / new_avg_l) if new_avg_l > 0 else 0.0
        new_be_wr = 1.0 / (1.0 + new_rr) if new_rr > 0 else 1.0
        new_kelly = ((new_wr * new_rr - (1 - new_wr)) / new_rr) if new_rr > 0 else -1.0
        new_exp = (kept_pnl / kept_trades) if kept_trades else 0.0
        delta_pnl = kept_pnl - port["total_pnl"]
        kill_names = ", ".join(f"`{s.strategy}`" for s in kills)
        exp_factor_line = ""
        if port["expectancy"] != 0:
            mult = new_exp / port["expectancy"]
            exp_factor_line = f"  (**{mult:.1f}x improvement**)"
        lines.append(f"- **Disabled:** {kill_names}")
        lines.append(f"- **Trades remaining:** {kept_trades} (was {port['trades']}, "
                     f"-{port['trades']-kept_trades} trades cut)")
        lines.append(f"- **Total PnL:** Rs {kept_pnl:+.0f}  (was Rs {port['total_pnl']:+.0f}, "
                     f"**delta Rs {delta_pnl:+.0f}**)")
        lines.append(f"- **Win Rate:** {new_wr*100:.1f}% (was {port['win_rate']*100:.1f}%, "
                     f"{(new_wr-port['win_rate'])*100:+.1f} pp)")
        lines.append(f"- **Profit Factor:** {new_pf:.2f} (was {port['profit_factor']:.2f})")
        lines.append(f"- **R:R:** 1:{new_rr:.2f} (was 1:{port['rr_ratio']:.2f})")
        lines.append(f"- **Breakeven WR:** {new_be_wr*100:.1f}% -> Edge "
                     f"{(new_wr-new_be_wr)*100:+.1f} pp (was {port['edge_pct_pts']:+.1f} pp)")
        lines.append(f"- **Kelly fraction:** {new_kelly:+.3f} (was {port['kelly']:+.3f})")
        lines.append(f"- **Expectancy:** Rs {new_exp:+.2f}/trade (was Rs {port['expectancy']:+.2f}"
                     f"/trade){exp_factor_line}")
        lines.append("")
        lines.append("_Caveat: counterfactual on closed trades. In reality, killing a strategy "
                     "frees portfolio slots that other strategies may fill -- actual forward "
                     "performance may differ from this projection._")
        lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--days", type=int, default=None,
                    help="Look at last N days only (default: all-time).")
    ap.add_argument("--strategy", default=None,
                    help="Filter to one strategy.")
    ap.add_argument("--min-trades", type=int, default=1,
                    help="Hide strategies with fewer trades than this.")
    ap.add_argument("--json", default=None,
                    help="Path to also dump machine-readable JSON.")
    ap.add_argument("--out", default=None,
                    help="Override markdown output path.")
    ap.add_argument("--phase-a-window", type=int, default=5,
                    help="Phase-A rolling-validation window (number of "
                         "trading days to roll over). Default: 5 = one "
                         "full trading week.")
    ap.add_argument("--phase-a-target-pf", type=float, default=1.5,
                    help="Rolling PF threshold above which Phase A PASSES "
                         "(default 1.5). Above this, proceed to Phase B.")
    ap.add_argument("--phase-a-floor-pf", type=float, default=1.0,
                    help="Rolling PF threshold below which Phase A FAILS "
                         "(default 1.0). Between floor and target = "
                         "INCONCLUSIVE -- extend Phase A by another window.")
    args = ap.parse_args()

    trades = load_trades(DB_PATH, days=args.days, strategy_filter=args.strategy)
    if not trades:
        print(f"[NO DATA] No trades found in {DB_PATH} "
              f"{'(last ' + str(args.days) + 'd)' if args.days else ''}"
              f"{' for strategy=' + args.strategy if args.strategy else ''}.")
        return 1

    stats = aggregate(trades)
    stats = {k: v for k, v in stats.items() if v.trades >= args.min_trades}
    port = portfolio_summary(stats)

    report = build_report(trades, stats, port, args)
    print(report)

    out = Path(args.out) if args.out else (
        OUT_DIR / f"profit_diagnostic_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    )
    out.write_text(report, encoding="utf-8")
    print(f"\n[SAVED] {out.relative_to(ROOT)}")

    if args.json:
        # Build JSON-safe dict from StratStats (dataclass has nested defaultdicts)
        def _safe(s: StratStats) -> dict:
            d = asdict(s)
            d["by_exit_reason"] = dict(s.by_exit_reason)
            d["by_regime"] = dict(s.by_regime)
            d["by_hour"] = {str(k): v for k, v in s.by_hour.items()}
            d.pop("pnl_series", None)
            d.update({
                "win_rate": s.win_rate, "avg_win": s.avg_win, "avg_loss": s.avg_loss,
                "rr_ratio": s.rr_ratio, "profit_factor": s.profit_factor,
                "expectancy": s.expectancy, "breakeven_wr": s.breakeven_wr,
                "edge_pct_pts": s.edge_pct_pts, "kelly_fraction": s.kelly_fraction,
                "verdict": s.verdict,
            })
            return d
        Path(args.json).write_text(json.dumps({
            "portfolio": port,
            "strategies": {k: _safe(v) for k, v in stats.items()},
            "metadata": {
                "trades_analyzed": len(trades),
                "period_days": args.days,
                "generated_at": datetime.now().isoformat(),
            },
        }, indent=2), encoding="utf-8")
        print(f"[SAVED] {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
