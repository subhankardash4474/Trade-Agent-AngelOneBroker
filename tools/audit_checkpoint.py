"""
Audit Checkpoint
================
Captures a comprehensive snapshot of the trading agent's runtime state at a
single point in time. Designed to be called periodically by the daemon's main
loop so the latest state is always available on disk for inspection.

Outputs:
    logs/audit/YYYY-MM-DD/checkpoint_HHMM.md   ← human/agent-readable
    logs/audit/YYYY-MM-DD/checkpoint_HHMM.json ← structured/machine-readable

The module is deliberately self-contained:
  - Reads agent state via the same Database/Portfolio/RiskManager objects the
    daemon already has (passed in by the caller).
  - Reads the live agent log file directly with file IO so the snapshot
    captures real wall-clock state (not just in-memory).
  - Never raises during normal operation — failures degrade to "[ERROR]"
    sections in the output rather than crashing the daemon.

Each checkpoint includes a delta vs the previous checkpoint of the same day,
so the consumer (a Cursor agent reading these files) can immediately see what
changed in the last hour.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil
import pytz

IST = pytz.timezone("Asia/Kolkata")
LOG_DIR = Path("logs")
AUDIT_ROOT = LOG_DIR / "audit"

# Columns we want to surface from open_positions for quick visual scan.
_POSITION_COLS = (
    "symbol", "side", "entry_price", "quantity",
    "stop_loss", "take_profit", "strategy", "regime",
    "contributing_strategies", "entry_time",
)


# ── ANSI / unicode stripping ─────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ── Helpers: filesystem layout ───────────────────────────────────────────

def _today_dir(now: Optional[datetime] = None) -> Path:
    now = now or datetime.now(IST)
    d = AUDIT_ROOT / now.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _checkpoint_paths(now: datetime) -> Tuple[Path, Path]:
    base = _today_dir(now) / f"checkpoint_{now.strftime('%H%M')}"
    return base.with_suffix(".md"), base.with_suffix(".json")


def _previous_checkpoint(now: datetime) -> Optional[Path]:
    """Latest checkpoint *before* `now` from today, for delta computation."""
    d = _today_dir(now)
    candidates = sorted(d.glob("checkpoint_*.json"))
    target_stem = f"checkpoint_{now.strftime('%H%M')}"
    prior = [p for p in candidates if p.stem < target_stem]
    return prior[-1] if prior else None


# ── Helpers: log scanning ────────────────────────────────────────────────

def _agent_log_path(now: datetime) -> Path:
    return LOG_DIR / f"trading_agent_{now.strftime('%Y-%m-%d')}.log"


def _read_log_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [_strip_ansi(line.rstrip("\n")) for line in f]
    except Exception:
        return []


def _line_ts(line: str) -> Optional[datetime]:
    """Parse the leading 'YYYY-MM-DD HH:MM:SS' timestamp from a log line."""
    if len(line) < 19:
        return None
    try:
        ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        return IST.localize(ts)
    except Exception:
        return None


def _filter_window(lines: List[str], since: datetime, until: datetime) -> List[str]:
    """Return lines whose leading timestamp is in [since, until)."""
    out: List[str] = []
    for ln in lines:
        t = _line_ts(ln)
        if t is None:
            continue
        if since <= t < until:
            out.append(ln)
    return out


# ── Section builders ─────────────────────────────────────────────────────

def _find_running_daemon_pid() -> Optional[int]:
    """Locate the trading daemon process by command line.

    The CLI used to fall back to `os.getpid()` which captured the audit
    script's own PID — making manual checkpoints look like they came from
    a 0-uptime "daemon". We scan psutil instead and return the first
    Python process whose cmdline contains `run_daemon.py`. Returns None
    if no such process is running (which is itself meaningful info).
    """
    try:
        my_pid = os.getpid()
        for p in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
            try:
                if p.info["pid"] == my_pid:
                    continue
                name = (p.info.get("name") or "").lower()
                if "python" not in name:
                    continue
                cmd = p.info.get("cmdline") or []
                if any("run_daemon.py" in str(arg) for arg in cmd):
                    return int(p.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return None


def _section_health(daemon_pid: Optional[int]) -> Dict[str, Any]:
    """Daemon process snapshot."""
    res: Dict[str, Any] = {"pid": daemon_pid, "alive": False}
    if daemon_pid is None:
        res["note"] = "no running daemon detected (run_daemon.py not found in process list)"
        return res
    try:
        p = psutil.Process(daemon_pid)
        with p.oneshot():
            res.update({
                "alive": p.is_running(),
                "ram_mb": int(p.memory_info().rss / (1024 * 1024)),
                "cpu_seconds": round(sum(p.cpu_times()[:2]), 1),
                "threads": p.num_threads(),
                "handles": getattr(p, "num_handles", lambda: None)(),
                "uptime_minutes": round(
                    (datetime.now() - datetime.fromtimestamp(p.create_time())).total_seconds() / 60.0,
                    1,
                ),
                "status": p.status(),
            })
    except Exception as e:
        res["error"] = str(e)
    return res


def _section_log_anomalies(lines: List[str], since: datetime, until: datetime) -> Dict[str, Any]:
    """Errors/warnings within the window. We surface up to 10 of each verbatim."""
    window = _filter_window(lines, since, until)
    errors = [l for l in window if "| ERROR" in l or "| CRITICAL" in l]
    warnings = [l for l in window if "| WARNING" in l]
    tracebacks = [l for l in window if "Traceback (most recent call last)" in l]
    # Group warnings by the first ~80 chars of message body to highlight repeats
    warn_bodies = []
    for ln in warnings:
        m = re.search(r"\| WARNING\s+\| (.+)$", ln)
        if m:
            body = m.group(1)
            warn_bodies.append(body[:80])
    warn_groups = Counter(warn_bodies).most_common(10)
    return {
        "error_count": len(errors),
        "warning_count": len(warnings),
        "traceback_count": len(tracebacks),
        "errors_sample": errors[:10],
        "warnings_top_groups": [{"count": c, "sample": s} for s, c in warn_groups],
    }


def _section_positions(db_path: str) -> Dict[str, Any]:
    """Read the live DB for currently open positions."""
    res: Dict[str, Any] = {"open_count": 0, "positions": [], "round_trip_ok": True, "round_trip_errors": []}
    c = None
    try:
        c = sqlite3.connect(db_path)
        rows = list(c.execute(
            "SELECT symbol, side, entry_price, quantity, stop_loss, take_profit, "
            "strategy, regime, contributing_strategies, entry_time "
            "FROM open_positions"
        ))
        for r in rows:
            sym, side, ep, qty, sl, tp, strat, reg, contrib_s, et = r
            try:
                contrib = json.loads(contrib_s or "{}")
                non_float = [
                    (k, type(v).__name__) for k, v in contrib.items()
                    if not isinstance(v, (int, float))
                ]
                if non_float:
                    res["round_trip_ok"] = False
                    res["round_trip_errors"].append({"symbol": sym, "non_float": non_float})
            except Exception as e:
                res["round_trip_ok"] = False
                res["round_trip_errors"].append({"symbol": sym, "error": str(e)})
                contrib = {}
            res["positions"].append({
                "symbol": sym, "side": side, "qty": qty,
                "entry_price": ep, "stop_loss": sl, "take_profit": tp,
                "strategy": strat, "regime": reg,
                "contributing_strategies": contrib,
                "entry_time": et,
            })
        res["open_count"] = len(rows)
    except Exception as e:
        res["error"] = str(e)
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
    return res


def _section_trades(db_path: str, since: datetime) -> Dict[str, Any]:
    """Trades closed since the previous checkpoint window started."""
    res: Dict[str, Any] = {"closed_count": 0, "trades": [], "realised_pnl": 0.0}
    c = None
    try:
        c = sqlite3.connect(db_path)
        rows = list(c.execute(
            "SELECT symbol, side, entry_price, exit_price, quantity, pnl, "
            "exit_reason, entry_time, exit_time "
            "FROM trades "
            "WHERE exit_time >= ? "
            "ORDER BY exit_time ASC",
            (since.isoformat(),)
        ))
        for r in rows:
            sym, side, ep, xp, qty, pnl, reason, et, xt = r
            res["trades"].append({
                "symbol": sym, "side": side, "qty": qty,
                "entry": ep, "exit": xp, "pnl": pnl,
                "reason": reason, "entry_time": et, "exit_time": xt,
            })
            res["realised_pnl"] += pnl or 0.0
        res["closed_count"] = len(rows)
        res["realised_pnl"] = round(res["realised_pnl"], 2)
    except Exception as e:
        res["error"] = str(e)
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
    return res


def _section_day_pnl(db_path: str, today: datetime) -> Dict[str, Any]:
    """Day-to-date realised P&L + latest equity snapshot."""
    res: Dict[str, Any] = {}
    c = None
    try:
        c = sqlite3.connect(db_path)
        # All trades closed today
        day_start = today.replace(hour=0, minute=0, second=0, microsecond=0)
        trades_today = list(c.execute(
            "SELECT pnl, exit_reason FROM trades WHERE exit_time >= ?",
            (day_start.isoformat(),)
        ))
        res["closed_trades_today"] = len(trades_today)
        res["realised_pnl_today"] = round(sum((p or 0.0) for p, _ in trades_today), 2)
        winners = sum(1 for p, _ in trades_today if (p or 0) > 0)
        losers = sum(1 for p, _ in trades_today if (p or 0) < 0)
        res["winners"] = winners
        res["losers"] = losers
        res["win_rate"] = round(winners / max(1, len(trades_today)) * 100, 1)
        # Exit reasons breakdown
        res["exit_reasons"] = dict(Counter([r for _, r in trades_today]))
        # Latest equity
        try:
            last = c.execute(
                "SELECT timestamp, equity, cash FROM equity_curve "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last:
                res["latest_equity_snapshot"] = {
                    "timestamp": last[0], "equity": round(last[1], 2),
                    "cash": round(last[2], 2),
                }
        except Exception:
            pass
    except Exception as e:
        res["error"] = str(e)
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
    return res


def _section_signal_pipeline(lines: List[str], since: datetime, until: datetime) -> Dict[str, Any]:
    """Cycle digests, ensemble actions, gate rejections in window."""
    win = _filter_window(lines, since, until)
    cycle_digests: List[Dict[str, Any]] = []
    for ln in win:
        if "[CYCLE-DIGEST]" not in ln:
            continue
        m = re.search(
            r"directional_votes=(\d+) ensemble_acts=(\d+) ensemble_holds=(\d+) "
            r"threshold=([\d.]+) regime=(\w+)",
            ln,
        )
        if m:
            cycle_digests.append({
                "ts": ln[:19],
                "votes": int(m.group(1)),
                "acts": int(m.group(2)),
                "holds": int(m.group(3)),
                "threshold": float(m.group(4)),
                "regime": m.group(5),
            })
    ensemble_acts = [l for l in win if re.search(r"\[ENSEMBLE\] (BUY|SELL)", l)]
    gate_rejections = Counter()
    for ln in win:
        for gate in ("ATR-GATE", "NOTIONAL-FLOOR", "REJECTED", "rejection_cooldown",
                     "Skipping.*sector", "max_open_positions", "circuit_proximity"):
            if re.search(rf"\[{gate}\]" if gate.isupper() else gate, ln):
                gate_rejections[gate.replace(".*", "")] += 1
    return {
        "cycles_completed": len(cycle_digests),
        "avg_directional_votes": round(
            sum(c["votes"] for c in cycle_digests) / max(1, len(cycle_digests)), 1
        ),
        "total_ensemble_acts": sum(c["acts"] for c in cycle_digests),
        "ensemble_actions_sample": ensemble_acts[-10:],
        "gate_rejections": dict(gate_rejections),
        "current_regime": cycle_digests[-1]["regime"] if cycle_digests else None,
        "current_threshold": cycle_digests[-1]["threshold"] if cycle_digests else None,
    }


def _section_xgb_firing(lines: List[str], since: datetime, until: datetime) -> Dict[str, Any]:
    """XGBoost SELL/BUY/HOLD distribution within window."""
    win = _filter_window(lines, since, until)
    buy = sum(1 for l in win if "[xgboost_classifier] BUY" in l)
    sell = sum(1 for l in win if "[xgboost_classifier] SELL" in l)
    hold = sum(1 for l in win if "xgboost_classifier=HOLD" in l)
    total = buy + sell + hold
    return {
        "buy": buy, "sell": sell, "hold": hold,
        "firing_rate_pct": round((buy + sell) / max(1, total) * 100, 1),
        "sell_buy_ratio": round(sell / max(1, buy), 1) if buy else None,
    }


def _section_risk_state(lines: List[str], since: datetime, until: datetime) -> Dict[str, Any]:
    """Pull the latest HEARTBEAT line from window for at-a-glance risk state."""
    win = _filter_window(lines, since, until)
    hb_lines = [l for l in win if "[HEARTBEAT]" in l]
    if not hb_lines:
        # Fall back: any heartbeat from today
        hb_lines = [l for l in lines if "[HEARTBEAT]" in l]
    if not hb_lines:
        return {"heartbeat_seen": False}
    last = hb_lines[-1]
    # Heartbeat sometimes renders the rupee glyph as "₹", sometimes as "?"
    # depending on the console/file encoding stripping path. The currency
    # marker is irrelevant to the value so make it permissive.
    cur = r"[?₹Rs]*"
    m = re.search(
        rf"Cycle=(\d+).*?Positions=(\d+).*?Cash={cur}([\d,.-]+).*?"
        rf"DayPnL={cur}([\d,.+-]+).*?Trades=(\d+).*?ConsecLoss=(\d+).*?"
        rf"Cooldowns=\[([^\]]*)\].*?Blacklisted=\[([^\]]*)\]",
        last,
    )
    if not m:
        return {"heartbeat_seen": True, "raw": last}
    return {
        "heartbeat_seen": True,
        "ts": last[:19],
        "cycle": int(m.group(1)),
        "positions": int(m.group(2)),
        "cash": m.group(3),
        "day_pnl": m.group(4),
        "trades_today": int(m.group(5)),
        "consec_loss": int(m.group(6)),
        "cooldowns": [s.strip().strip("'\"") for s in m.group(7).split(",") if s.strip()],
        "blacklisted": [s.strip().strip("'\"") for s in m.group(8).split(",") if s.strip()],
    }


# ── Markdown rendering ───────────────────────────────────────────────────

def _render_markdown(now: datetime, data: Dict[str, Any], delta: Optional[Dict[str, Any]]) -> str:
    """Render the JSON snapshot into a human/agent-readable markdown report."""
    h = data.get("health", {})
    log = data.get("log_anomalies", {})
    pos = data.get("positions", {})
    tr = data.get("trades", {})
    pnl = data.get("day_pnl", {})
    sig = data.get("signal_pipeline", {})
    xgb = data.get("xgb", {})
    risk = data.get("risk_state", {})

    def _verdict() -> str:
        if not h.get("alive"):
            return "RED — daemon not alive"
        if log.get("error_count", 0) > 0:
            return "RED — errors in window"
        if log.get("traceback_count", 0) > 0:
            return "RED — traceback detected"
        if not pos.get("round_trip_ok", True):
            return "RED — DB round-trip failure"
        if log.get("warning_count", 0) > 50:
            return f"YELLOW — high warning count ({log['warning_count']})"
        return "GREEN"

    parts: List[str] = []
    parts.append(f"# Audit Checkpoint — {now.strftime('%H:%M IST  %Y-%m-%d')}")
    parts.append("")
    parts.append(f"**Window:** {data['window']['since']} → {data['window']['until']}  (60 min)")
    parts.append(f"**Verdict:** {_verdict()}")
    if delta:
        parts.append(f"**Previous checkpoint:** `{delta['filename']}` ({delta['minutes_ago']} min ago)")
    parts.append("")

    # Health
    parts.append("## Daemon health")
    if h.get("alive"):
        parts.append(
            f"- PID {h.get('pid')} alive · {h.get('ram_mb')} MB RAM · "
            f"{h.get('threads')} threads · {h.get('uptime_minutes')} min uptime · "
            f"status={h.get('status')}"
        )
    else:
        parts.append(f"- **DAEMON NOT ALIVE** (pid={h.get('pid')}, error={h.get('error')})")
    parts.append("")

    # Errors / warnings
    parts.append("## Errors & warnings (window only)")
    parts.append(
        f"- Errors: **{log.get('error_count', 0)}** · "
        f"Warnings: **{log.get('warning_count', 0)}** · "
        f"Tracebacks: **{log.get('traceback_count', 0)}**"
    )
    if log.get("errors_sample"):
        parts.append("")
        parts.append("**Errors:**")
        parts.append("```")
        for e in log["errors_sample"]:
            parts.append(e)
        parts.append("```")
    if log.get("warnings_top_groups"):
        parts.append("")
        parts.append("**Top warning groups:**")
        for g in log["warnings_top_groups"]:
            parts.append(f"- {g['count']}× `{g['sample']}`")
    parts.append("")

    # Day P&L
    parts.append("## Day P&L")
    parts.append(
        f"- Realised today: **₹{pnl.get('realised_pnl_today', 0):+.2f}** "
        f"({pnl.get('closed_trades_today', 0)} closed · "
        f"{pnl.get('winners', 0)}W / {pnl.get('losers', 0)}L · "
        f"WR {pnl.get('win_rate', 0)}%)"
    )
    if pnl.get("latest_equity_snapshot"):
        eq = pnl["latest_equity_snapshot"]
        parts.append(
            f"- Latest equity snapshot: ₹{eq['equity']:.2f} (cash ₹{eq['cash']:.2f}) "
            f"@ {eq['timestamp']}"
        )
    if pnl.get("exit_reasons"):
        parts.append(f"- Exit reasons today: {pnl['exit_reasons']}")
    parts.append("")

    # Trades in window
    parts.append("## Trades closed in this window")
    if tr.get("closed_count", 0) == 0:
        parts.append("- (none)")
    else:
        parts.append(f"- {tr['closed_count']} trades · realised ₹{tr.get('realised_pnl', 0):+.2f}")
        parts.append("")
        parts.append("| Symbol | Side | Qty | Entry | Exit | P&L | Reason |")
        parts.append("|---|---|---:|---:|---:|---:|---|")
        for t in tr.get("trades", []):
            parts.append(
                f"| {t['symbol']} | {t['side']} | {t['qty']} | "
                f"{t['entry']:.2f} | {t['exit']:.2f} | "
                f"₹{t['pnl']:+.2f} | {t['reason']} |"
            )
    parts.append("")

    # Open positions
    parts.append("## Open positions")
    if pos.get("open_count", 0) == 0:
        parts.append("- (none)")
    else:
        parts.append(f"- {pos['open_count']} open  ·  DB round-trip OK: {pos.get('round_trip_ok', True)}")
        parts.append("")
        parts.append("| Symbol | Side | Qty | Entry | SL | TP | Strategy | Regime |")
        parts.append("|---|---|---:|---:|---:|---:|---|---|")
        for p in pos.get("positions", []):
            parts.append(
                f"| {p['symbol']} | {p['side']} | {p['qty']} | "
                f"{p['entry_price']:.2f} | {p['stop_loss']:.2f} | "
                f"{p['take_profit']:.2f} | {p['strategy']} | {p['regime']} |"
            )
    if not pos.get("round_trip_ok", True):
        parts.append("")
        parts.append("**[!] DB round-trip failure:**")
        parts.append("```")
        parts.append(json.dumps(pos.get("round_trip_errors", []), indent=2))
        parts.append("```")
    parts.append("")

    # Signal pipeline
    parts.append("## Signal pipeline (window)")
    parts.append(
        f"- Cycles completed: **{sig.get('cycles_completed', 0)}** · "
        f"avg directional votes/cycle: {sig.get('avg_directional_votes', 0)} · "
        f"ensemble acts: **{sig.get('total_ensemble_acts', 0)}**"
    )
    parts.append(
        f"- Current regime: `{sig.get('current_regime')}` · "
        f"threshold: {sig.get('current_threshold')}"
    )
    if sig.get("gate_rejections"):
        parts.append(f"- Gate rejections: {sig['gate_rejections']}")
    if sig.get("ensemble_actions_sample"):
        parts.append("")
        parts.append("**Recent ensemble actions:**")
        parts.append("```")
        for a in sig["ensemble_actions_sample"]:
            parts.append(a)
        parts.append("```")
    parts.append("")

    # XGBoost
    parts.append("## XGBoost firing pattern")
    parts.append(
        f"- BUY: {xgb.get('buy', 0)} · SELL: {xgb.get('sell', 0)} · "
        f"HOLD: {xgb.get('hold', 0)} · firing rate: {xgb.get('firing_rate_pct', 0)}% · "
        f"SELL/BUY ratio: {xgb.get('sell_buy_ratio')}"
    )
    parts.append("")

    # Risk state
    parts.append("## Risk state (latest HEARTBEAT)")
    if not risk.get("heartbeat_seen"):
        parts.append("- (no heartbeat in window or earlier today)")
    else:
        parts.append(f"- Cycle: {risk.get('cycle')} · Positions: {risk.get('positions')}")
        parts.append(f"- Cash: ₹{risk.get('cash')} · Day P&L: ₹{risk.get('day_pnl')}")
        parts.append(
            f"- Trades today: {risk.get('trades_today')} · "
            f"Consec losses: {risk.get('consec_loss')}"
        )
        if risk.get("cooldowns"):
            parts.append(f"- Cooldowns: {risk['cooldowns']}")
        if risk.get("blacklisted"):
            parts.append(f"- Blacklisted: {risk['blacklisted']}")
    parts.append("")

    # Delta vs previous
    if delta:
        parts.append("## Delta vs previous checkpoint")
        parts.append(f"- Δ realised P&L: **₹{delta.get('pnl_change', 0):+.2f}**")
        parts.append(f"- Δ closed trades: {delta.get('trades_change', 0):+}")
        parts.append(f"- Δ open positions: {delta.get('positions_change', 0):+}")
        parts.append(f"- Δ errors: {delta.get('error_change', 0):+}")
        parts.append("")

    return "\n".join(parts)


def _build_delta(prev_path: Optional[Path], curr: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compute simple deltas vs the previous checkpoint."""
    if prev_path is None or not prev_path.exists():
        return None
    try:
        with open(prev_path, "r", encoding="utf-8") as f:
            prev = json.load(f)
        prev_now = datetime.fromisoformat(prev["timestamp"]).astimezone(IST)
        curr_now = datetime.fromisoformat(curr["timestamp"]).astimezone(IST)
        return {
            "filename": prev_path.name,
            "minutes_ago": int((curr_now - prev_now).total_seconds() / 60),
            "pnl_change": round(
                curr.get("day_pnl", {}).get("realised_pnl_today", 0)
                - prev.get("day_pnl", {}).get("realised_pnl_today", 0),
                2,
            ),
            "trades_change": (
                curr.get("day_pnl", {}).get("closed_trades_today", 0)
                - prev.get("day_pnl", {}).get("closed_trades_today", 0)
            ),
            "positions_change": (
                curr.get("positions", {}).get("open_count", 0)
                - prev.get("positions", {}).get("open_count", 0)
            ),
            "error_change": (
                curr.get("log_anomalies", {}).get("error_count", 0)
                - prev.get("log_anomalies", {}).get("error_count", 0)
            ),
        }
    except Exception:
        return None


