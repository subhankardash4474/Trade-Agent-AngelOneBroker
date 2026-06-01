"""Sector classifier for the V4 Mode A swing_cash universe (75 instruments).

Used by ``tools/v27_backtest_2026_06_01.py --sector-cap N`` to enforce
charter §3.6's "max N positions per sector" rule on the candidate-entry
gate. Without this cap, the standalone backtester's V32 result was
heavily concentrated in commodity / energy / Adani-group exposures
(top winners + top losers both cluster there — concentration risk
flagged in Phase 10 attribution).

Sector assignments follow NSE GICS-style buckets with a few practical
collapses (Conglomerate-Adani groups Adani-family separately because
their joint exposure was a top-10 concentration in V32). ETFs get
their own buckets so the cap doesn't conflate a broad-market ETF
with one of its constituents.

This file lives in `packages/core/` because:
  - It's pure data + helpers (no I/O, no live state)
  - Both `tools/` (standalone backtester) and `packages/strategies/`
    (V27 strategy code) need to import it
  - Pod boundary: `strategies` and `tools` can both import `core`
"""
from __future__ import annotations

from typing import Dict, Set

# Sector mapping — keep alphabetical within each bucket for diff-ability.
# Source: NSE sector classifications + manual review of v27_v32 trades.
# When in doubt about a stock's primary sector, the bucket that captures
# the trader's mental model of "won't be hedged by adding another" wins.
SECTORS: Dict[str, str] = {
    # ── Banking & Diversified Financials ─────────────────────────────
    "AXISBANK": "financials",
    "BAJAJFINSV": "financials",
    "BAJFINANCE": "financials",
    "BANDHANBNK": "financials",
    "CHOLAFIN": "financials",
    "HDFCAMC": "financials",
    "HDFCBANK": "financials",
    "HDFCLIFE": "financials",
    "ICICIBANK": "financials",
    "ICICIGI": "financials",
    "ICICIPRULI": "financials",
    "INDUSINDBK": "financials",
    "KOTAKBANK": "financials",
    "MUTHOOTFIN": "financials",
    "SBILIFE": "financials",
    "SBIN": "financials",
    "SHRIRAMFIN": "financials",

    # ── Information Technology ───────────────────────────────────────
    "HCLTECH": "it",
    "INFY": "it",
    "LTIM": "it",
    "MPHASIS": "it",
    "PERSISTENT": "it",
    "TCS": "it",
    "TECHM": "it",
    "WIPRO": "it",

    # ── FMCG / Consumer Staples ──────────────────────────────────────
    "BRITANNIA": "fmcg",
    "COLPAL": "fmcg",
    "DABUR": "fmcg",
    "DMART": "fmcg",
    "GODREJCP": "fmcg",
    "HINDUNILVR": "fmcg",
    "ITC": "fmcg",
    "MARICO": "fmcg",
    "NESTLEIND": "fmcg",
    "TATACONSUM": "fmcg",
    "VBL": "fmcg",

    # ── Pharma & Healthcare ──────────────────────────────────────────
    "APOLLOHOSP": "pharma",
    "BIOCON": "pharma",
    "CIPLA": "pharma",
    "DIVISLAB": "pharma",
    "DRREDDY": "pharma",
    "MAXHEALTH": "pharma",
    "SUNPHARMA": "pharma",
    "ZYDUSLIFE": "pharma",

    # ── Auto & Auto Components ───────────────────────────────────────
    "ASHOKLEY": "auto",
    "BAJAJ-AUTO": "auto",
    "BOSCHLTD": "auto",
    "EICHERMOT": "auto",
    "HEROMOTOCO": "auto",
    "M&M": "auto",
    "MARUTI": "auto",
    "TATAMOTORS": "auto",
    "TVSMOTOR": "auto",

    # ── Energy / Oil & Gas / Power ──────────────────────────────────
    "BPCL": "energy",
    "COALINDIA": "energy",
    "GAIL": "energy",
    "HINDPETRO": "energy",
    "IOC": "energy",
    "NTPC": "energy",
    "ONGC": "energy",
    "POWERGRID": "energy",
    "RELIANCE": "energy",
    "TATAPOWER": "energy",

    # ── Metals & Mining ──────────────────────────────────────────────
    "HINDALCO": "metals",
    "JINDALSTEL": "metals",
    "JSWSTEEL": "metals",
    "NMDC": "metals",
    "SAIL": "metals",
    "TATASTEEL": "metals",
    "VEDL": "metals",

    # ── Cement & Construction Materials ──────────────────────────────
    "ACC": "cement",
    "AMBUJACEMENT": "cement",
    "GRASIM": "cement",       # Aditya Birla cement-led conglomerate
    "SHREECEM": "cement",
    "ULTRACEMCO": "cement",

    # ── Telecom ──────────────────────────────────────────────────────
    "BHARTIARTL": "telecom",
    "IDEA": "telecom",

    # ── Consumer Durables / Paints / Specialty ──────────────────────
    "ASIANPAINT": "consumer_durables",
    "BAJAJELECTR": "consumer_durables",
    "BERGEPAINT": "consumer_durables",
    "HAVELLS": "consumer_durables",
    "PIDILITIND": "consumer_durables",
    "TITAN": "consumer_durables",

    # ── Adani group (own bucket — concentration risk flagged in V32) ─
    "ADANIENT": "adani_group",
    "ADANIGREEN": "adani_group",
    "ADANIPORTS": "adani_group",
    "ADANIPOWER": "adani_group",

    # ── Capital Goods / Defense / Engineering ────────────────────────
    "ABB": "capital_goods",
    "BEL": "capital_goods",
    "HAL": "capital_goods",
    "LT": "capital_goods",
    "SIEMENS": "capital_goods",

    # ── Real Estate ─────────────────────────────────────────────────
    "DLF": "realestate",
    "GODREJPROP": "realestate",

    # ── ETFs (each in its own bucket so the sector cap doesn't
    #         double-count a broad ETF and its constituents) ─────────
    "NIFTYBEES": "etf_broad_market",
    "JUNIORBEES": "etf_broad_market",
    "NIFTYIETF": "etf_broad_market",
    "BANKBEES": "etf_sector_bank",
    "ITBEES": "etf_sector_it",
    "PSUBNKBEES": "etf_sector_psu_bank",
    "AUTOBEES": "etf_sector_auto",
    "CPSEETF": "etf_sector_psu",
    "GOLDBEES": "etf_commodity_gold",
    "SILVERBEES": "etf_commodity_silver",
    "LIQUIDBEES": "etf_debt",
    "GILT5YBEES": "etf_debt",
}


def sector_for(symbol: str) -> str:
    """Return the sector bucket for a symbol.

    Unknown symbols return ``"unknown"`` so the sector cap NEVER
    silently passes a new instrument added later to the universe
    without first updating this map. Callers can choose to treat
    "unknown" as "no cap" or "always blocked" per their semantics.
    """
    return SECTORS.get(symbol.upper(), "unknown")


def sectors_in_universe(symbols: list[str]) -> Dict[str, list[str]]:
    """Return {sector: [symbols]} grouping for a universe, for sanity
    checks. Diagnostic / tooling helper, not used by the backtester."""
    out: Dict[str, list[str]] = {}
    for s in symbols:
        sec = sector_for(s)
        out.setdefault(sec, []).append(s)
    for sec in out:
        out[sec].sort()
    return out


__all__ = ["SECTORS", "sector_for", "sectors_in_universe"]
