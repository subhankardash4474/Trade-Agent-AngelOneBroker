"""One-shot diagnostic — confirm V25-AngelOne 4-winners aren't a single-symbol cluster.

Reads:
    logs/backtests/battery_chg_recompute_20260601T114500/results/V25_swing_combined_shorts.json

Prints:
    - Total trade count
    - Winner count + PnL
    - Per-winner: symbol, side, entry/exit dates, PnL, strategy
    - Symbol distribution of winners (single-cluster check)

Usage: python tools/_v25_winners_check.py
"""
from __future__ import annotations
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "logs" / "backtests" / "battery_chg_recompute_20260601T114500" / "results"


def _scan(path: Path, label: str) -> None:
    with path.open("r", encoding="utf-8") as f:
        d = json.load(f)

    trades = d.get("trades") or d.get("trade_log") or []
    if not trades and isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "pnl" in v[0]:
                trades = v
                break

    winners = [t for t in trades if (t.get("pnl") or 0) > 0]
    losers = [t for t in trades if (t.get("pnl") or 0) < 0]
    flat = [t for t in trades if (t.get("pnl") or 0) == 0]

    print(f"\n=== {label} ===")
    print(f"path: {path.relative_to(ROOT)}")
    print(f"trades total : {len(trades)}")
    print(f"winners      : {len(winners)}")
    print(f"losers       : {len(losers)}")
    print(f"flat         : {len(flat)}")
    print(f"top-level keys: {sorted(d.keys()) if isinstance(d, dict) else type(d).__name__}")

    if winners:
        win_syms = Counter(t.get("symbol", "?") for t in winners)
        win_strats = Counter(t.get("strategy", "?") for t in winners)
        win_sides = Counter(t.get("side", t.get("direction", "?")) for t in winners)
        print("\nWinner symbol distribution:")
        for sym, n in win_syms.most_common():
            print(f"  {sym:12s} x{n}")
        print("Winner strategy distribution:")
        for s, n in win_strats.most_common():
            print(f"  {s:25s} x{n}")
        print("Winner side distribution:")
        for s, n in win_sides.most_common():
            print(f"  {s:12s} x{n}")

        print("\nPer-winner detail:")
        for t in winners:
            entry = (t.get("entry_time") or t.get("entry") or "?")[:19]
            exit_ = (t.get("exit_time") or t.get("exit") or "?")[:19]
            print(
                f"  {t.get('symbol','?'):12s}  "
                f"{t.get('side', t.get('direction', '?')):6s}  "
                f"strategy={t.get('strategy','?'):20s}  "
                f"entry={entry}  exit={exit_}  "
                f"pnl=Rs{(t.get('pnl') or 0):+.2f}  "
                f"charges=Rs{(t.get('charges') or 0):.2f}"
            )

    if not winners and trades:
        print(f"  (no winners; sample trade keys: {sorted(trades[0].keys())[:12]})")


if __name__ == "__main__":
    for fname in ("V25_swing_combined_shorts.json", "V26_swing_combined_shorts_high_cap.json"):
        p = RESULTS / fname
        if p.exists():
            _scan(p, fname.replace(".json", ""))
        else:
            print(f"[missing] {p}")
