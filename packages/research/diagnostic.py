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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DB_PATH = ROOT / "data" / "trading_agent.db"
OUT_DIR = ROOT / "logs" / "diagnostics"
OUT_DIR.mkdir(parents=True, exist_ok=True)


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
def build_report(trades: list[dict], stats: dict[str, StratStats],
                 port: dict, args: argparse.Namespace) -> str:
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    period = f"last {args.days}d" if args.days else "all-time"
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
