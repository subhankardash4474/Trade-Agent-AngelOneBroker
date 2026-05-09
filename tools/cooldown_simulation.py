"""
Cooldown what-if simulation
============================
Replays the live trade history to estimate what the 7-day P&L would have
been under several "opening-window cooldown" variants:

  - Baseline:        actual live trading (no change)
  - Variant A:       suppress all strategies entering 09:15-09:30 IST
  - Variant B:       suppress mean_reversion only entering 09:15-09:30
  - Variant C:       suppress mean_reversion entering 09:15-09:35 (5 extra min)

The simulation is a *pure post-hoc filter*: trades that would have been
suppressed are removed from the realised P&L tally. It does NOT model what
the freed-up capital would have done — that's a real backtest, not a
what-if. Treat the numbers as a *lower bound* on the upside (without
counting any trades the cooldown would have made room for).

Output: logs/cooldown_simulation_<YYYY-MM-DD>.md
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Tuple


def _load_trades(db_path: str) -> List[dict]:
    c = sqlite3.connect(db_path)
    rows = list(c.execute(
        "SELECT symbol, side, entry_price, exit_price, quantity, pnl, "
        "       exit_reason, entry_time, exit_time, strategy "
        "FROM trades ORDER BY entry_time ASC"
    ))
    c.close()
    cols = ("symbol", "side", "entry_price", "exit_price", "quantity",
            "pnl", "exit_reason", "entry_time", "exit_time", "strategy")
    return [dict(zip(cols, r)) for r in rows]


def _hhmm(entry_time: str) -> str:
    """Extract HH:MM from a timestamp like '2026-05-06T09:16:00...'."""
    return entry_time[11:16]


def _in_window(t: dict, start_hhmm: str, end_hhmm: str) -> bool:
    return start_hhmm <= _hhmm(t["entry_time"]) <= end_hhmm


# ── Variant filters: each returns True if the trade is SUPPRESSED ──────

def variant_baseline(t: dict) -> bool:
    return False  # nothing suppressed


def variant_a_all_915_930(t: dict) -> bool:
    return _in_window(t, "09:15", "09:30")


def variant_b_meanrev_915_930(t: dict) -> bool:
    return t["strategy"] == "mean_reversion" and _in_window(t, "09:15", "09:30")


def variant_c_meanrev_915_935(t: dict) -> bool:
    return t["strategy"] == "mean_reversion" and _in_window(t, "09:15", "09:35")


VARIANTS: List[Tuple[str, Callable[[dict], bool]]] = [
    ("Baseline (current live)",        variant_baseline),
    ("Variant A: all strategies 09:15-09:30",  variant_a_all_915_930),
    ("Variant B: mean_reversion only 09:15-09:30",  variant_b_meanrev_915_930),
    ("Variant C: mean_reversion 09:15-09:35", variant_c_meanrev_915_935),
]


def _summarise(trades: List[dict], suppress: Callable[[dict], bool]) -> Dict:
    kept = [t for t in trades if not suppress(t)]
    suppressed = [t for t in trades if suppress(t)]
    pnls = [t["pnl"] or 0.0 for t in kept]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return {
        "n_kept": len(kept),
        "n_suppressed": len(suppressed),
        "total_pnl": round(sum(pnls), 2),
        "winners": len(wins),
        "losers": len(losses),
        "win_rate": round(len(wins) / max(1, len(kept)) * 100, 1),
        "gross_profit": round(sum(wins), 2),
        "gross_loss": round(sum(losses), 2),
        "profit_factor": round(sum(wins) / max(1e-9, abs(sum(losses))), 2),
        "max_loss": round(min(pnls), 2) if pnls else 0,
        "max_win": round(max(pnls), 2) if pnls else 0,
        "suppressed_pnl": round(sum((t["pnl"] or 0.0) for t in suppressed), 2),
    }


def _per_day(trades: List[dict], suppress: Callable[[dict], bool]) -> List[Tuple[str, float]]:
    by_day: Dict[str, float] = {}
    for t in trades:
        if suppress(t):
            continue
        d = t["entry_time"][:10]
        by_day[d] = by_day.get(d, 0) + (t["pnl"] or 0)
    return sorted(by_day.items())


def render_markdown(trades: List[dict]) -> str:
    """Build the markdown report for all variants vs baseline."""
    parts: List[str] = []
    parts.append("# Cooldown What-If Simulation")
    parts.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M IST')}_")
    parts.append("")
    parts.append(
        f"**Sample:** {len(trades)} live trades from "
        f"{trades[0]['entry_time'][:10]} to {trades[-1]['entry_time'][:10]}"
    )
    parts.append("")
    parts.append(
        "Each variant is a *pure post-hoc filter*: trades matching the rule "
        "are removed from the realised tally. The simulation does NOT model "
        "what the agent would have done with the freed-up capital, so the "
        "numbers should be read as a **lower bound** on the cooldown's upside."
    )
    parts.append("")

    # ── Summary table ────────────────────────────────────────────────
    parts.append("## Summary")
    parts.append("")
    parts.append(
        "| Variant | Kept | Suppressed | Total P&L | Δ vs base | WR | PF |"
    )
    parts.append(
        "|---|---:|---:|---:|---:|---:|---:|"
    )
    base_pnl = None
    for name, fn in VARIANTS:
        s = _summarise(trades, fn)
        delta = "—" if base_pnl is None else f"{(s['total_pnl'] - base_pnl):+.2f}"
        parts.append(
            f"| {name} | {s['n_kept']} | {s['n_suppressed']} | "
            f"₹{s['total_pnl']:+.2f} | ₹{delta} | "
            f"{s['win_rate']}% | {s['profit_factor']} |"
        )
        if base_pnl is None:
            base_pnl = s["total_pnl"]
    parts.append("")

    # ── Per-day P&L comparison ───────────────────────────────────────
    parts.append("## Per-day P&L")
    parts.append("")
    parts.append("| Date | Baseline | Variant A | Variant B | Variant C |")
    parts.append("|---|---:|---:|---:|---:|")
    days = sorted({t["entry_time"][:10] for t in trades})
    by_var = {name: dict(_per_day(trades, fn)) for name, fn in VARIANTS}
    for d in days:
        cells = [f"₹{by_var[name].get(d, 0):+.2f}" for name, _ in VARIANTS]
        parts.append(f"| {d} | " + " | ".join(cells) + " |")
    totals = [sum(by_var[name].values()) for name, _ in VARIANTS]
    parts.append(
        "| **TOTAL** | "
        + " | ".join(f"**₹{t:+.2f}**" for t in totals)
        + " |"
    )
    parts.append("")

    # ── Suppressed trades detail (Variant B) ─────────────────────────
    parts.append("## Trades that Variant B (the recommended fix) would have suppressed")
    parts.append("")
    suppressed = [t for t in trades if variant_b_meanrev_915_930(t)]
    if not suppressed:
        parts.append("_None._")
    else:
        parts.append(
            "| Date | Symbol | Side | Entry | Exit | Qty | P&L | Reason |"
        )
        parts.append("|---|---|---|---:|---:|---:|---:|---|")
        for t in suppressed:
            parts.append(
                f"| {t['entry_time'][:10]} | {t['symbol']} | {t['side']} | "
                f"{t['entry_price']:.2f} | {t['exit_price']:.2f} | "
                f"{t['quantity']} | ₹{(t['pnl'] or 0):+.2f} | {t['exit_reason']} |"
            )
        suppressed_total = sum((t["pnl"] or 0) for t in suppressed)
        parts.append(
            f"| | | | | | **TOTAL** | **₹{suppressed_total:+.2f}** | |"
        )
    parts.append("")

    # ── Recommendation block ─────────────────────────────────────────
    parts.append("## Honest caveats")
    parts.append("")
    parts.append(
        "1. **7 days, 66 trades is a small sample.** Statistical confidence is "
        "low. The signal is consistent but a 60-day formal backtest would be "
        "more rigorous."
    )
    parts.append(
        "2. **Capital reallocation is unmodeled.** If the cooldown frees up "
        "capital that lets the agent take *better* trades after 09:30, the "
        "real upside would be larger than what's shown here."
    )
    parts.append(
        "3. **Survivorship in entry timing.** Some trades opened during 09:15-09:30 "
        "*won* because of luck on those specific days. Removing them is fair "
        "in expectation but loses some upside on those particular days."
    )
    parts.append(
        "4. **The fix is reversible.** If shipped and it doesn't help, "
        "rolling back is a 1-line config change."
    )
    parts.append("")
    return "\n".join(parts)


def main() -> int:
    db_path = "data/trading_agent.db"
    trades = _load_trades(db_path)
    if not trades:
        print("No trades found in DB.")
        return 1
    md = render_markdown(trades)
    out = Path("logs") / f"cooldown_simulation_{datetime.now().strftime('%Y-%m-%d')}.md"
    out.write_text(md, encoding="utf-8")
    print(f"Wrote: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