# ── Public entry point ──────────────────────────────────────────────────

def run_and_save(
    db_path: str = "data/trading_agent.db",
    daemon_pid: Optional[int] = None,
    now: Optional[datetime] = None,
    window_minutes: int = 60,
) -> Tuple[Path, Path]:
    """Capture a checkpoint and write both .md and .json files.

    Returns (markdown_path, json_path). Never raises during normal operation —
    any failure is recorded as a section in the output.

    `daemon_pid` is auto-detected from the running process list if not
    provided. We deliberately avoid `os.getpid()` here because the audit
    checkpoint is sometimes run as a one-shot CLI by a different process
    (e.g. `python -m tools.audit_checkpoint`); using its own PID would
    misreport the "daemon" as a 0-uptime script. If no daemon is running,
    daemon_pid stays None and the health section reports that explicitly.
    """
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = IST.localize(now)
    if daemon_pid is None:
        daemon_pid = _find_running_daemon_pid()
    since = now - timedelta(minutes=window_minutes)

    # Read log once and reuse across sections.
    lines = _read_log_lines(_agent_log_path(now))

    data: Dict[str, Any] = {
        "timestamp": now.isoformat(),
        "window": {
            "since": since.strftime("%H:%M:%S"),
            "until": now.strftime("%H:%M:%S"),
            "minutes": window_minutes,
        },
    }
    safe_calls = (
        ("health", lambda: _section_health(daemon_pid)),
        ("log_anomalies", lambda: _section_log_anomalies(lines, since, now)),
        ("positions", lambda: _section_positions(db_path)),
        ("trades", lambda: _section_trades(db_path, since)),
        ("day_pnl", lambda: _section_day_pnl(db_path, now)),
        ("signal_pipeline", lambda: _section_signal_pipeline(lines, since, now)),
        ("xgb", lambda: _section_xgb_firing(lines, since, now)),
        ("risk_state", lambda: _section_risk_state(lines, since, now)),
    )
    for key, fn in safe_calls:
        try:
            data[key] = fn()
        except Exception as e:
            data[key] = {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()}

    md_path, json_path = _checkpoint_paths(now)
    delta = _build_delta(_previous_checkpoint(now), data)
    md = _render_markdown(now, data, delta)

    json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")
    return md_path, json_path


# ── Module-level CLI fallback (for tests / manual smoke) ────────────────

if __name__ == "__main__":
    md, js = run_and_save()
    print(f"Wrote: {md}")
    print(f"Wrote: {js}")
