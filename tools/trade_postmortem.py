"""Per-trade post-mortem.

For every closed trade in a given day (default today), pulls the 5-min bars
between entry and exit and computes:

  MFE  - Maximum Favourable Excursion: the best-case unrealised gain we
         saw during the trade (capped by bar resolution).
  MAE  - Maximum Adverse Excursion: the worst-case unrealised loss we
         saw during the trade (i.e. how close we got to SL).
  capture_pct - what fraction of the MFE we actually captured at exit.

Then it overlays the daily-trend context (50d SMA distance, 30d return)
and flags issues:

  [LATE EXIT]      Captured < 60% of MFE (we exited well after the peak).
  [TREND MISMATCH] Trade direction fights the 50d SMA by >5% margin.
  [TIGHT TP]       Bar lows/highs never touched our TP.
  [NEAR-SL HOLD]   Trade survived a touch within 0.2 ATR of SL.
  [CARRYOVER]      Position held overnight (>1 trading session).

Output: rich markdown report saved to logs/postmortem/<date>.md

Usage:
  python tools/trade_postmortem.py            # today, all closed trades
  python tools/trade_postmortem.py 2026-05-07
  python tools/trade_postmortem.py --range 2026-05-01 2026-05-07
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = pytz.timezone("Asia/Kolkata")
DB_PATH = ROOT / "data" / "trading_agent.db"
OUTPUT_DIR = ROOT / "logs" / "postmortem"


def load_trades(day_iso: str) -> list[dict]:
    """Load all trades that exited on day_iso. Day boundary is local IST."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT symbol, side, entry_price, exit_price, quantity, "
            "entry_time, exit_time, pnl, pnl_pct, strategy, exit_reason, "
            "commission FROM trades WHERE exit_time LIKE ? ORDER BY exit_time",
            (f"{day_iso}%",),
        ).fetchall()
    finally:
        conn.close()
    cols = ["symbol", "side", "entry_price", "exit_price", "quantity",
            "entry_time", "exit_time", "pnl", "pnl_pct", "strategy",
            "exit_reason", "commission"]
    return [dict(zip(cols, r)) for r in rows]


def fetch_intraday_bars(symbol: str, entry_dt: datetime, exit_dt: datetime) -> Optional[pd.DataFrame]:
    """Fetch 5-min bars covering the trade's lifetime. Yahoo limits 5m to ~60d."""
    days_back = max(2, (datetime.now(IST).date() - entry_dt.date()).days + 2)
    days_back = min(days_back, 59)
    period = f"{days_back}d"
    df = yf.download(f"{symbol}.NS", period=period, interval="5m",
                     progress=False, auto_adjust=False)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)

    entry_aware = entry_dt if entry_dt.tzinfo else IST.localize(entry_dt)
    exit_aware = exit_dt if exit_dt.tzinfo else IST.localize(exit_dt)
    mask = (df.index >= entry_aware - pd.Timedelta(minutes=5)) & \
           (df.index <= exit_aware + pd.Timedelta(minutes=5))
    return df.loc[mask].copy()


def fetch_daily_context(symbol: str) -> Optional[dict]:
    """50d SMA, 30d return for trend-mismatch flagging."""
    df = yf.download(f"{symbol}.NS", period="3mo", interval="1d",
                     progress=False, auto_adjust=False)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    closes = df["Close"]
    if len(closes) < 30:
        return None
    last = float(closes.iloc[-1])
    sma50 = float(closes.rolling(min(50, len(closes))).mean().iloc[-1])
    pct_30d = (closes.iloc[-1] / closes.iloc[-30] - 1) * 100
    return {
        "last_close": last,
        "sma50": sma50,
        "pct_vs_sma50": (last / sma50 - 1) * 100,
        "ret_30d": float(pct_30d),
    }


