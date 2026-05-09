"""
Daily Gap-Detector — automated end-of-day analysis.

Runs after market close (or on demand) and produces:
  - Quantitative breakdown: win rate, R:R, profit factor, expectancy
  - Exit-reason mix (SL dominance? too many signal exits?)
  - Time-of-day heatmap (which hours lose money consistently)
  - Symbol churn (repeated losers — blacklist candidates)
  - Strategy performance (which strategy is dragging)
  - Gate-efficacy stats (how many trades each safety gate rejected)
  - Actionable config suggestions with expected impact

Output:
  - logs/gap_report_YYYY-MM-DD.md    (human-readable report)
  - logs/config_suggestions.json     (machine-readable, idempotent)

Usage:
  python analyze_day.py                       # analyze today
  python analyze_day.py --date 2026-04-28     # analyze a past day
  python analyze_day.py --days 7              # rolling 7-day analysis
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DB_PATH = os.environ.get("AGENT_DB_PATH", "data/trading_agent.db")
LOG_DIR = "logs"

# Breakeven sensitivity: win rate needed at given R:R
# If rr = avg_win / avg_loss, breakeven_wr = 1 / (1 + rr)


# ─────────────────────────────────────────────────────────────
# Data loaders
# ─────────────────────────────────────────────────────────────


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def load_trades(start_iso: str, end_iso: str) -> List[dict]:
    with _conn() as c:
        rows = c.execute(
            """SELECT * FROM trades
               WHERE exit_time >= ? AND exit_time < ?
               ORDER BY exit_time""",
            (start_iso, end_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def load_orders(start_iso: str, end_iso: str) -> List[dict]:
    """Order ledger — reveals rejection reasons and partial fills."""
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT * FROM orders
                   WHERE timestamp >= ? AND timestamp < ?
                   ORDER BY timestamp""",
                (start_iso, end_iso),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        # Orders table may not exist in older DBs
        return []


