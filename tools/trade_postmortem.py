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
SIGNAL_AUDIT_DIR = ROOT / "logs"

# 2026-05-13: anything above this gets a [LATE-ENTRY] flag. Daemon polls
# every 60s, so a healthy entry fires within the same or next cycle
# (<=120s). 5 min means we saw the opportunity, signal was rejected at
# least a few times, and only later did the ensemble flip to ACCEPTED --
# usually a sign of cooldown / opening_lockout / threshold hesitation
# eating away easy money. Surfacing it lets us tune the filters.
LATE_ENTRY_THRESHOLD_MIN = 5.0


def load_signal_audit(day_iso: str) -> Optional[pd.DataFrame]:
    """Load the signal_audit CSV for ``day_iso``. Returns None if absent
    (older days, or days predating the audit feature). The frame is
    lightly normalised: timestamps as tz-aware IST datetimes, columns
    we don't use stripped to keep memory low (~2k rows/day is typical
    but we read it once per report rather than per trade)."""
    path = SIGNAL_AUDIT_DIR / f"signal_audit_{day_iso}.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, usecols=[
        "timestamp", "symbol", "direction", "outcome", "reason",
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df = df.dropna(subset=["timestamp"])
    df["timestamp"] = df["timestamp"].dt.tz_convert(IST)
    return df


def compute_entry_lag(symbol: str, side: str, entry_dt: datetime,
                      audit_df: Optional[pd.DataFrame]) -> Optional[dict]:
    """Return {first_signal_dt, lag_min, signals_seen_count, rejected_count}.

    Semantics
    ---------
    The signal_audit logs every ensemble decision for a symbol+direction
    with an ``outcome`` of ACCEPTED (we entered), REJECTED (filter blocked
    us), or HOLD. We want to know: **how long was our system shouting
    "trade this!" before we actually entered?**

    Implementation note: an ACCEPTED row is logged at fill-confirmation
    time (a few hundred ms *after* the actual entry_time stored in the
    trades table), so we cannot use entry_dt < timestamp as a strict
    cutoff -- the very signal that birthed the trade would be excluded.

    Algorithm:
      1. Filter audit to (symbol, side).
      2. Find the *entry marker*: prefer the ACCEPTED row closest to
         entry_dt (any of: just-before / same-second / just-after fill).
         If absent (rare: restored position, manual entry, audit lost a
         row), fall back to entry_dt itself as the marker.
      3. lag_min = entry_marker - earliest matching signal of the day.
      4. signals_seen_count = number of matching rows at-or-before the
         marker. rejected_count = subset with outcome == REJECTED.

    Instant fills (single ACCEPTED row, no rejection trail) produce
    lag_min == 0.0 -- exactly what we want to display as "no late entry".
    """
    if audit_df is None or audit_df.empty:
        return None

    entry_aware = entry_dt if entry_dt.tzinfo else IST.localize(entry_dt)

    matches = audit_df[
        (audit_df["symbol"] == symbol) & (audit_df["direction"] == side)
    ].sort_values("timestamp")
    if matches.empty:
        return None

    accepted = matches[matches["outcome"] == "ACCEPTED"]
    if not accepted.empty:
        # Pick the ACCEPTED row closest in time to entry_dt -- handles the
        # case of multiple ACCEPTED rows (rare: same-day re-entry after
        # exit, e.g. CYIENT 13:30 + 13:50 on 2026-05-08).
        deltas = (accepted["timestamp"] - entry_aware).abs()
        entry_marker = accepted.loc[deltas.idxmin(), "timestamp"]
    else:
        entry_marker = entry_aware

    pre_entry = matches[matches["timestamp"] <= entry_marker]
    if pre_entry.empty:
        return None

    first_dt = pre_entry.iloc[0]["timestamp"].to_pydatetime()
    entry_marker_dt = (entry_marker.to_pydatetime()
                       if hasattr(entry_marker, "to_pydatetime")
                       else entry_marker)
    lag_min = (entry_marker_dt - first_dt).total_seconds() / 60.0
    rejected = int((pre_entry["outcome"] == "REJECTED").sum())
    return {
        "first_signal_dt": first_dt,
        "lag_min": max(0.0, lag_min),
        "signals_seen_count": int(len(pre_entry)),
        "rejected_count": rejected,
    }


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


