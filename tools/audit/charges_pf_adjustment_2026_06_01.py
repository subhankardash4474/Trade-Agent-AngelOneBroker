"""CHG-01..CHG-05 per-variant PF adjustment script (audit 2026-06-01).

Walks every battery-style results JSON we have locally and reproduces, for
each trade, the AngelOne-corrected charges the trade would have incurred
in production. Then re-computes the variant's headline metrics
(PnL, profit factor, win rate, expectancy) so we can quantify how
optimistic the pre-CHG reports were.

Methodology (deterministic, no re-simulation):
  - For each trade record we read entry_price, exit_price, quantity, side,
    plus the variant's product_type (read from `overrides`).
  - We compute `correct_charges = compute_round_trip(...)` using the NEW
    AngelOne calibration -- the same code production now uses.
  - The trade's pre-CHG "commission" is in the JSON. The extra charges
    we should have charged are `correct_charges - old_commission`.
  - Adjusted per-trade PnL = original_pnl - extra_charges.
  - We do NOT touch the original JSON; the output is a Markdown report
    keyed by variant filename plus a CSV for further analysis.

This is a POST-HOC ANALYSIS. The pre-committed v2.1 and v3 verdict numbers
remain auditable; the report only annotates them. To re-run a variant
against the corrected charges in the simulator, you would need to invoke
the battery scheduler properly -- intentionally out of scope here.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
# The codebase uses `from core.charges import ...` style (the `packages/`
# directory is on sys.path via conftest.py). Mirror that here.
sys.path.insert(0, str(REPO_ROOT / "packages"))

from core.charges import compute_round_trip  # noqa: E402


def _safe_get_product(overrides: list[list[Any]]) -> str:
    """Read execution.product_type from the overrides list; default INTRADAY."""
    for kv in overrides or []:
        if len(kv) >= 2 and kv[0] == "execution.product_type":
            return str(kv[1]).upper()
    return "INTRADAY"


def _safe_round_trip(buy_price: float, sell_price: float, qty: int, product: str) -> float:
    """Cost of one round trip, ₹. Returns NaN if inputs are not finite."""
    if not (math.isfinite(buy_price) and math.isfinite(sell_price)):
        return float("nan")
    if qty <= 0:
        return 0.0
    try:
        return compute_round_trip(
            buy_price=buy_price,
            sell_price=sell_price,
            quantity=qty,
            product=product,
        ).total
    except Exception as exc:  # pragma: no cover - sanity log only
        print(f"  WARN compute_round_trip raised: {exc!r}", file=sys.stderr)
        return float("nan")


def _adjust_trade(trade: dict[str, Any], product: str) -> tuple[float, float, float, float]:
    """Return (orig_pnl, adj_pnl, orig_charges, correct_charges) for one trade."""
    entry = float(trade.get("entry_price", 0.0) or 0.0)
    exit_ = float(trade.get("exit_price", 0.0) or 0.0)
    qty = int(trade.get("quantity", 0) or 0)
    side = str(trade.get("side", "BUY")).upper()
    orig_pnl = float(trade.get("pnl", 0.0) or 0.0)
    orig_charges = float(trade.get("commission", 0.0) or 0.0)

    if qty <= 0 or entry <= 0 or exit_ <= 0:
        return orig_pnl, orig_pnl, orig_charges, orig_charges

    # compute_round_trip wants (buy_price, sell_price). For a SHORT we open
    # at the entry (which is the SELL leg) and close at exit (the BUY leg).
    if side == "BUY":
        buy_price, sell_price = entry, exit_
    else:
        buy_price, sell_price = exit_, entry

    correct = _safe_round_trip(buy_price, sell_price, qty, product)
    if not math.isfinite(correct):
        return orig_pnl, orig_pnl, orig_charges, orig_charges

    delta = correct - orig_charges
    adj_pnl = orig_pnl - delta
    return orig_pnl, adj_pnl, orig_charges, correct


def _profit_factor(pnls: list[float]) -> float:
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss == 0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


def _process_variant(json_path: Path) -> dict[str, Any]:
    raw = json_path.read_text(encoding="utf-8")
    data = json.loads(raw)

    overrides = data.get("overrides", [])
    product = _safe_get_product(overrides)
    trades = data.get("trades", [])
    summary = data.get("summary", {}) or {}

    orig_pnls: list[float] = []
    adj_pnls: list[float] = []
    extra_charges_total = 0.0
    correct_charges_total = 0.0
    orig_charges_total = 0.0

    for tr in trades:
        op, ap, oc, cc = _adjust_trade(tr, product)
        orig_pnls.append(op)
        adj_pnls.append(ap)
        orig_charges_total += oc
        correct_charges_total += cc
        extra_charges_total += (cc - oc)

    orig_pnl_total = sum(orig_pnls)
    adj_pnl_total = sum(adj_pnls)

    orig_pf = _profit_factor(orig_pnls)
    adj_pf = _profit_factor(adj_pnls)

    orig_wins = sum(1 for p in orig_pnls if p > 0)
    adj_wins = sum(1 for p in adj_pnls if p > 0)
    n = len(orig_pnls)

    orig_wr = (100.0 * orig_wins / n) if n else 0.0
    adj_wr = (100.0 * adj_wins / n) if n else 0.0
    flipped = sum(1 for o, a in zip(orig_pnls, adj_pnls) if (o > 0) != (a > 0))

    initial_balance = 10_000.0
    orig_return_pct = float(summary.get("return_pct", 100.0 * orig_pnl_total / initial_balance))
    adj_return_pct_delta = 100.0 * (adj_pnl_total - orig_pnl_total) / initial_balance

    return {
        "variant": data.get("variant", json_path.stem),
        "product": product,
        "trades": n,
        "orig_pnl": round(orig_pnl_total, 2),
        "adj_pnl": round(adj_pnl_total, 2),
        "extra_charges": round(extra_charges_total, 2),
        "orig_charges_total": round(orig_charges_total, 2),
        "correct_charges_total": round(correct_charges_total, 2),
        "orig_pf": round(orig_pf, 4) if math.isfinite(orig_pf) else float("inf"),
        "adj_pf": round(adj_pf, 4) if math.isfinite(adj_pf) else float("inf"),
        "orig_wr": round(orig_wr, 2),
        "adj_wr": round(adj_wr, 2),
        "trades_flipped_to_loss": sum(1 for o, a in zip(orig_pnls, adj_pnls) if o > 0 and a <= 0),
        "trades_flipped_to_win": sum(1 for o, a in zip(orig_pnls, adj_pnls) if o <= 0 and a > 0),
        "trades_flipped_total": flipped,
        "orig_return_pct": round(orig_return_pct, 2),
        "adj_return_pct_delta": round(adj_return_pct_delta, 2),
        "summary_pnl_in_json": summary.get("pnl"),
        "summary_pf_in_json": summary.get("profit_factor"),
        "summary_charges_in_json": summary.get("charges"),
        "json_path": str(json_path.relative_to(REPO_ROOT)),
    }


def _fmt_pf(pf: float) -> str:
    if pf == float("inf"):
        return "inf"
    if pf == 0.0:
        return "0.00"
    return f"{pf:.3f}"


def _md_row(r: dict[str, Any]) -> str:
    return (
        f"| {r['variant']} | {r['product']} | {r['trades']} | "
        f"{r['orig_pnl']:>+10,.2f} | {r['adj_pnl']:>+10,.2f} | "
        f"{r['extra_charges']:>+10,.2f} | "
        f"{_fmt_pf(r['orig_pf'])} | {_fmt_pf(r['adj_pf'])} | "
        f"{r['orig_wr']:.1f}% | {r['adj_wr']:.1f}% | "
        f"{r['trades_flipped_total']} ({r['trades_flipped_to_loss']}W→L, "
        f"{r['trades_flipped_to_win']}L→W) |"
    )


def main(out_md: Path, out_csv: Path) -> None:
    battery_dirs = sorted(REPO_ROOT.glob("logs/backtests/*"))
    rows: list[dict[str, Any]] = []
    by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for d in battery_dirs:
        if not d.is_dir():
            continue
        results_dir = d / "results"
        if not results_dir.is_dir():
            continue
        for json_path in sorted(results_dir.glob("*.json")):
            try:
                row = _process_variant(json_path)
            except Exception as exc:
                print(f"FAILED {json_path}: {exc!r}", file=sys.stderr)
                continue
            row["battery_run"] = d.name
            rows.append(row)
            by_run[d.name].append(row)

    rows.sort(key=lambda r: (r["battery_run"], r["variant"]))

    # CSV (raw, every metric)
    if rows:
        fieldnames = list(rows[0].keys())
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {len(rows)} rows to {out_csv}")
    else:
        print("No variant JSONs found.")
        return

    # Markdown report
    md_lines: list[str] = []
    md_lines.append("# CHG-01..CHG-05 Per-Variant PF Adjustment — 2026-06-01\n")
    md_lines.append(
        f"_Generated by `tools/audit/charges_pf_adjustment_2026_06_01.py` on "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M IST')}._\n"
    )
    md_lines.append("")
    md_lines.append(
        "Re-prices every trade in every locally-available battery results JSON "
        "using the AngelOne-corrected charges model (CHG-01..CHG-05 — see "
        "`docs/findings/findings_log_2026-06-01.md`). The original JSONs are "
        "**not modified**; this is a transparent post-hoc adjustment.\n"
    )
    md_lines.append("**Methodology:**")
    md_lines.append("")
    md_lines.append("1. For each trade we recompute `compute_round_trip(buy, sell, qty, product)` "
                    "under the new AngelOne defaults.")
    md_lines.append("2. `extra_charges = correct_charges − original_commission`")
    md_lines.append("3. `adjusted_pnl = original_pnl − extra_charges`")
    md_lines.append("4. PF is recomputed from the adjusted per-trade PnLs as "
                    "`Σ(adj_pnl>0) / |Σ(adj_pnl<0)|`.")
    md_lines.append("5. Trades that flip from winner to loser (or vice-versa) are counted.")
    md_lines.append("")
    md_lines.append(
        "**What this number is NOT:** a re-simulated backtest. The simulator's "
        "position-sizing, gate-firing, ensemble vote, etc. would all be different "
        "if it had been priced correctly. A full re-run would likely show fewer "
        "trades (more rejected at the reward_vs_charges gate) and somewhat "
        "different per-trade outcomes. Treat the numbers below as a **lower "
        "bound on the optimistic bias** — a re-simulation would almost certainly "
        "tighten PF further on the v3 swing variants.\n"
    )

    for run_name, run_rows in by_run.items():
        md_lines.append(f"## `{run_name}`\n")
        # Run-level summary
        total_extra = sum(r["extra_charges"] for r in run_rows)
        total_orig_pnl = sum(r["orig_pnl"] for r in run_rows)
        total_adj_pnl = sum(r["adj_pnl"] for r in run_rows)
        total_trades = sum(r["trades"] for r in run_rows)
        product_set = sorted({r["product"] for r in run_rows})
        md_lines.append(
            f"**Run-level totals:** {len(run_rows)} variants, {total_trades:,} trades, "
            f"product={','.join(product_set)}. "
            f"Total extra charges under AngelOne: **₹{total_extra:+,.2f}**. "
            f"Aggregate PnL: ₹{total_orig_pnl:+,.2f} → ₹{total_adj_pnl:+,.2f}.\n"
        )
        md_lines.append(
            "| Variant | Product | Trades | Orig PnL ₹ | Adj PnL ₹ | "
            "Δ Charges ₹ | Orig PF | Adj PF | Orig WR | Adj WR | Trades flipped |"
        )
        md_lines.append(
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
        )
        for r in run_rows:
            md_lines.append(_md_row(r))
        md_lines.append("")

    out_md.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Wrote Markdown report to {out_md}")


if __name__ == "__main__":
    out_dir = REPO_ROOT / "docs" / "findings"
    out_dir.mkdir(parents=True, exist_ok=True)
    main(
        out_md=out_dir / "charges_pf_adjustment_2026-06-01.md",
        out_csv=out_dir / "charges_pf_adjustment_2026-06-01.csv",
    )
