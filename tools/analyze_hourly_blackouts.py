"""Phase-B prep: identify hours-of-day with structurally negative edge.

Scans every closed trade in `data/trading_agent.db`, buckets by IST hour,
computes per-hour PF / WR / expectancy, and flags blackout candidates --
hours where:

    Profit Factor < BLACKOUT_PF_MAX  (default 0.8)
    AND trade count >= BLACKOUT_MIN_TRADES  (default 10)

Both gates are required: the PF gate identifies negative edge, the sample
gate makes sure we're not killing an hour off n=2 noise. (Sample-size
discipline is the entire point of "Phase A first, Phase B second" -- we
don't blacklist hours based on hunches.)

Output: docs/phase_b_hourly_blackout_candidates.md (overwritten on
re-run) plus a console summary. Re-run safely whenever the trade table
grows.

This is a RESEARCH tool, not a runtime tool. It produces a markdown
report; deciding to actually wire those hours into config.yaml is a
separate Phase-B decision once we have 5 days of Phase-A validation
data confirming the underlying mix is profitable.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "trading_agent.db"
OUT_PATH = ROOT / "docs" / "phase_b_hourly_blackout_candidates.md"

BLACKOUT_PF_MAX = 0.8
BLACKOUT_MIN_TRADES = 10

# Indian market hours: 09:15 - 15:30 IST. Hours outside this window are
# bogus (could only contain after-hours order entries or test data) so we
# clamp the report to the active market hours.
MARKET_HOUR_MIN = 9
MARKET_HOUR_MAX = 15


def main() -> int:
    if not DB_PATH.exists():
        print(f"[ERROR] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # SQLite's strftime() on an ISO-8601 timestamp WITH offset (e.g.
    # "2026-05-08T13:50:06+05:30") converts to UTC first -- giving us 08
    # instead of 13 for an IST entry. Since every entry_time is stored
    # with the same IST offset, we strip the offset (substr 1-19 keeps
    # only "YYYY-MM-DDTHH:MM:SS") so strftime treats it as naive and
    # returns the actual IST hour the trade was entered.
    rows = conn.execute("""
        SELECT CAST(strftime('%H', substr(entry_time, 1, 19)) AS INTEGER) AS hour,
               COUNT(*) AS trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_win,
               SUM(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END) AS gross_loss,
               SUM(pnl) AS net_pnl,
               AVG(pnl) AS avg_pnl
        FROM trades
        WHERE exit_time IS NOT NULL
          AND entry_time IS NOT NULL
        GROUP BY hour
        ORDER BY hour
    """).fetchall()
    conn.close()

    if not rows:
        print("[NO DATA] no closed trades to analyze")
        return 1

    # Compute per-hour metrics + flag candidates.
    hourly = []
    for r in rows:
        h = int(r["hour"]) if r["hour"] is not None else -1
        if h < MARKET_HOUR_MIN or h > MARKET_HOUR_MAX:
            continue
        trades = int(r["trades"] or 0)
        wins = int(r["wins"] or 0)
        gw = float(r["gross_win"] or 0.0)
        gl = float(r["gross_loss"] or 0.0)
        net = float(r["net_pnl"] or 0.0)
        wr = (wins / trades) if trades else 0.0
        pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
        avg = (net / trades) if trades else 0.0
        is_candidate = pf < BLACKOUT_PF_MAX and trades >= BLACKOUT_MIN_TRADES
        hourly.append({
            "hour": h,
            "trades": trades,
            "wins": wins,
            "wr": wr,
            "gross_win": gw,
            "gross_loss": gl,
            "net": net,
            "pf": pf,
            "avg": avg,
            "candidate": is_candidate,
        })

    total_trades = sum(h["trades"] for h in hourly)
    candidates = [h for h in hourly if h["candidate"]]
    inconclusive_low_pf = [h for h in hourly if h["pf"] < BLACKOUT_PF_MAX and not h["candidate"]]

    # ── Build markdown report ───────────────────────────────────────────
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# Phase B prep — Hourly Blackout Candidates",
        "",
        f"_Generated {when} from `{DB_PATH.relative_to(ROOT)}` "
        f"({total_trades} closed trades across {len(hourly)} active hours)_",
        "",
        "## Purpose",
        "",
        "Identify hours-of-day where new entries have **structurally negative "
        "edge** and should be blacklisted in Phase B (alongside quarter-Kelly "
        "sizing). Two gates must be passed for an hour to make the candidate "
        "list:",
        "",
        f"1. **PF gate**: profit factor < `{BLACKOUT_PF_MAX}` "
        f"(i.e., gross losses > 0.8 × gross wins -- structural bleed)",
        f"2. **Sample gate**: at least `{BLACKOUT_MIN_TRADES}` trades in that "
        f"hour (kills noise-driven false positives)",
        "",
        "Both gates required. An hour with PF 0.4 from 2 trades is **not** a "
        "blackout candidate; statistically it's a coin flip dressed up as data.",
        "",
        "## Per-hour breakdown",
        "",
        "| Hour (IST) | Trades | WR% | Gross Win | Gross Loss | PF | Net PnL | Avg/trade | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for h in hourly:
        pf_disp = f"{h['pf']:.2f}" if h["pf"] != float("inf") else "INF"
        status = (
            "**BLACKOUT CANDIDATE**" if h["candidate"]
            else "low-PF n<gate" if h["pf"] < BLACKOUT_PF_MAX
            else "ok"
        )
        lines.append(
            f"| {h['hour']:02d}:00-{h['hour']:02d}:59 | {h['trades']} | "
            f"{h['wr']*100:.0f}% | Rs {h['gross_win']:,.0f} | "
            f"Rs {h['gross_loss']:,.0f} | {pf_disp} | "
            f"Rs {h['net']:+,.0f} | Rs {h['avg']:+,.1f} | {status} |"
        )

    lines.extend(["", "## Candidates (action items for Phase B)", ""])
    if candidates:
        for h in candidates:
            lines.append(
                f"- **{h['hour']:02d}:00-{h['hour']:02d}:59 IST** -- "
                f"PF {h['pf']:.2f}, "
                f"{h['trades']} trades, "
                f"net Rs {h['net']:+,.0f} "
                f"(avg Rs {h['avg']:+,.1f}/trade). "
                f"Blacklisting this hour retroactively would have removed "
                f"Rs {h['gross_loss']:,.0f} in gross losses against "
                f"Rs {h['gross_win']:,.0f} in gross wins -- net delta "
                f"**Rs {-h['net']:+,.0f}**."
            )
    else:
        lines.append(
            "_No hour meets BOTH gates. Either the edge is uniform across "
            "the trading day, or we have insufficient samples to call any "
            "hour structurally bad. Re-run after the trade table grows by "
            "another ~30 trades (typically one good week of activity)._"
        )

    lines.extend(["", "## Inconclusive: low PF, insufficient samples", ""])
    if inconclusive_low_pf:
        for h in inconclusive_low_pf:
            pf_disp = f"{h['pf']:.2f}" if h["pf"] != float("inf") else "INF"
            lines.append(
                f"- {h['hour']:02d}:00-{h['hour']:02d}:59 IST: "
                f"PF {pf_disp}, only {h['trades']} trades "
                f"(need >= {BLACKOUT_MIN_TRADES}). Suggests potential bleed but "
                f"cannot conclude. Watch list for re-analysis next week."
            )
    else:
        lines.append(
            "_No additional hours with PF below the gate. All hours either "
            "pass the PF gate or have enough samples to confirm/reject._"
        )

    # ── Projected impact if all candidates get blacklisted ────────────
    lines.extend(["", "## What-if: blacklist all candidate hours retroactively", ""])
    if candidates:
        cand_trades = sum(h["trades"] for h in candidates)
        cand_net = sum(h["net"] for h in candidates)
        cand_gw = sum(h["gross_win"] for h in candidates)
        cand_gl = sum(h["gross_loss"] for h in candidates)
        kept_trades = total_trades - cand_trades
        # Reconstruct portfolio PF if these trades had not happened.
        port_gw = sum(h["gross_win"] for h in hourly)
        port_gl = sum(h["gross_loss"] for h in hourly)
        port_net = sum(h["net"] for h in hourly)
        port_pf = (port_gw / port_gl) if port_gl > 0 else float("inf")
        kept_gw = port_gw - cand_gw
        kept_gl = port_gl - cand_gl
        kept_pf = (kept_gw / kept_gl) if kept_gl > 0 else float("inf")
        kept_net = port_net - cand_net
        lines.extend([
            f"- **Trades cut**: {cand_trades} ({cand_trades/total_trades*100:.1f}% "
            f"of total {total_trades})",
            f"- **PnL cut (gross)**: -Rs {cand_gw:,.0f} wins, "
            f"-Rs {cand_gl:,.0f} losses",
            f"- **Net PnL preserved**: Rs {-cand_net:+,.0f} (since these "
            f"hours were net {('negative' if cand_net < 0 else 'positive')})",
            f"- **Portfolio PF would shift**: {port_pf:.2f} -> **{kept_pf:.2f}**",
            f"- **Portfolio PnL would shift**: Rs {port_net:+,.0f} -> "
            f"**Rs {kept_net:+,.0f}**",
            "",
            "_Counterfactual caveat: removing these hours frees attention/"
            "capital that other hours can use. Forward performance may differ. "
            "Validate via walk-forward backtest before deploying._",
        ])
    else:
        lines.append("_No candidates -- no what-if to compute._")

    lines.extend(["", "## Next steps", ""])
    lines.extend([
        "1. **Wait for Phase A validation to PASS** (5-day rolling PF >= 1.5). "
        "Do NOT deploy blackouts on an unvalidated strategy mix.",
        "2. Once Phase A passes: re-run this script. If candidates have "
        "stabilized (same hours flagged with more data), proceed.",
        "3. Walk-forward backtest with the proposed blackouts enabled. Use "
        f"`tools/run_battery.py --train-window-days 60 --holdout-window-days 30`.",
        "4. If holdout PF improves: add the blackouts to `config.yaml` under "
        "`risk.entry_blackout_hours` (key TBD) + ship behind a feature flag.",
        "5. Re-validate live for 5 trading days (Phase A re-check).",
    ])
    lines.append("")
    lines.append(f"_To regenerate: `python tools/analyze_hourly_blackouts.py`_")
    lines.append("")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")

    # ── Console summary ───────────────────────────────────────────────
    print(f"\n=== Hourly blackout analysis ({total_trades} trades) ===")
    print(f"{'Hour':<8} {'N':>4} {'WR':>5} {'PF':>6} {'Net':>10}  Status")
    for h in hourly:
        pf_disp = f"{h['pf']:.2f}" if h["pf"] != float("inf") else "INF"
        flag = ("BLACKOUT" if h["candidate"]
                else "low-PF" if h["pf"] < BLACKOUT_PF_MAX
                else "ok")
        print(f"{h['hour']:02d}:00    {h['trades']:>4} "
              f"{h['wr']*100:>4.0f}% {pf_disp:>6}  Rs {h['net']:>+8.0f}  {flag}")
    print(f"\nBlackout candidates: {len(candidates)}")
    print(f"Saved: {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
