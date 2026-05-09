"""Aggregate every per-day post-mortem in logs/postmortem/ into a single
strategy-level report. Used to confirm patterns aren't just a single bad
day. Generates `logs/postmortem/_aggregate.md`.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PM_DIR = ROOT / "logs" / "postmortem"


def main() -> None:
    files = sorted([p for p in PM_DIR.glob("20*.md") if p.stem != "_aggregate"])
    if not files:
        print("No per-day post-mortem files yet.")
        return

    by_strategy: dict[str, list[dict]] = defaultdict(list)
    by_flag: dict[str, int] = defaultdict(int)

    trade_re = re.compile(
        r"## (\S+) (BUY|SELL)\s+([^\n]*)\n.*?Strategy:\*\* `([^`]+)`.*?"
        r"Realised:\*\* Rs ([+\-][\d,\.]+)"
        r"(?:.*?Capture:\*\* ([\-\d.]+)%)?"
        r"(?:.*?money on table:\*\* Rs ([+\-]?[\d,\.]+))?"
        r"(?:.*?pct_vs_sma50.*?\(([+\-][\d.]+)%\))?",
        re.DOTALL,
    )

    for f in files:
        text = f.read_text(encoding="utf-8")
        sections = text.split("## ")
        for sec in sections[1:]:
            head = sec.splitlines()[0]
            if " " not in head:
                continue
            sym_side = head.split()
            if len(sym_side) < 2:
                continue
            symbol = sym_side[0]
            side = sym_side[1]
            flags_line = " ".join(sym_side[2:]) if len(sym_side) > 2 else ""
            flags = re.findall(r"\[([A-Z\-]+)\]", flags_line)
            for fl in flags:
                by_flag[fl] += 1

            strat_m = re.search(r"Strategy:\*\* `([^`]+)`", sec)
            pnl_m = re.search(r"Realised:\*\* Rs ([+\-][\d,\.]+)", sec)
            cap_m = re.search(r"Capture:\*\* ([\-\d.]+)%", sec)
            mot_m = re.search(r"money on table:\*\* Rs ([+\-][\d,\.]+)", sec)
            sma_m = re.search(r"50d SMA [\d.]+\s+\(([+\-][\d.]+)%\)", sec)
            reason_m = re.search(r"exit reason `([^`]+)`", sec)
            if not strat_m or not pnl_m:
                continue

            try:
                pnl = float(pnl_m.group(1).replace(",", ""))
            except ValueError:
                continue
            try:
                cap = float(cap_m.group(1)) if cap_m else None
            except ValueError:
                cap = None
            try:
                mot = float(mot_m.group(1).replace(",", "")) if mot_m else None
            except ValueError:
                mot = None
            try:
                sma_pct = float(sma_m.group(1)) if sma_m else None
            except ValueError:
                sma_pct = None

            by_strategy[strat_m.group(1)].append({
                "day": f.stem,
                "symbol": symbol,
                "side": side,
                "pnl": pnl,
                "capture": cap,
                "money_on_table": mot,
                "sma50_pct": sma_pct,
                "reason": reason_m.group(1) if reason_m else "?",
                "flags": flags,
            })

    out_lines = ["# Strategy Post-Mortem Aggregate", ""]
    out_lines.append(f"**Days analysed:** {len(files)} ({files[0].stem} to {files[-1].stem})")
    out_lines.append("")
    out_lines.append("## Strategy summary")
    out_lines.append("")
    out_lines.append("| Strategy | Trades | Win | Loss | Net PnL | WR | Avg capture | Total table | "
                     "% trend-mismatch | % opening (09:15-09:30 entries via flag) |")
    out_lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

    for strat, trades in sorted(by_strategy.items(), key=lambda x: -sum(t["pnl"] for t in x[1])):
        wins = sum(1 for t in trades if t["pnl"] > 0)
        losses = sum(1 for t in trades if t["pnl"] < 0)
        net = sum(t["pnl"] for t in trades)
        wr = wins / len(trades) * 100 if trades else 0
        caps = [t["capture"] for t in trades if t["capture"] is not None]
        avg_cap = sum(caps) / len(caps) if caps else 0
        mots = [t["money_on_table"] for t in trades if t["money_on_table"] is not None]
        total_mot = sum(mots) if mots else 0
        tm_count = sum(1 for t in trades
                       if any("TREND-MISMATCH" in f for f in t["flags"]))
        tm_pct = tm_count / len(trades) * 100 if trades else 0
        out_lines.append(
            f"| `{strat}` | {len(trades)} | {wins} | {losses} | "
            f"Rs {net:+,.0f} | {wr:.0f}% | {avg_cap:.0f}% | "
            f"Rs {total_mot:+,.0f} | {tm_pct:.0f}% | n/a |"
        )

    out_lines.append("")
    out_lines.append("## Flag frequency")
    out_lines.append("")
    out_lines.append("| Flag | Count |")
    out_lines.append("|---|---:|")
    for flag, count in sorted(by_flag.items(), key=lambda x: -x[1]):
        out_lines.append(f"| `[{flag}]` | {count} |")

    out_lines.append("")
    out_lines.append("## Hypothesis check: trend filter at 5% — PER STRATEGY")
    out_lines.append("")
    out_lines.append("| Strategy | Total | Would block | Blocked-trades PnL | Pass-through PnL | Verdict |")
    out_lines.append("|---|---:|---:|---:|---:|---|")

    for strat, trades in sorted(by_strategy.items()):
        n_total = 0
        blocked = []
        passed = []
        for t in trades:
            if t["sma50_pct"] is None:
                continue
            n_total += 1
            block = (t["side"] == "SELL" and t["sma50_pct"] > 5) or \
                    (t["side"] == "BUY" and t["sma50_pct"] < -5)
            if block:
                blocked.append(t)
            else:
                passed.append(t)
        b_pnl = sum(t["pnl"] for t in blocked)
        p_pnl = sum(t["pnl"] for t in passed)
        if blocked:
            verdict = "FILTER SAVES" if b_pnl < -10 else ("FILTER COSTS" if b_pnl > 10 else "neutral")
        else:
            verdict = "no trades to block"
        out_lines.append(
            f"| `{strat}` | {n_total} | {len(blocked)} | "
            f"Rs {b_pnl:+,.0f} | Rs {p_pnl:+,.0f} | **{verdict}** |"
        )
    out_lines.append("")
    out_lines.append("**Reading:** `Blocked-trades PnL` is what we'd have given up if the filter was active. "
                     "Negative -> filter saves money. Positive -> filter costs profitable trades. "
                     "The verdict column flags which strategies should ship the filter.")

    out = PM_DIR / "_aggregate.md"
    out.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"Wrote {out}")
    print()
    print("\n".join(out_lines))


if __name__ == "__main__":
    main()