def parse_dt(s: str) -> datetime:
    if "T" in s:
        s = s.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def analyse_trade(trade: dict) -> dict:
    """Returns enriched dict with MFE / MAE / flags."""
    entry_dt = parse_dt(trade["entry_time"])
    exit_dt = parse_dt(trade["exit_time"])
    qty = trade["quantity"]
    entry = trade["entry_price"]
    exit_p = trade["exit_price"]
    side = trade["side"]
    pnl_actual = trade["pnl"]

    bars = fetch_intraday_bars(trade["symbol"], entry_dt, exit_dt)
    daily = fetch_daily_context(trade["symbol"])

    result = dict(trade)
    result["entry_dt"] = entry_dt
    result["exit_dt"] = exit_dt
    result["holding_minutes"] = (exit_dt - entry_dt).total_seconds() / 60
    result["session_count"] = max(1, (exit_dt.date() - entry_dt.date()).days + 1)

    if bars is None or bars.empty:
        result["mfe"] = None
        result["mae"] = None
        result["capture_pct"] = None
        result["best_bar_time"] = None
        result["worst_bar_time"] = None
        result["flags"] = ["[NO-BARS]"]
        result["daily"] = daily
        return result

    if side == "BUY":
        mfe_price = float(bars["High"].max())
        mae_price = float(bars["Low"].min())
        mfe_value = (mfe_price - entry) * qty
        mae_value = (mae_price - entry) * qty
        best_bar_time = bars["High"].idxmax()
        worst_bar_time = bars["Low"].idxmin()
    else:
        mfe_price = float(bars["Low"].min())
        mae_price = float(bars["High"].max())
        mfe_value = (entry - mfe_price) * qty
        mae_value = (entry - mae_price) * qty
        best_bar_time = bars["Low"].idxmin()
        worst_bar_time = bars["High"].idxmax()

    pnl_gross = (entry - exit_p) * qty if side == "SELL" else (exit_p - entry) * qty
    capture_pct = (pnl_gross / mfe_value * 100) if mfe_value > 0 else 0.0

    flags = []
    if mfe_value > 0 and capture_pct < 60:
        flags.append("[LATE-EXIT]")
    if daily and daily.get("pct_vs_sma50") is not None:
        if side == "SELL" and daily["pct_vs_sma50"] > 5:
            flags.append("[TREND-MISMATCH-SHORT]")
        elif side == "BUY" and daily["pct_vs_sma50"] < -5:
            flags.append("[TREND-MISMATCH-LONG]")
    if mae_value < 0:
        sl_distance_per_share = abs(mfe_price - mae_price) / max(qty, 1)
        if mae_value < -0.5 * abs(pnl_actual) and pnl_actual > 0:
            flags.append("[NEAR-SL-RECOVERY]")
    if result["session_count"] > 1:
        flags.append("[CARRYOVER]")

    result.update({
        "mfe": mfe_value,
        "mae": mae_value,
        "mfe_price": mfe_price,
        "mae_price": mae_price,
        "best_bar_time": best_bar_time,
        "worst_bar_time": worst_bar_time,
        "capture_pct": capture_pct,
        "pnl_gross": pnl_gross,
        "money_on_table": mfe_value - pnl_gross,
        "flags": flags,
        "daily": daily,
    })
    return result


