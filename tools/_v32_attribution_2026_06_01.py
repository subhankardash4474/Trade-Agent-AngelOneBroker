"""Per-symbol P&L attribution on V32's trades.csv.

Answers the question: how much of V32's profit comes from broad ETFs
(NIFTYBEES, JUNIORBEES, BANKBEES, NIFTYIETF) vs individual stocks?
If broad ETFs dominate, V32 is closet-indexing — operator must know
this before any deployment decision.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

BROAD_ETFS = {"NIFTYBEES", "JUNIORBEES", "BANKBEES", "NIFTYIETF"}
SECTOR_ETFS = {"PSUBNKBEES", "ITBEES", "CPSEETF"}
COMMODITY_ETFS = {"GOLDBEES", "SILVERBEES"}
DEBT_ETFS = {"LIQUIDBEES", "GILT5YBEES"}


def attribution_report(trades_csv: Path) -> None:
    df = pd.read_csv(trades_csv)
    print(f"=== V32 per-symbol attribution ===")
    print(f"Source: {trades_csv}")
    print(f"Total trades: {len(df)}")
    print(f"Total net P&L: ₹{df['pnl_net_inr'].sum():,.2f}")
    print(f"Win rate: {(df['pnl_net_inr'] > 0).mean() * 100:.1f}%")
    print()

    per_symbol = (
        df.groupby("symbol")
        .agg(
            n_trades=("pnl_net_inr", "count"),
            wins=("pnl_net_inr", lambda x: (x > 0).sum()),
            net_pnl=("pnl_net_inr", "sum"),
            gross_profit=("pnl_net_inr", lambda x: x[x > 0].sum()),
            gross_loss=("pnl_net_inr", lambda x: x[x < 0].sum()),
        )
        .sort_values("net_pnl", ascending=False)
    )
    total_net = per_symbol["net_pnl"].sum()
    per_symbol["pct_of_net"] = per_symbol["net_pnl"] / total_net * 100

    print("=== TOP 15 CONTRIBUTORS (by net P&L) ===")
    print(per_symbol.head(15).to_string(float_format=lambda x: f"{x:,.0f}"))
    print()
    print("=== BOTTOM 10 (worst losers) ===")
    print(per_symbol.tail(10).to_string(float_format=lambda x: f"{x:,.0f}"))
    print()

    def bucket(sym: str) -> str:
        if sym in BROAD_ETFS:
            return "broad_etf"
        if sym in SECTOR_ETFS:
            return "sector_etf"
        if sym in COMMODITY_ETFS:
            return "commodity_etf"
        if sym in DEBT_ETFS:
            return "debt_etf"
        return "individual_stock"

    df["bucket"] = df["symbol"].map(bucket)
    by_bucket = (
        df.groupby("bucket")
        .agg(
            n_trades=("pnl_net_inr", "count"),
            net_pnl=("pnl_net_inr", "sum"),
            unique_symbols=("symbol", "nunique"),
        )
        .sort_values("net_pnl", ascending=False)
    )
    by_bucket["pct_of_net"] = by_bucket["net_pnl"] / total_net * 100
    print("=== AGGREGATED BY BUCKET ===")
    print(by_bucket.to_string(float_format=lambda x: f"{x:,.1f}"))
    print()

    broad_etf_pnl = by_bucket.loc["broad_etf", "net_pnl"] if "broad_etf" in by_bucket.index else 0
    pct = broad_etf_pnl / total_net * 100 if total_net else 0
    print(f"=== HEADLINE ===")
    print(f"Broad ETFs (NIFTYBEES+JUNIORBEES+BANKBEES+NIFTYIETF) contribute "
          f"{pct:.1f}% of V32's net P&L.")
    print()
    if pct > 70:
        print("VERDICT: V32 IS closet-indexing. Mode A is essentially a")
        print("         disguised broad-ETF buy-and-hold. Cannot honestly")
        print("         claim cross-asset trend edge.")
    elif pct > 50:
        print("VERDICT: V32 is HEAVILY broad-ETF biased but has SOME edge")
        print("         from individual stock picks. Marginal case.")
    elif pct > 30:
        print("VERDICT: V32 has MEANINGFUL contribution from individual")
        print("         stocks; broad-ETF concentration is not the whole story.")
    else:
        print("VERDICT: V32's edge comes primarily from individual stocks;")
        print("         broad-ETF concentration is a minor factor.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    trades_csv = Path("logs/backtests/v27_v32_maxc6_2026_06_01/trades.csv")
    if not trades_csv.exists():
        print(f"ERROR: {trades_csv} not found", file=sys.stderr)
        sys.exit(1)
    attribution_report(trades_csv)