def analyse_trade(trade: dict,
                  audit_df: Optional[pd.DataFrame] = None) -> dict:
    """Returns enriched dict with MFE / MAE / flags.

    ``audit_df`` is the day's signal_audit DataFrame (already loaded once
    at the caller level). When provided, we attach entry-lag stats and
    raise a ``[LATE-ENTRY]`` flag when the lag exceeds
    ``LATE_ENTRY_THRESHOLD_MIN``.
    """
    entry_dt = parse_dt(trade["entry_time"])
    exit_dt = parse_dt(trade["exit_time"])
    qty = trade["quantity"]
    entry = trade["entry_price"]
    exit_p = trade["exit_price"]
    side = trade["side"]
    pnl_actual = trade["pnl"]

    bars = fetch_intraday_bars(trade["symbol"], entry_dt, exit_dt)
    daily = fetch_daily_context(trade["symbol"])
    entry_lag = compute_entry_lag(trade["symbol"], side, entry_dt, audit_df)

    result = dict(trade)
    result["entry_dt"] = entry_dt
    result["exit_dt"] = exit_dt
    result["holding_minutes"] = (exit_dt - entry_dt).total_seconds() / 60
    result["session_count"] = max(1, (exit_dt.date() - entry_dt.date()).days + 1)
    result["entry_lag"] = entry_lag

    if bars is None or bars.empty:
        result["mfe"] = None
        result["mae"] = None
        result["capture_pct"] = None
        result["best_bar_time"] = None
        result["worst_bar_time"] = None
        result["flags"] = ["[NO-BARS]"]
        if entry_lag and entry_lag["lag_min"] > LATE_ENTRY_THRESHOLD_MIN:
            result["flags"].append("[LATE-ENTRY]")
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
    if entry_lag and entry_lag["lag_min"] > LATE_ENTRY_THRESHOLD_MIN:
        flags.append("[LATE-ENTRY]")
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

    lag_vals = [a["entry_lag"]["lag_min"] for a in analyses
                if a.get("entry_lag")]
    late_entry_count = sum(1 for a in analyses
                           if "[LATE-ENTRY]" in a.get("flags", []))
    lines += [
        f"**Trades closed:** {len(analyses)}",
        f"**Realised PnL:**  Rs {total_pnl:+,.2f}",
        f"**Sum of MFE:**    Rs {total_mfe:+,.2f}  (theoretical max if perfect exits)",
        f"**Money on table:** Rs {total_table:+,.2f}  (MFE - actual gross PnL)",
        f"**Avg MFE capture:** {avg_capture:.1f}%",
    ]
    if lag_vals:
        lag_sorted = sorted(lag_vals)
        median_lag = lag_sorted[len(lag_sorted) // 2]
        max_lag = max(lag_vals)
        lines.append(
            f"**Entry lag:** median {median_lag:.1f} min, max {max_lag:.1f} min, "
            f"[LATE-ENTRY] flags: {late_entry_count}/{len(analyses)} "
            f"(threshold {LATE_ENTRY_THRESHOLD_MIN:.0f} min)"
        )
    elif any(a.get("entry_lag") is None for a in analyses):
        lines.append(
            "**Entry lag:** _no signal_audit data for these trades_"
        )
    lines += ["", "---", ""]

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
        if a.get("entry_lag"):
            el = a["entry_lag"]
            first_str = el["first_signal_dt"].strftime("%H:%M")
            extra = ""
            if el["signals_seen_count"] > 1:
                extra = (f"  ({el['signals_seen_count']} matching signals seen, "
                         f"{el['rejected_count']} rejected before entry)")
            lines.append(
                f"- **Entry lag:** {el['lag_min']:.1f} min "
                f"(first {a['side']} signal at {first_str}){extra}"
            )
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
        audit_df = load_signal_audit(day)
        if audit_df is None:
            print(f"  (no signal_audit_{day}.csv -> entry-lag stats disabled)")
        analyses = []
        for t in trades:
            print(f"  Analysing {t['symbol']} {t['side']}...", end=" ", flush=True)
            try:
                analyses.append(analyse_trade(t, audit_df=audit_df))
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