def render_report(day_iso: str, analyses: list[dict]) -> str:
    lines = [f"# Trade Post-Mortem - {day_iso}", ""]
    if not analyses:
        lines.append("_No trades closed on this day._")
        return "\n".join(lines)

    total_pnl = sum(a["pnl"] for a in analyses)
    total_mfe = sum(a["mfe"] for a in analyses if a.get("mfe") is not None)
    total_table = sum(a["money_on_table"] for a in analyses if a.get("money_on_table") is not None)
    avg_capture = sum(a["capture_pct"] for a in analyses if a.get("capture_pct") is not None) / max(len([a for a in analyses if a.get("capture_pct") is not None]), 1)

    lines += [
        f"**Trades closed:** {len(analyses)}",
        f"**Realised PnL:**  Rs {total_pnl:+,.2f}",
        f"**Sum of MFE:**    Rs {total_mfe:+,.2f}  (theoretical max if perfect exits)",
        f"**Money on table:** Rs {total_table:+,.2f}  (MFE - actual gross PnL)",
        f"**Avg MFE capture:** {avg_capture:.1f}%",
        "",
        "---",
        "",
    ]

    for a in analyses:
        sym = a["symbol"]
        side = a["side"]
        flags = " ".join(a.get("flags", []))
        lines.append(f"## {sym} {side}  {flags}")
        lines.append("")
        lines += [
            f"- **Strategy:** `{a['strategy']}` -> exit reason `{a['exit_reason']}`",
            f"- **Entry:** {a['entry_price']:.2f} @ {a['entry_dt'].strftime('%Y-%m-%d %H:%M')}",
            f"- **Exit:**  {a['exit_price']:.2f} @ {a['exit_dt'].strftime('%Y-%m-%d %H:%M')}  ({a['holding_minutes']:.0f} min held, {a['session_count']} session{'s' if a['session_count']>1 else ''})",
            f"- **Realised:** Rs {a['pnl']:+,.2f} ({a['pnl_pct']:+.2f}%)  |  commission Rs {a['commission']:.2f}",
        ]
        if a.get("mfe") is not None:
            lines += [
                f"- **MFE:** Rs {a['mfe']:+,.2f} at price {a['mfe_price']:.2f} @ {a['best_bar_time'].strftime('%H:%M')}",
                f"- **MAE:** Rs {a['mae']:+,.2f} at price {a['mae_price']:.2f} @ {a['worst_bar_time'].strftime('%H:%M')}",
                f"- **Capture:** {a['capture_pct']:.1f}% of MFE  |  **money on table:** Rs {a['money_on_table']:+,.2f}",
            ]
        if a.get("daily"):
            d = a["daily"]
            lines += [
                f"- **Daily context:** close {d['last_close']:.2f}  |  50d SMA {d['sma50']:.2f}  ({d['pct_vs_sma50']:+.1f}%)  |  30d ret {d['ret_30d']:+.1f}%",
            ]
        if not a.get("flags"):
            lines.append(f"- _No flags - clean trade._")
        lines.append("")

    by_strategy: dict[str, list[dict]] = {}
    for a in analyses:
        by_strategy.setdefault(a["strategy"], []).append(a)
    if len(by_strategy) > 1:
        lines += ["---", "", "## Strategy roll-up", ""]
        lines.append("| Strategy | Trades | Win | Loss | PnL | Avg MFE capture | Money on table |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for strat, arr in by_strategy.items():
            wins = sum(1 for x in arr if x["pnl"] > 0)
            losses = sum(1 for x in arr if x["pnl"] < 0)
            pnl_sum = sum(x["pnl"] for x in arr)
            cap_avg = sum(x["capture_pct"] for x in arr if x.get("capture_pct") is not None) / max(len([x for x in arr if x.get("capture_pct") is not None]), 1)
            mot = sum(x["money_on_table"] for x in arr if x.get("money_on_table") is not None)
            lines.append(f"| `{strat}` | {len(arr)} | {wins} | {losses} | Rs {pnl_sum:+.2f} | {cap_avg:.1f}% | Rs {mot:+.2f} |")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("day", nargs="?",
                        default=datetime.now(IST).strftime("%Y-%m-%d"))
    parser.add_argument("--range", nargs=2, metavar=("FROM", "TO"))
    args = parser.parse_args()

    days = [args.day]
    if args.range:
        start = datetime.strptime(args.range[0], "%Y-%m-%d").date()
        end = datetime.strptime(args.range[1], "%Y-%m-%d").date()
        days = []
        d = start
        while d <= end:
            days.append(d.isoformat())
            d += timedelta(days=1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for day in days:
        trades = load_trades(day)
        print(f"[{day}] {len(trades)} closed trade(s)")
        if not trades:
            continue
        analyses = []
        for t in trades:
            print(f"  Analysing {t['symbol']} {t['side']}...", end=" ", flush=True)
            try:
                analyses.append(analyse_trade(t))
                print("OK")
            except Exception as e:
                print(f"FAILED: {e}")
        report = render_report(day, analyses)
        out = OUTPUT_DIR / f"{day}.md"
        out.write_text(report, encoding="utf-8")
        print(f"  -> wrote {out}")
        print()
        # Also stdout for immediate viewing
        print(report)


if __name__ == "__main__":
    main()