def load_equity_curve(start_iso: str, end_iso: str) -> List[dict]:
    with _conn() as c:
        rows = c.execute(
            """SELECT timestamp, equity, cash, positions FROM equity_curve
               WHERE timestamp >= ? AND timestamp < ?
               ORDER BY timestamp""",
            (start_iso, end_iso),
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────────


@dataclass
class DayStats:
    n: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    win_rate: float = 0.0
    rr: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    breakeven_wr: float = 100.0
    total_charges: float = 0.0
    sharpe_daily: Optional[float] = None
    max_drawdown_pct: Optional[float] = None


def compute_stats(trades: List[dict]) -> DayStats:
    s = DayStats()
    if not trades:
        return s
    s.n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    s.wins = len(wins)
    s.losses = len(losses)
    s.total_pnl = sum(t["pnl"] for t in trades)
    s.total_charges = sum(t.get("commission", 0) or 0 for t in trades)
    s.win_rate = s.wins / s.n * 100 if s.n else 0
    s.avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    s.avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    s.rr = abs(s.avg_win / s.avg_loss) if s.avg_loss else 0
    pf_wins = sum(t["pnl"] for t in wins)
    pf_losses = abs(sum(t["pnl"] for t in losses)) or 1.0
    s.profit_factor = pf_wins / pf_losses
    s.expectancy = s.total_pnl / s.n
    s.breakeven_wr = (1.0 / (1.0 + s.rr) * 100) if s.rr else 100.0
    return s


def by_dimension(trades: List[dict], key_fn) -> Dict[str, Dict[str, float]]:
    """Group trades by some key and compute count/wins/pnl per bucket."""
    buckets: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0}
    )
    for t in trades:
        try:
            key = key_fn(t)
        except Exception:
            continue
        if key is None:
            continue
        b = buckets[key]
        b["n"] += 1
        b["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            b["wins"] += 1
    return dict(buckets)


def holding_minutes(t: dict) -> float:
    try:
        e = datetime.fromisoformat(t["entry_time"].replace("Z", ""))
        x = datetime.fromisoformat(t["exit_time"].replace("Z", ""))
        return (x - e).total_seconds() / 60
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────
# Gap detection → config suggestions
# ─────────────────────────────────────────────────────────────


@dataclass
class Suggestion:
    severity: str       # critical / high / medium / low / info
    finding: str
    evidence: str
    action: str
    config_change: Optional[Dict] = None


def detect_gaps(trades: List[dict], stats: DayStats) -> List[Suggestion]:
    out: List[Suggestion] = []

    if stats.n < 5:
        out.append(Suggestion(
            severity="info",
            finding="Too few trades for confident analysis",
            evidence=f"Only {stats.n} trades in the window",
            action="Need more samples before tuning config.",
        ))
        return out

    # ── 1. R:R inversion (wins smaller than losses)
    if stats.rr and stats.rr < 0.8:
        mult_hint = 2.5 if stats.rr < 0.5 else 2.25
        out.append(Suggestion(
            severity="high",
            finding=f"R:R is 1:{stats.rr:.2f} (wins smaller than losses)",
            evidence=f"Avg win Rs {stats.avg_win:+.2f} vs avg loss Rs {stats.avg_loss:.2f}. "
                     f"Need {stats.breakeven_wr:.0f}% WR to break even at this R:R.",
            action=f"Widen SL multiplier to catch wider moves OR tighten TP. "
                   f"Suggest atr_stop_multiplier -> {mult_hint}.",
            config_change={"risk.atr_stop_multiplier": mult_hint},
        ))

    # ── 2. Stop-loss domination
    exit_buckets = by_dimension(trades, lambda t: t.get("exit_reason", "?"))
    sl_bucket = exit_buckets.get("stop_loss", {"n": 0, "wins": 0, "pnl": 0})
    sl_rate = sl_bucket["n"] / stats.n if stats.n else 0
    if sl_rate > 0.45 and stats.n >= 10:
        out.append(Suggestion(
            severity="high",
            finding=f"Stop-loss dominates exits ({sl_rate*100:.0f}% of trades)",
            evidence=f"{sl_bucket['n']}/{stats.n} trades hit SL. "
                     f"Only {sl_bucket['wins']} of those were net wins (slippage-driven).",
            action="Stops are being triggered by noise. Widen ATR multiplier, "
                   "or tighten entry signals (raise ensemble confidence threshold).",
            config_change={"ensemble.confidence_threshold": 0.6},
        ))

    # ── 3. Hour-of-day weakness
    hour_buckets = by_dimension(trades, lambda t: int(t["entry_time"][11:13]))
    weak_hours: List[int] = []
    for hr, b in sorted(hour_buckets.items()):
        if b["n"] >= 3:
            wr = b["wins"] / b["n"] * 100
            if wr < 30 and b["pnl"] < -20:
                weak_hours.append(hr)
    if weak_hours:
        new_blocks = [f"{h:02d}:00-{h+1:02d}:00" for h in weak_hours]
        out.append(Suggestion(
            severity="high",
            finding=f"Hour(s) {weak_hours} consistently losing",
            evidence=f"Windows with <30% win rate and negative P&L: {weak_hours}",
            action=f"Add to robustness.dead_hour_blocks: {new_blocks}",
            config_change={"robustness.dead_hour_blocks_append": new_blocks},
        ))

    # ── 4. Symbol churn / repeat losers
    sym_buckets = by_dimension(trades, lambda t: t["symbol"])
    repeat_losers = [
        (s, b) for s, b in sym_buckets.items()
        if b["n"] >= 3 and b["wins"] == 0
    ]
    if repeat_losers:
        syms = [s for s, _ in repeat_losers]
        out.append(Suggestion(
            severity="high",
            finding=f"Repeat losing symbols: {syms}",
            evidence="Each of these symbols opened >=3 times today with 0 wins.",
            action="Blacklist after 2 losses (max_losses_per_stock_per_day=2 is already set). "
                   "If it still happens, reduce to 1, or investigate why entries keep firing.",
            config_change={"robustness.max_losses_per_stock_per_day": 1},
        ))

    # ── 5. Tiny wins (edge vs charges)
    tiny_wins = [t for t in trades if 0 < t["pnl"] < 15]
    if stats.wins > 0 and len(tiny_wins) / stats.wins > 0.6:
        out.append(Suggestion(
            severity="medium",
            finding=f"{len(tiny_wins)}/{stats.wins} wins are under Rs 15 (tight edge)",
            evidence="Small wins barely clear charges; a single slippage spike wipes them out.",
            action="Raise min_absolute_reward_rs threshold.",
            config_change={"risk.min_absolute_reward_rs": 25.0},
        ))

    # ── 6. Holding-time inversion (losses held longer than wins)
    win_holds = [holding_minutes(t) for t in trades if t["pnl"] > 0]
    loss_holds = [holding_minutes(t) for t in trades if t["pnl"] <= 0]
    if win_holds and loss_holds:
        avg_win_hold = sum(win_holds) / len(win_holds)
        avg_loss_hold = sum(loss_holds) / len(loss_holds)
        if avg_loss_hold > avg_win_hold * 1.3 and avg_loss_hold > 60:
            out.append(Suggestion(
                severity="medium",
                finding="Losing trades held significantly longer than winners",
                evidence=f"Avg hold — wins: {avg_win_hold:.0f}m  losses: {avg_loss_hold:.0f}m",
                action="Classic 'cut losses early / let winners run' violation. "
                       "Check that intraday_exit_time actually fires for bleeders.",
            ))

    # ── 7. Strategy-level underperformers
    strat_buckets = by_dimension(trades, lambda t: t.get("strategy", "?"))
    for s, b in strat_buckets.items():
        if b["n"] >= 5 and b["wins"] / b["n"] < 0.2 and b["pnl"] < -20:
            out.append(Suggestion(
                severity="medium",
                finding=f"Strategy '{s}' underperforming",
                evidence=f"{b['wins']}/{b['n']} wins, P&L Rs {b['pnl']:+.2f}",
                action=f"Consider reducing weight in ensemble or disabling until backtest confirms edge.",
                config_change={f"ensemble.weights.{s}": 0.3},
            ))

    # ── 8. Profit factor < 1
    if stats.profit_factor and stats.profit_factor < 1.0:
        out.append(Suggestion(
            severity="high",
            finding=f"Profit factor {stats.profit_factor:.2f} < 1.0 (losing system)",
            evidence=f"Every Rs 1 of wins is offset by Rs {1/stats.profit_factor:.2f} of losses.",
            action="Review entire entry/exit framework. Run backtest before adjusting.",
        ))

    # ── 9. High charge drag
    if stats.total_pnl != 0 and abs(stats.total_charges / (stats.total_pnl or 1)) > 0.5:
        out.append(Suggestion(
            severity="low",
            finding="Charge drag is >50% of net P&L",
            evidence=f"Total charges Rs {stats.total_charges:.2f} vs net P&L Rs {stats.total_pnl:+.2f}",
            action="Over-trading or tiny profits. Fewer, larger trades reduce relative drag.",
        ))

    return out


# ─────────────────────────────────────────────────────────────
# Report builders
# ─────────────────────────────────────────────────────────────


def _fmt_row(label: str, n: int, wins: int, pnl: float, extra: str = "") -> str:
    wr = wins / n * 100 if n else 0
    return f"  {label:<24} n={n:>3}  wins={wins:>3}  win%={wr:>5.1f}  pnl=Rs{pnl:>+9.2f}{extra}"


def build_report(
    date_label: str,
    trades: List[dict],
    stats: DayStats,
    orders: List[dict],
    equity: List[dict],
    suggestions: List[Suggestion],
) -> str:
    if not trades:
        return f"# Gap Report — {date_label}\n\nNo trades in the window.\n"

    lines = [
        f"# Gap Report — {date_label}",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Summary",
        "",
        f"- Trades: **{stats.n}** (wins: {stats.wins}, losses: {stats.losses})",
        f"- Total P&L: **Rs {stats.total_pnl:+,.2f}**",
        f"- Win rate: **{stats.win_rate:.1f}%**",
        f"- R:R ratio: **1 : {stats.rr:.2f}**",
        f"- Profit factor: **{stats.profit_factor:.2f}**",
        f"- Expectancy/trade: **Rs {stats.expectancy:+.2f}**",
        f"- Breakeven WR needed (at current R:R): **{stats.breakeven_wr:.0f}%**",
        f"- Total charges: Rs {stats.total_charges:.2f}",
        "",
    ]

    # Exit mix
    exit_buckets = by_dimension(trades, lambda t: t.get("exit_reason", "?"))
    lines.append("## Exit-reason mix")
    lines.append("")
    lines.append("```")
    for reason, b in sorted(exit_buckets.items(), key=lambda kv: -kv[1]["n"]):
        lines.append(_fmt_row(reason, int(b["n"]), int(b["wins"]), b["pnl"]))
    lines.append("```")
    lines.append("")

    # Hour-of-day
    hour_buckets = by_dimension(trades, lambda t: int(t["entry_time"][11:13]))
    if hour_buckets:
        lines.append("## By entry hour")
        lines.append("")
        lines.append("```")
        for hr in sorted(hour_buckets):
            b = hour_buckets[hr]
            wr = b["wins"] / b["n"] * 100 if b["n"] else 0
            flag = "  <-- weak" if wr < 30 and b["n"] >= 2 else ""
            lines.append(_fmt_row(f"{hr:02d}:00-{hr:02d}:59", int(b["n"]), int(b["wins"]), b["pnl"], flag))
        lines.append("```")
        lines.append("")

    # Holding-time buckets
    lines.append("## Holding-time buckets")
    lines.append("")
    lines.append("```")
    for low, high, label in [(0, 10, "<10 min"), (10, 30, "10-30 min"),
                             (30, 60, "30-60 min"), (60, 120, "1-2 hr"),
                             (120, 10000, ">2 hr")]:
        bucket = [t for t in trades if low <= holding_minutes(t) < high]
        if bucket:
            bw = sum(1 for t in bucket if t["pnl"] > 0)
            bp = sum(t["pnl"] for t in bucket)
            lines.append(_fmt_row(label, len(bucket), bw, bp))
    lines.append("```")
    lines.append("")

    # Symbol repeats
    sym_buckets = by_dimension(trades, lambda t: t["symbol"])
    repeats = [(s, b) for s, b in sym_buckets.items() if b["n"] >= 2]
    if repeats:
        lines.append("## Symbol repeats (n >= 2)")
        lines.append("")
        lines.append("```")
        for s, b in sorted(repeats, key=lambda kv: -kv[1]["n"]):
            lines.append(_fmt_row(s, int(b["n"]), int(b["wins"]), b["pnl"]))
        lines.append("```")
        lines.append("")

    # Strategy breakdown
    strat_buckets = by_dimension(trades, lambda t: t.get("strategy", "?"))
    if len(strat_buckets) > 1:
        lines.append("## By strategy")
        lines.append("")
        lines.append("```")
        for s, b in sorted(strat_buckets.items(), key=lambda kv: -kv[1]["n"]):
            lines.append(_fmt_row(s, int(b["n"]), int(b["wins"]), b["pnl"]))
        lines.append("```")
        lines.append("")

    # Gate efficacy (from orders table vs trades)
    if orders:
        rejections = [o for o in orders if o.get("status") not in ("FILLED", "PARTIALLY_FILLED", "PLACED")]
        partials = [o for o in orders if o.get("status") == "PARTIALLY_FILLED"]
        lines.append("## Order ledger")
        lines.append("")
        lines.append(f"- Total orders: {len(orders)}")
        lines.append(f"- Filled: {len(orders) - len(rejections)}")
        lines.append(f"- Partial fills: {len(partials)}")
        lines.append(f"- Rejected: {len(rejections)}")
        lines.append("")

    # Suggestions
    lines.append("## Config suggestions")
    lines.append("")
    if not suggestions:
        lines.append("No actionable gaps detected.")
    else:
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        for sug in sorted(suggestions, key=lambda s: order.get(s.severity, 9)):
            lines.append(f"### [{sug.severity.upper()}] {sug.finding}")
            lines.append("")
            lines.append(f"**Evidence**: {sug.evidence}")
            lines.append("")
            lines.append(f"**Action**: {sug.action}")
            lines.append("")
            if sug.config_change:
                lines.append("```yaml")
                for k, v in sug.config_change.items():
                    lines.append(f"{k}: {v}")
                lines.append("```")
                lines.append("")

    return "\n".join(lines)


def save_outputs(date_label: str, report: str, suggestions: List[Suggestion]) -> Tuple[str, str]:
    os.makedirs(LOG_DIR, exist_ok=True)
    md_path = os.path.join(LOG_DIR, f"gap_report_{date_label}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)

    json_path = os.path.join(LOG_DIR, f"config_suggestions_{date_label}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "date": date_label,
                "generated": datetime.now().isoformat(),
                "suggestions": [
                    {
                        "severity": s.severity,
                        "finding": s.finding,
                        "evidence": s.evidence,
                        "action": s.action,
                        "config_change": s.config_change,
                    }
                    for s in suggestions
                ],
            },
            f,
            indent=2,
        )
    return md_path, json_path


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", help="YYYY-MM-DD (defaults to today)")
    p.add_argument("--days", type=int, default=1,
                   help="Rolling window in days, ending at --date (default 1)")
    p.add_argument("--quiet", action="store_true", help="Don't print report, just save")
    args = p.parse_args()

    end_date = datetime.fromisoformat(args.date).date() if args.date else datetime.now().date()
    start_date = end_date - timedelta(days=args.days - 1)
    start_iso = start_date.strftime("%Y-%m-%dT00:00:00")
    end_iso = (end_date + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")

    if args.days == 1:
        date_label = end_date.strftime("%Y-%m-%d")
    else:
        date_label = f"{start_date}_to_{end_date}"

    trades = load_trades(start_iso, end_iso)
    orders = load_orders(start_iso, end_iso)
    equity = load_equity_curve(start_iso, end_iso)
    stats = compute_stats(trades)
    suggestions = detect_gaps(trades, stats)

    report = build_report(date_label, trades, stats, orders, equity, suggestions)
    md_path, json_path = save_outputs(date_label, report, suggestions)

    if not args.quiet:
        print(report)
        print()
        print(f"Saved: {md_path}")
        print(f"Saved: {json_path}")


if __name__ == "__main__":
    main()
