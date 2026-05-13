"""Empirical slippage logger for live broker fills.

The paper-mode strategy backtest uses a static `slippage_bps` assumption
(15 bps adverse on both sides by default — see `core/execution.py`). That
number is a guess until we have real fills to compare against. This module
provides the seam where every live e2e fill gets appended to
`data/slippage_log.csv` in a single canonical schema, so once we accumulate
20+ datapoints we can finally calibrate paper-mode against reality.

Schema (`data/slippage_log.csv`)
--------------------------------
    timestamp_ist     ISO-8601 with +05:30 offset, time of fill detection
    symbol            e.g. YESBANK-EQ
    side              "BUY" | "SELL"
    limit_price       float, the LIMIT price we submitted
    ltp_at_decision   float, LTP captured right before submitting the order
    filled_price      float, broker-reported average fill price
    quantity          int, lot size that filled
    slippage_bps      float, signed; negative = favourable (we paid better
                      than mid), positive = adverse. Computed against
                      ltp_at_decision, NOT against limit_price, because
                      LTP is the closest available proxy for the true mid.
    source            free-form, e.g. "stage21" or "daemon_live"

Design choices
--------------
* CSV not SQLite: this is an append-only audit log, never queried hot.
  Tools/users can `pandas.read_csv` it ad hoc; no schema migration risk.
* No locking: we expect one writer at a time (Stage 2.1 finishes in
  100ms, daemon writes once per fill via _on_trade_closed). Worst case
  concurrent writes corrupt one row — not catastrophic.
* Header is written lazily on first append, never overwritten.
* The file lives at `data/slippage_log.csv` (gitignored, paper-only data).
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import pytz

IST = pytz.timezone("Asia/Kolkata")

ROOT = Path(__file__).resolve().parent.parent
SLIPPAGE_LOG_PATH = ROOT / "data" / "slippage_log.csv"

_HEADER = (
    "timestamp_ist",
    "symbol",
    "side",
    "limit_price",
    "ltp_at_decision",
    "filled_price",
    "quantity",
    "slippage_bps",
    "source",
)


def compute_slippage_bps(
    side: Literal["BUY", "SELL"],
    ltp_at_decision: float,
    filled_price: float,
) -> float:
    """Return signed slippage in basis points vs LTP-at-decision.

    Sign convention is **trader-perspective adversity**:
      - POSITIVE bps = adverse (we got a worse price than the prevailing LTP)
      - NEGATIVE bps = favourable (we got a better price than the prevailing LTP)

    Per-side rules (which way "worse" goes):
      - BUY  filled HIGHER than LTP -> adverse    (paid more) -> positive bps
      - BUY  filled LOWER  than LTP -> favourable (paid less) -> negative bps
      - SELL filled LOWER  than LTP -> adverse    (got less)  -> positive bps
      - SELL filled HIGHER than LTP -> favourable (got more)  -> negative bps

    Examples (Stage 2.1 actuals, 2026-05-13):
      compute_slippage_bps("BUY",  22.21, 22.20) -> -4.5 bps (favourable)
      compute_slippage_bps("SELL", 22.21, 22.19) -> +9.0 bps (adverse)
    """
    if ltp_at_decision <= 0:
        return 0.0
    raw_diff_bps = (filled_price - ltp_at_decision) / ltp_at_decision * 10_000.0
    # BUY: higher fill = adverse -> positive. Sign matches.
    # SELL: lower fill = adverse -> needs flipping.
    if side == "SELL":
        raw_diff_bps = -raw_diff_bps
    return round(raw_diff_bps, 2)


def append_fill(
    *,
    symbol: str,
    side: Literal["BUY", "SELL"],
    limit_price: float,
    ltp_at_decision: float,
    filled_price: float,
    quantity: int,
    source: str = "live",
    timestamp_ist: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> Path:
    """Append a single fill row. Returns the file path written."""
    target = Path(path) if path else SLIPPAGE_LOG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    ts = timestamp_ist or datetime.now(IST)
    if ts.tzinfo is None:
        ts = IST.localize(ts)

    slippage = compute_slippage_bps(side, ltp_at_decision, filled_price)

    row = {
        "timestamp_ist":   ts.isoformat(timespec="seconds"),
        "symbol":          symbol,
        "side":            side,
        "limit_price":     f"{limit_price:.4f}",
        "ltp_at_decision": f"{ltp_at_decision:.4f}",
        "filled_price":    f"{filled_price:.4f}",
        "quantity":        int(quantity),
        "slippage_bps":    f"{slippage:+.2f}",
        "source":          source,
    }

    existed = target.exists()
    with target.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_HEADER)
        if not existed:
            writer.writeheader()
        writer.writerow(row)
    return target


def summarize(path: Optional[Path] = None) -> dict:
    """Return a small dict summary of the log: count, mean bps per side,
    favourable vs adverse split. Used by the EOD profit-diagnostic report
    once n >= 5."""
    target = Path(path) if path else SLIPPAGE_LOG_PATH
    if not target.exists():
        return {"count": 0}

    rows: list[dict] = []
    with target.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                r["slippage_bps"] = float(r["slippage_bps"])
                rows.append(r)
            except Exception:
                continue

    if not rows:
        return {"count": 0}

    buys = [r for r in rows if r["side"] == "BUY"]
    sells = [r for r in rows if r["side"] == "SELL"]

    def _mean(xs):
        return round(sum(x["slippage_bps"] for x in xs) / len(xs), 2) if xs else 0.0

    return {
        "count": len(rows),
        "buy_count":  len(buys),
        "sell_count": len(sells),
        "mean_buy_bps":  _mean(buys),
        "mean_sell_bps": _mean(sells),
        "mean_all_bps":  _mean(rows),
        "favourable_count": sum(1 for r in rows if r["slippage_bps"] < 0),
        "adverse_count":    sum(1 for r in rows if r["slippage_bps"] > 0),
    }


if __name__ == "__main__":
    # CLI for ad-hoc inspection: `python tools/_slippage_logger.py`
    s = summarize()
    if s["count"] == 0:
        print("[slippage] log is empty — no live fills recorded yet")
    else:
        print(f"[slippage] {s['count']} fills logged")
        print(f"  BUY:  n={s['buy_count']}  mean={s['mean_buy_bps']:+.2f} bps")
        print(f"  SELL: n={s['sell_count']}  mean={s['mean_sell_bps']:+.2f} bps")
        print(f"  ALL:  n={s['count']}  mean={s['mean_all_bps']:+.2f} bps  "
              f"(favourable={s['favourable_count']}, adverse={s['adverse_count']})")
